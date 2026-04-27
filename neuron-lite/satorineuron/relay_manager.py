import io
import json
import os
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from satorineuron import config
from satorineuron import logging

try:
    import docker
    from docker.errors import DockerException, NotFound
except Exception:  # pragma: no cover - handled in migration paths
    docker = None

    class DockerException(Exception):
        pass

    class NotFound(Exception):
        pass


RELAY_MODES = {"off", "public", "private"}
DEFAULT_PUBLIC_PORT = 7777
DEFAULT_PRIVATE_PORT = 7171
STARTUP_WAIT_SECONDS = 10
STOP_WAIT_SECONDS = 8
DEFAULT_STRFRY_BIN = '/usr/local/bin/strfry'


@dataclass(frozen=True)
class RelayNames:
    mode: str
    process_name: str
    legacy_network: str
    legacy_strfry: str
    legacy_nginx: str
    legacy_db_volume: str


class LocalRelayManager:
    def __init__(self, startup):
        self.startup = startup
        self._lock = threading.Lock()
        self._last_error = None
        self._last_status = 'idle'
        self._docker_base_url = None
        self.default_public_port = int(os.environ.get(
            'SATORI_RELAY_PUBLIC_PORT', DEFAULT_PUBLIC_PORT))
        self.default_private_port = int(os.environ.get(
            'SATORI_RELAY_PRIVATE_PORT', DEFAULT_PRIVATE_PORT))
        self.public_port = self.default_public_port
        self.private_port = self.default_private_port
        self.public_host = ''
        self.private_host = ''
        self._base_name = f"satori-local-relay-{startup.uiPort}"

    def _refresh_ports(self) -> None:
        cfg = config.get()
        self.public_port = self._coerce_port(
            cfg.get('nostr relay public port'), self.default_public_port)
        self.private_port = self._coerce_port(
            cfg.get('nostr relay private port'), self.default_private_port)
        self.public_host = self._coerce_host(cfg.get('nostr relay public host'))
        self.private_host = self._coerce_host(cfg.get('nostr relay private host'))

    @staticmethod
    def _coerce_port(value: Any, default: int) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return default
        if 1 <= port <= 65535:
            return port
        return default

    @staticmethod
    def _coerce_host(value: Any) -> str:
        if value is None:
            return ''
        host = str(value).strip()
        return host.strip('/')

    def _docker_candidate_endpoints(self) -> list[str]:
        endpoints = []
        docker_host = os.environ.get('DOCKER_HOST', '').strip()
        if docker_host:
            endpoints.append(docker_host)
        docker_npipe = os.environ.get('SATORI_DOCKER_NPIPE', '').strip()
        if docker_npipe:
            endpoints.append(docker_npipe)
        if os.name == 'nt':
            endpoints.append('npipe:////./pipe/docker_engine')
        for socket_path in (
            os.environ.get('SATORI_DOCKER_SOCKET'),
            '/var/run/docker.sock',
            '/run/docker.sock',
            f"/run/user/{os.getuid()}/docker.sock",
            '/run/podman/podman.sock',
        ):
            if not socket_path:
                continue
            socket_path = str(socket_path).strip()
            if not socket_path or not os.path.exists(socket_path):
                continue
            endpoints.append(f'unix://{socket_path}')
        deduped = []
        seen = set()
        for endpoint in endpoints:
            if endpoint in seen:
                continue
            seen.add(endpoint)
            deduped.append(endpoint)
        return deduped

    @staticmethod
    def _describe_endpoint(endpoint: str | None) -> str:
        if not endpoint:
            return 'embedded-runtime'
        parsed = urlparse(endpoint)
        if parsed.scheme == 'unix':
            return parsed.path or endpoint
        if parsed.scheme == 'npipe':
            return endpoint
        return endpoint

    def _docker_client_optional(self):
        if docker is None:
            return None
        for endpoint in self._docker_candidate_endpoints():
            try:
                client = docker.DockerClient(base_url=endpoint, version='auto')
                client.ping()
                self._docker_base_url = endpoint
                return client
            except Exception:
                continue
        return None

    def desired_mode(self) -> str:
        self._refresh_ports()
        cfg = config.get()
        mode = str(cfg.get('nostr relay mode', 'off')).strip().lower()
        enabled = bool(cfg.get('nostr relay enabled', False))
        if mode not in RELAY_MODES:
            mode = 'off'
        if not enabled and mode != 'off':
            return 'off'
        return mode

    def persist_mode(self, mode: str, public_port: int | None = None,
                     private_port: int | None = None, public_host: str | None = None,
                     private_host: str | None = None) -> None:
        mode = (mode or 'off').strip().lower()
        if mode not in RELAY_MODES:
            raise ValueError(f'invalid relay mode: {mode}')
        public_port = self._coerce_port(
            public_port if public_port is not None else self.public_port,
            self.default_public_port)
        private_port = self._coerce_port(
            private_port if private_port is not None else self.private_port,
            self.default_private_port)
        if public_port != self.default_public_port:
            raise ValueError(
                f'embedded public relay currently requires container restart to use port {public_port}; '
                f'this container publishes {self.default_public_port}')
        if private_port != self.default_private_port:
            raise ValueError(
                f'embedded private relay currently requires container restart to use port {private_port}; '
                f'this container publishes {self.default_private_port}')
        public_host = self._coerce_host(
            public_host if public_host is not None else self.public_host)
        private_host = self._coerce_host(
            private_host if private_host is not None else self.private_host)
        config.add(data={
            'nostr relay enabled': mode != 'off',
            'nostr relay mode': mode,
            'nostr relay public port': public_port,
            'nostr relay private port': private_port,
            'nostr relay public host': public_host,
            'nostr relay private host': private_host,
        })
        self.public_port = public_port
        self.private_port = private_port
        self.public_host = public_host
        self.private_host = private_host
        logging.info(
            f'Relay: persisted desired mode={mode} '
            f'public_port={public_port} private_port={private_port} '
            f'public_host={public_host or "-"} private_host={private_host or "-"}',
            color='blue')

    def ensure_state_async(self) -> None:
        def runner():
            try:
                self.ensure_state()
            except Exception as exc:
                self._last_error = str(exc)
                self._last_status = 'error'
                logging.error(f'Relay: failed to reconcile relay state: {exc}')
        threading.Thread(target=runner, daemon=True).start()

    def ensure_state(self) -> dict[str, Any]:
        with self._lock:
            return self._ensure_state_locked()

    def stop_all(self) -> dict[str, Any]:
        with self._lock:
            return self._stop_all_locked()

    def status(self) -> dict[str, Any]:
        self._refresh_ports()
        desired_mode = self.desired_mode()
        public_status = self._service_status('public')
        private_status = self._service_status('private')
        running_mode = 'off'
        running = False
        if private_status.get('running'):
            running_mode = 'private'
            running = True
        elif public_status.get('running'):
            running_mode = 'public'
            running = True
        status = {
            'docker_available': True,
            'docker_error': None,
            'docker_endpoint': 'embedded strfry runtime',
            'docker_help': {
                'cause': 'embedded_runtime',
                'summary': 'Relay runs as an embedded strfry process inside the neuron container.',
                'details': [
                    'Nginx sidecars are no longer required.',
                    f'This container publishes public relay port {self.default_public_port} and private relay port {self.default_private_port}.',
                ],
            },
            'last_error': self._last_error,
            'last_status': self._last_status,
            'desired_mode': desired_mode,
            'running_mode': running_mode,
            'running': running,
            'public_port': self.public_port,
            'private_port': self.private_port,
            'public_host': self.public_host,
            'private_host': self.private_host,
            'nostr_pubkey': self.startup.nostrPubkey,
            'containers': {
                'public': {'strfry': public_status},
                'private': {'strfry': private_status},
            },
            'port_conflicts': self._port_conflicts(),
            'last_event_at': self._last_event_time({
                'public': {'strfry': public_status},
                'private': {'strfry': private_status},
            }),
            'last_health_check_at': self._last_healthcheck_time({
                'public': {'strfry': public_status},
                'private': {'strfry': private_status},
            }),
        }
        return status

    def _ensure_state_locked(self) -> dict[str, Any]:
        self._refresh_ports()
        mode = self.desired_mode()
        if mode == 'off':
            return self._stop_all_locked()
        if not self.startup.nostrPubkey:
            raise RuntimeError('cannot start local relay without nostr pubkey')
        if self._port_conflicts()[mode]:
            self._last_error = self._port_conflicts()[mode]
            self._last_status = 'error'
            raise RuntimeError(self._last_error)
        self._last_status = f'starting-{mode}'
        self._last_error = None
        logging.info(
            f'Relay: reconciling embedded mode={mode} public_port={self.public_port} '
            f'private_port={self.private_port}',
            color='blue')
        other_mode = 'private' if mode == 'public' else 'public'
        self._stop_mode_process(other_mode)
        self._stop_legacy_mode(mode)
        self._stop_legacy_mode(other_mode)
        self._migrate_legacy_db_if_needed(mode)
        self._write_mode_assets(mode)
        self._start_mode_process(mode)
        status = self.status()
        self._last_status = f'running-{mode}' if status['running'] else 'stopped'
        logging.info(
            f"Relay: active embedded mode={mode} running={status['running']} "
            f"public_port={self.public_port} private_port={self.private_port}",
            color='green')
        return status

    def _stop_all_locked(self) -> dict[str, Any]:
        stopped_any = False
        stopped_any |= bool(self._stop_mode_process('public'))
        stopped_any |= bool(self._stop_mode_process('private'))
        stopped_any |= bool(self._stop_legacy_mode('public'))
        stopped_any |= bool(self._stop_legacy_mode('private'))
        self._last_status = 'off'
        self._last_error = None
        if stopped_any:
            logging.info('Relay: stopped embedded relay runtime', color='yellow')
        return self.status()

    def _port_conflicts(self) -> dict[str, str | None]:
        conflicts = {'public': None, 'private': None}
        if self.public_port != self.default_public_port:
            conflicts['public'] = (
                f'embedded public relay port {self.public_port} requires recreating the neuron container; '
                f'current published port is {self.default_public_port}')
        if self.private_port != self.default_private_port:
            conflicts['private'] = (
                f'embedded private relay port {self.private_port} requires recreating the neuron container; '
                f'current published port is {self.default_private_port}')
        return conflicts

    def _mode_dir(self, mode: str) -> Path:
        engine_db = Path('/Satori/Engine/db')
        if engine_db.exists() and os.access(engine_db, os.W_OK):
            return engine_db / 'relay' / mode
        configured = Path(config.dataPath())
        if configured.exists() and os.access(configured, os.W_OK):
            return configured / 'relay' / mode
        return engine_db / 'relay' / mode

    def _db_dir(self, mode: str) -> Path:
        return self._mode_dir(mode) / 'strfry-db'

    def _conf_path(self, mode: str) -> Path:
        return self._mode_dir(mode) / 'strfry.conf'

    def _policy_path(self, mode: str) -> Path:
        return self._mode_dir(mode) / 'write-policy.py'

    def _log_path(self, mode: str) -> Path:
        return self._mode_dir(mode) / 'strfry.log'

    def _pid_path(self, mode: str) -> Path:
        return self._mode_dir(mode) / 'strfry.pid'

    def _migration_marker_path(self, mode: str) -> Path:
        return self._mode_dir(mode) / 'migration.json'

    def _runtime_port(self, mode: str) -> int:
        return self.default_public_port if mode == 'public' else self.default_private_port

    def _ensure_mode_dir(self, mode: str) -> Path:
        root = self._mode_dir(mode)
        root.mkdir(parents=True, exist_ok=True)
        self._db_dir(mode).mkdir(parents=True, exist_ok=True)
        return root

    def _mode_process_name(self, mode: str) -> str:
        return f'{self._base_name}-{mode}-embedded-strfry'

    def _read_pid(self, mode: str) -> int | None:
        path = self._pid_path(mode)
        if not path.exists():
            return None
        try:
            return int(path.read_text().strip())
        except Exception:
            return None

    def _write_pid(self, mode: str, pid: int) -> None:
        self._pid_path(mode).write_text(str(pid))

    def _clear_pid(self, mode: str) -> None:
        try:
            self._pid_path(mode).unlink(missing_ok=True)
        except Exception:
            pass

    @staticmethod
    def _is_process_alive(pid: int | None) -> bool:
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _stop_mode_process(self, mode: str) -> bool:
        pid = self._read_pid(mode)
        if not self._is_process_alive(pid):
            self._clear_pid(mode)
            return False
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._clear_pid(mode)
            return False
        deadline = time.time() + STOP_WAIT_SECONDS
        while time.time() < deadline:
            if not self._is_process_alive(pid):
                break
            time.sleep(0.2)
        if self._is_process_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._clear_pid(mode)
        logging.info(f'Relay: stopped embedded {mode} strfry process', color='yellow')
        return True

    def _start_mode_process(self, mode: str) -> None:
        strfry_bin = shutil.which('strfry') or (
            DEFAULT_STRFRY_BIN if Path(DEFAULT_STRFRY_BIN).exists() else None
        )
        if strfry_bin is None:
            raise RuntimeError('embedded strfry binary is missing from the neuron image')
        self._ensure_mode_dir(mode)
        port = self._runtime_port(mode)
        if not self._can_bind_port(port):
            raise RuntimeError(f'embedded {mode} relay cannot bind to port {port}')
        log_path = self._log_path(mode)
        with open(log_path, 'ab') as log_file:
            process = subprocess.Popen(
                [strfry_bin, f'--config={self._conf_path(mode)}', 'relay'],
                cwd=str(self._mode_dir(mode)),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._write_pid(mode, process.pid)
        if not self._wait_for_relay(port):
            self._stop_mode_process(mode)
            raise RuntimeError(f'embedded {mode} relay failed health check on port {port}')
        logging.info(
            f'Relay: started embedded {mode} strfry process on port {port}',
            color='green')

    @staticmethod
    def _can_bind_port(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(('0.0.0.0', port))
            except OSError:
                return False
        return True

    def _wait_for_relay(self, port: int) -> bool:
        deadline = time.time() + STARTUP_WAIT_SECONDS
        while time.time() < deadline:
            if self._probe_nip11(port):
                return True
            time.sleep(0.3)
        return False

    @staticmethod
    def _probe_nip11(port: int) -> bool:
        req = urllib.request.Request(
            f'http://127.0.0.1:{port}/',
            headers={'Accept': 'application/nostr+json'},
        )
        try:
            with urllib.request.urlopen(req, timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def _service_status(self, mode: str) -> dict[str, Any]:
        pid = self._read_pid(mode)
        port = self._runtime_port(mode)
        running = self._is_process_alive(pid)
        health = None
        last_health = None
        if running:
            health = 'healthy' if self._probe_nip11(port) else 'starting'
            last_health = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        return {
            'exists': self._conf_path(mode).exists() or self._db_dir(mode).exists(),
            'running': running,
            'status': 'running' if running else 'stopped',
            'name': self._mode_process_name(mode),
            'image': ['embedded:strfry'],
            'ports': {'tcp': [{'HostIp': '', 'HostPort': str(port)}]},
            'health': health,
            'last_health_check_at': last_health,
            'pid': pid,
            'backend': 'embedded',
        }

    @staticmethod
    def _last_healthcheck_time(containers: dict[str, Any]) -> str | None:
        values = []
        for mode_data in containers.values():
            for container_data in mode_data.values():
                value = container_data.get('last_health_check_at')
                if value:
                    values.append(value)
        return max(values) if values else None

    def _last_event_time(self, containers: dict[str, Any]) -> str | None:
        latest = None
        for mode in ('public', 'private'):
            strfry = containers.get(mode, {}).get('strfry', {})
            if not strfry.get('exists'):
                continue
            candidate = self._log_last_event_time(mode)
            if candidate and (latest is None or candidate > latest):
                latest = candidate
        if latest is None:
            return None
        return latest.isoformat().replace('+00:00', 'Z')

    def _log_last_event_time(self, mode: str) -> datetime | None:
        log_path = self._log_path(mode)
        if not log_path.exists():
            return None
        try:
            lines = log_path.read_text(errors='ignore').splitlines()[-200:]
        except Exception:
            return None
        for line in reversed(lines):
            if 'Inserted event.' not in line and 'Deleting event (d-tag).' not in line:
                continue
            try:
                stamp = line.split(' (', 1)[0]
                parsed = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S.%f')
                return parsed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        return None

    def _names(self, mode: str) -> RelayNames:
        return RelayNames(
            mode=mode,
            process_name=self._mode_process_name(mode),
            legacy_network=f'{self._base_name}-{mode}-net',
            legacy_strfry=f'{self._base_name}-{mode}-strfry',
            legacy_nginx=f'{self._base_name}-{mode}-nginx',
            legacy_db_volume=f'{self._base_name}-{mode}-db',
        )

    def _stop_legacy_mode(self, mode: str) -> bool:
        client = self._docker_client_optional()
        if client is None:
            return False
        removed_any = False
        names = self._names(mode)
        try:
            for container_name in (names.legacy_nginx, names.legacy_strfry):
                try:
                    container = client.containers.get(container_name)
                    container.remove(force=True)
                    removed_any = True
                    logging.info(
                        f'Relay: removed legacy {mode} sidecar {container_name}',
                        color='yellow')
                except NotFound:
                    pass
            try:
                network = client.networks.get(names.legacy_network)
                network.remove()
                removed_any = True
            except NotFound:
                pass
            except DockerException as exc:
                logging.warning(f'Relay: could not remove legacy network {names.legacy_network}: {exc}')
        finally:
            client.close()
        return removed_any

    def _migrate_legacy_db_if_needed(self, mode: str) -> bool:
        db_dir = self._db_dir(mode)
        marker = self._migration_marker_path(mode)
        if marker.exists():
            return False
        if db_dir.exists() and any(db_dir.iterdir()):
            marker.write_text(json.dumps({'migrated_from': 'existing-local-db'}))
            return False
        client = self._docker_client_optional()
        if client is None:
            return False
        try:
            for volume_name in self._legacy_volume_candidates(mode):
                if not self._volume_exists(client, volume_name):
                    continue
                self._copy_volume_tree(client, volume_name, db_dir)
                marker.write_text(json.dumps({
                    'migrated_from': volume_name,
                    'migrated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                }, indent=2))
                logging.info(
                    f'Relay: migrated embedded {mode} DB from legacy volume {volume_name}',
                    color='green')
                return True
        finally:
            client.close()
        return False

    def _legacy_volume_candidates(self, mode: str) -> list[str]:
        names = self._names(mode)
        if mode == 'public':
            candidates = [names.legacy_db_volume, 'satori-relay_strfry-db']
        else:
            # Prefer the long-lived dedicated 7171 relay volume over the
            # later sidecar-private volume, which may be empty in migrated installs.
            candidates = ['satori-relay-7171_strfry-db', names.legacy_db_volume]
        deduped = []
        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            deduped.append(candidate)
        return deduped

    @staticmethod
    def _volume_exists(client, volume_name: str) -> bool:
        try:
            client.volumes.get(volume_name)
            return True
        except NotFound:
            return False
        except DockerException:
            return False

    def _copy_volume_tree(self, client, volume_name: str, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        helper = client.containers.create(
            'alpine:latest',
            command=['sh', '-lc', 'sleep 60'],
            volumes={volume_name: {'bind': '/src', 'mode': 'ro'}},
        )
        tar_path = None
        extract_dir = None
        try:
            helper.start()
            stream, _ = helper.get_archive('/src')
            fd, tar_path = tempfile.mkstemp(prefix='relay-volume-', suffix='.tar')
            os.close(fd)
            with open(tar_path, 'wb') as handle:
                for chunk in stream:
                    handle.write(chunk)
            extract_dir = Path(tempfile.mkdtemp(prefix='relay-extract-'))
            with tarfile.open(tar_path) as archive:
                archive.extractall(path=extract_dir)
            roots = [child for child in extract_dir.iterdir()]
            source_root = roots[0] if len(roots) == 1 and roots[0].is_dir() else extract_dir
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.copytree(source_root, target_dir)
        finally:
            try:
                helper.remove(force=True)
            except Exception:
                pass
            if tar_path and os.path.exists(tar_path):
                os.unlink(tar_path)
            if extract_dir and extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)

    def _write_mode_assets(self, mode: str) -> None:
        root = self._ensure_mode_dir(mode)
        relay_name = f'Satori {mode.capitalize()} Relay'
        relay_description = (
            'A Satori Network relay managed by Satori Lite '
            f'({mode} mode)'
        )
        self._conf_path(mode).write_text(
            self._render_strfry_conf(mode, relay_name, relay_description)
        )
        if mode == 'private':
            policy_path = self._policy_path(mode)
            policy_path.write_text(self._render_private_policy(self.startup.nostrPubkey))
            os.chmod(policy_path, 0o755)
        else:
            self._policy_path(mode).unlink(missing_ok=True)
        logging.info(f'Relay: wrote embedded {mode} relay assets', color='blue')

    def _render_strfry_conf(self, mode: str, relay_name: str, relay_description: str) -> str:
        plugin = f'"{self._policy_path(mode)}"' if mode == 'private' else '""'
        return f'''##\n## Satori Lite managed embedded strfry configuration\n##\n\ndb = "{self._db_dir(mode)}/"\n\ndbParams {{\n    maxreaders = 256\n    mapsize = 10995116277760\n    noReadAhead = false\n}}\n\nevents {{\n    maxEventSize = 65536\n    rejectEventsNewerThanSeconds = 900\n    rejectEventsOlderThanSeconds = 94608000\n    rejectEphemeralEventsOlderThanSeconds = 60\n    ephemeralEventsLifetimeSeconds = 300\n    maxNumTags = 2000\n    maxTagValSize = 1024\n}}\n\nrelay {{\n    bind = "0.0.0.0"\n    port = {self._runtime_port(mode)}\n    nofiles = 0\n    realIpHeader = "x-real-ip"\n\n    info {{\n        name = "{relay_name}"\n        description = "{relay_description}"\n        pubkey = "{self.startup.nostrPubkey or ''}"\n        contact = ""\n        icon = ""\n        nips = ""\n    }}\n\n    maxWebsocketPayloadSize = 131072\n    maxReqFilterSize = 200\n    autoPingSeconds = 55\n    enableTcpKeepalive = false\n    queryTimesliceBudgetMicroseconds = 10000\n    maxFilterLimit = 500\n    maxSubsPerConnection = 20\n\n    writePolicy {{\n        plugin = {plugin}\n    }}\n\n    compression {{\n        enabled = true\n        slidingWindow = true\n    }}\n\n    logging {{\n        dumpInAll = false\n        dumpInEvents = false\n        dumpInReqs = false\n        dbScanPerf = false\n        invalidEvents = true\n    }}\n\n    numThreads {{\n        ingester = 3\n        reqWorker = 3\n        reqMonitor = 3\n        negentropy = 2\n    }}\n\n    negentropy {{\n        enabled = true\n        maxSyncEvents = 1000000\n    }}\n}}\n'''

    def _render_private_policy(self, pubkey: str) -> str:
        return f'''#!/usr/bin/env python3\n\nimport json\nimport subprocess\nimport sys\nfrom typing import Any\n\n\nSTRFRY_BIN = "{DEFAULT_STRFRY_BIN}"\nMY_PUBKEYS = {{\n    "{pubkey.lower()}",\n}}\n\nALLOWED_SOURCE_TYPES = {{\n    "Stream",\n    "Import",\n    "Sync",\n}}\n\nALLOWED_KINDS = {{\n    10002,\n}}\n\n\ndef eprint(message: str) -> None:\n    print(message, file=sys.stderr, flush=True)\n\n\ndef respond(event_id: str, action: str, msg: str | None = None) -> None:\n    payload = {{\n        "id": event_id,\n        "action": action,\n    }}\n    if msg:\n        payload["msg"] = msg\n    print(json.dumps(payload, separators=(",", ":")), file=sys.stdout, flush=True)\n\n\ndef get_d_tag(event: dict[str, Any]) -> str | None:\n    tags = event.get("tags")\n    if not isinstance(tags, list):\n        return None\n    for tag in tags:\n        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d":\n            return str(tag[1])\n    return None\n\n\ndef is_prediction_event(event: dict[str, Any]) -> bool:\n    d_tag = get_d_tag(event)\n    return bool(d_tag and d_tag.endswith("_pred"))\n\n\ndef source_stream_from_prediction(d_tag: str | None) -> str | None:\n    if not d_tag or not d_tag.endswith("_pred"):\n        return None\n    return d_tag[:-5]\n\n\ndef parse_kind(event: dict[str, Any]) -> int | None:\n    kind = event.get("kind")\n    try:\n        return int(kind)\n    except (TypeError, ValueError):\n        return None\n\n\ndef relay_has_my_source_stream(source_stream: str) -> bool:\n    try:\n        raw = subprocess.check_output(\n            [STRFRY_BIN, f"--config={self._conf_path('private')}", "export"],\n            text=True,\n            stderr=subprocess.DEVNULL,\n        )\n    except Exception as exc:\n        eprint(f"error: failed to query relay db: {{exc}}")\n        return False\n\n    for line in raw.splitlines():\n        line = line.strip()\n        if not line.startswith("{{"):\n            continue\n        try:\n            event = json.loads(line)\n        except json.JSONDecodeError:\n            continue\n\n        event_pubkey = event.get("pubkey")\n        kind = parse_kind(event)\n        d_tag = get_d_tag(event)\n\n        if event_pubkey not in MY_PUBKEYS:\n            continue\n        if d_tag != source_stream:\n            continue\n        if kind not in {{34600, 34601}}:\n            continue\n\n        return True\n\n    return False\n\n\ndef accept(event_id: str, reason: str) -> None:\n    eprint(f"accept: {{reason}}")\n    respond(event_id, "accept")\n\n\ndef reject(event_id: str, reason: str) -> None:\n    eprint(f"reject: {{reason}}")\n    respond(event_id, "reject", reason)\n\n\ndef handle_request(request: dict[str, Any]) -> None:\n    request_type = request.get("type")\n\n    if request_type == "lookback":\n        return\n\n    if request_type != "new":\n        eprint(f"ignore: unexpected request type={{request_type!r}}")\n        return\n\n    event = request.get("event")\n    if not isinstance(event, dict):\n        eprint("reject: malformed request without event object")\n        return\n\n    event_id = event.get("id")\n    if not isinstance(event_id, str) or not event_id:\n        eprint("reject: missing event id")\n        return\n\n    event_pubkey = event.get("pubkey")\n    if not isinstance(event_pubkey, str) or not event_pubkey:\n        reject(event_id, "blocked: missing pubkey")\n        return\n\n    kind = parse_kind(event)\n    d_tag = get_d_tag(event)\n    source_type = request.get("sourceType")\n    source_info = request.get("sourceInfo")\n\n    if event_pubkey in MY_PUBKEYS:\n        accept(event_id, f"own pubkey={{event_pubkey[:12]}}")\n        return\n\n    if kind in ALLOWED_KINDS:\n        accept(event_id, f"allowed kind={{kind}}")\n        return\n\n    if source_type in ALLOWED_SOURCE_TYPES:\n        accept(event_id, f"allowed sourceType={{source_type}}")\n        return\n\n    if is_prediction_event(event):\n        source_stream = source_stream_from_prediction(d_tag)\n        if source_stream and relay_has_my_source_stream(source_stream):\n            accept(\n                event_id,\n                f"foreign prediction for owned source stream pubkey={{event_pubkey[:12]}} source={{source_stream}}",\n            )\n            return\n\n        reject(\n            event_id,\n            (\n                "blocked: foreign prediction does not target an owned source stream "\n                f"(pubkey={{event_pubkey[:12]}}, d={{d_tag}}, source={{source_stream}}, "\n                f"sourceType={{source_type}}, sourceInfo={{source_info}})"\n            ),\n        )\n        return\n\n    reject(\n        event_id,\n        (\n            "blocked: foreign pubkey may not publish non-prediction streams "\n            f"(pubkey={{event_pubkey[:12]}}, kind={{kind}}, d={{d_tag}}, "\n            f"sourceType={{source_type}}, sourceInfo={{source_info}})"\n        ),\n    )\n\n\ndef main() -> None:\n    for line in sys.stdin:\n        line = line.strip()\n        if not line:\n            continue\n\n        try:\n            request = json.loads(line)\n        except json.JSONDecodeError as exc:\n            eprint(f"ignore: invalid json input: {{exc}}")\n            continue\n\n        if not isinstance(request, dict):\n            eprint("ignore: request is not a JSON object")\n            continue\n\n        try:\n            handle_request(request)\n        except Exception as exc:\n            event = request.get("event", {{}})\n            event_id = event.get("id") if isinstance(event, dict) else None\n            eprint(f"error: unexpected exception: {{exc}}")\n            if isinstance(event_id, str) and event_id:\n                reject(event_id, "blocked: internal write policy error")\n\n\nif __name__ == "__main__":\n    main()\n'''
