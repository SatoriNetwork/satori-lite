import json
import os
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from satorineuron import config
from satorineuron import logging

try:
    import docker
    from docker.errors import DockerException, NotFound
except Exception:  # pragma: no cover - handled in status paths
    docker = None

    class DockerException(Exception):
        pass

    class NotFound(Exception):
        pass


RELAY_MODES = {"off", "public", "private"}
DEFAULT_PUBLIC_PORT = 7777
DEFAULT_PRIVATE_PORT = 7171
@dataclass(frozen=True)
class RelayNames:
    mode: str
    network: str
    strfry: str
    nginx: str
    db_volume: str
    assets_volume: str
    nip11_volume: str
    nginx_conf_volume: str


class LocalRelayManager:
    def __init__(self, startup):
        self.startup = startup
        self._lock = threading.Lock()
        self._last_error = None
        self._last_status = 'idle'
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

    def docker_available(self) -> bool:
        if docker is None:
            self._last_error = 'python docker package is not installed'
            return False
        if not os.path.exists('/var/run/docker.sock'):
            self._last_error = 'docker socket /var/run/docker.sock is not mounted'
            return False
        try:
            client = docker.from_env()
            client.ping()
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

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
        status = {
            'docker_available': self.docker_available(),
            'docker_error': self._last_error,
            'last_error': self._last_error,
            'last_status': self._last_status,
            'desired_mode': self.desired_mode(),
            'running_mode': 'off',
            'running': False,
            'public_port': self.public_port,
            'private_port': self.private_port,
            'public_host': self.public_host,
            'private_host': self.private_host,
            'nostr_pubkey': self.startup.nostrPubkey,
            'containers': {},
            'port_conflicts': {},
            'last_event_at': None,
            'last_health_check_at': None,
        }
        if not status['docker_available']:
            return status
        client = docker.from_env()
        public_running = self._mode_running(client, 'public')
        private_running = self._mode_running(client, 'private')
        if private_running:
            status['running_mode'] = 'private'
            status['running'] = True
        elif public_running:
            status['running_mode'] = 'public'
            status['running'] = True
        for mode in ('public', 'private'):
            names = self._names(mode)
            host_port = self.public_port if mode == 'public' else self.private_port
            status['containers'][mode] = {
                'strfry': self._container_status(client, names.strfry),
                'nginx': self._container_status(client, names.nginx),
            }
            status['port_conflicts'][mode] = self._port_owner(
                client, host_port, names.nginx)
        status['last_event_at'] = self._last_event_time(status['containers'])
        status['last_health_check_at'] = self._last_healthcheck_time(status['containers'])
        return status

    def _ensure_state_locked(self) -> dict[str, Any]:
        self._refresh_ports()
        mode = self.desired_mode()
        if mode == 'off':
            return self._stop_all_locked()
        if not self.startup.nostrPubkey:
            raise RuntimeError('cannot start local relay without nostr pubkey')
        if not self.docker_available():
            raise RuntimeError(self._last_error or 'docker is not available')
        client = docker.from_env()
        requested_port = self.public_port if mode == 'public' else self.private_port
        port_owner = self._port_owner(client, requested_port, self._names(mode).nginx)
        if port_owner:
            self._last_error = (
                f'relay {mode} port {requested_port} is already in use by {port_owner}')
            self._last_status = 'error'
            logging.error(f'Relay: {self._last_error}')
            raise RuntimeError(self._last_error)
        self._last_status = f'starting-{mode}'
        self._last_error = None
        logging.info(
            f'Relay: reconciling mode={mode} public_port={self.public_port} '
            f'private_port={self.private_port}',
            color='blue')
        other_mode = 'private' if mode == 'public' else 'public'
        self._stop_mode_locked(client, other_mode)
        self._recreate_mode_locked(client, mode)
        status = self.status()
        self._last_status = f'running-{mode}' if status['running'] else 'stopped'
        logging.info(
            f"Relay: active mode={mode} running={status['running']} "
            f"public_port={self.public_port} private_port={self.private_port}",
            color='green')
        return status

    def _stop_all_locked(self) -> dict[str, Any]:
        if not self.docker_available():
            return self.status()
        client = docker.from_env()
        self._stop_mode_locked(client, 'public')
        self._stop_mode_locked(client, 'private')
        self._last_status = 'off'
        self._last_error = None
        logging.info('Relay: stopped all local relay sidecars', color='yellow')
        return self.status()

    def _recreate_mode_locked(self, client, mode: str) -> None:
        self._stop_mode_locked(client, mode)
        names = self._names(mode)
        self._ensure_network(client, names.network)
        for volume_name in (
            names.db_volume,
            names.assets_volume,
            names.nip11_volume,
            names.nginx_conf_volume,
        ):
            self._ensure_volume(client, volume_name)
        self._populate_mode_assets(client, mode, names)
        self._start_strfry(client, mode, names)
        self._start_nginx(client, mode, names)

    def _start_strfry(self, client, mode: str, names: RelayNames) -> None:
        labels = self._labels(mode)
        container = client.containers.run(
            'dockurr/strfry:latest',
            name=names.strfry,
            command=[
                'sh', '-lc',
                'cp /relay-assets/strfry.conf /etc/strfry.conf && '
                'if [ -f /relay-assets/write-policy.py ]; then '
                'cp /relay-assets/write-policy.py /app/write-policy.py && '
                'chmod 755 /app/write-policy.py; fi && '
                'exec /app/strfry relay'
            ],
            detach=True,
            restart_policy={'Name': 'unless-stopped'},
            network=names.network,
            hostname='strfry',
            labels=labels,
            volumes={
                names.db_volume: {'bind': '/app/strfry-db', 'mode': 'rw'},
                names.assets_volume: {'bind': '/relay-assets', 'mode': 'ro'},
            },
            working_dir='/app',
        )
        logging.info(
            f'Relay: started {mode} strfry sidecar {container.name}',
            color='green')

    def _start_nginx(self, client, mode: str, names: RelayNames) -> None:
        labels = self._labels(mode)
        host_port = self.public_port if mode == 'public' else self.private_port
        port_owner = self._port_owner(client, host_port, names.nginx)
        if port_owner:
            raise RuntimeError(
                f'relay {mode} port {host_port} is already in use by {port_owner}')
        container = client.containers.run(
            'nginx:alpine',
            name=names.nginx,
            detach=True,
            restart_policy={'Name': 'unless-stopped'},
            network=names.network,
            labels=labels,
            ports={'80/tcp': host_port},
            volumes={
                names.nip11_volume: {'bind': '/usr/share/nginx/nip11', 'mode': 'ro'},
                names.nginx_conf_volume: {'bind': '/etc/nginx/conf.d', 'mode': 'ro'},
            },
        )
        logging.info(
            f'Relay: started {mode} nginx sidecar {container.name} on port {host_port}',
            color='green')

    def _populate_mode_assets(self, client, mode: str, names: RelayNames) -> None:
        relay_name = f'Satori {mode.capitalize()} Relay'
        relay_description = (
            'A Satori Network relay managed by Satori Lite '
            f'({mode} mode)'
        )
        nip11 = json.dumps({
            'name': relay_name,
            'description': relay_description,
            'pubkey': self.startup.nostrPubkey,
            'self': self.startup.nostrPubkey,
            'contact': '',
            'supported_nips': [1, 2, 4, 9, 11, 22, 28, 40, 70],
            'software': 'https://github.com/hoytech/strfry',
            'version': 'strfry',
        }, indent=2) + '\n'
        script = '\n'.join([
            'set -e',
            'mkdir -p /assets /nip11 /nginx',
            "cat > /assets/strfry.conf <<'EOF_STRFRY'",
            self._render_strfry_conf(mode, relay_name, relay_description),
            'EOF_STRFRY',
            "cat > /nginx/relay.conf <<'EOF_NGINX'",
            self._render_nginx_conf(names.strfry),
            'EOF_NGINX',
            "cat > /nip11/nip11.json <<'EOF_NIP11'",
            nip11.rstrip('\n'),
            'EOF_NIP11',
        ])
        if mode == 'private':
            script += '\n' + '\n'.join([
                "cat > /assets/write-policy.py <<'EOF_POLICY'",
                self._render_private_policy(self.startup.nostrPubkey),
                'EOF_POLICY',
                'chmod 755 /assets/write-policy.py',
            ])
        client.containers.run(
            'alpine:latest',
            command=['sh', '-lc', script],
            remove=True,
            volumes={
                names.assets_volume: {'bind': '/assets', 'mode': 'rw'},
                names.nip11_volume: {'bind': '/nip11', 'mode': 'rw'},
                names.nginx_conf_volume: {'bind': '/nginx', 'mode': 'rw'},
            },
        )
        logging.info(f'Relay: wrote {mode} relay assets', color='blue')

    def _stop_mode_locked(self, client, mode: str) -> None:
        names = self._names(mode)
        for container_name in (names.nginx, names.strfry):
            try:
                container = client.containers.get(container_name)
                container.remove(force=True)
                logging.info(
                    f'Relay: removed {mode} sidecar {container_name}',
                    color='yellow')
            except NotFound:
                pass
        try:
            network = client.networks.get(names.network)
            network.remove()
        except NotFound:
            pass
        except DockerException as exc:
            logging.warning(f'Relay: could not remove network {names.network}: {exc}')

    def _port_owner(self, client, host_port: int, expected_container: str | None = None) -> str | None:
        host_port = str(host_port)
        for container in client.containers.list(all=True):
            if expected_container and container.name == expected_container:
                continue
            ports = container.attrs.get('NetworkSettings', {}).get('Ports', {}) or {}
            for bindings in ports.values():
                if not bindings:
                    continue
                for binding in bindings:
                    if str(binding.get('HostPort')) == host_port:
                        return container.name
        return None

    def _mode_running(self, client, mode: str) -> bool:
        names = self._names(mode)
        return (
            self._container_status(client, names.strfry).get('running', False) and
            self._container_status(client, names.nginx).get('running', False)
        )

    def _container_status(self, client, container_name: str) -> dict[str, Any]:
        try:
            container = client.containers.get(container_name)
            container.reload()
            ports = container.attrs.get('NetworkSettings', {}).get('Ports', {})
            health = (
                container.attrs.get('State', {})
                .get('Health', {})
            )
            health_logs = health.get('Log', []) or []
            last_health = None
            if health_logs:
                last_health = (
                    health_logs[-1].get('End') or
                    health_logs[-1].get('Start')
                )
            return {
                'exists': True,
                'running': container.status == 'running',
                'status': container.status,
                'name': container.name,
                'image': container.image.tags,
                'ports': ports,
                'health': health.get('Status'),
                'last_health_check_at': last_health,
            }
        except NotFound:
            return {'exists': False, 'running': False, 'status': 'missing', 'ports': {}}
        except DockerException as exc:
            return {'exists': False, 'running': False, 'status': f'error: {exc}', 'ports': {}}

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
        for mode_data in containers.values():
            strfry = mode_data.get('strfry', {})
            if not strfry.get('exists'):
                continue
            candidate = self._container_last_event_time(strfry)
            if candidate and (latest is None or candidate > latest):
                latest = candidate
        if latest is None:
            return None
        return latest.isoformat() + 'Z'

    def _container_last_event_time(self, container_data: dict[str, Any]) -> datetime | None:
        image_tags = container_data.get('image') or []
        if not image_tags:
            return None
        try:
            client = docker.from_env()
            container = client.containers.get(
                container_data.get('name') or image_tags[0]
            )
        except Exception:
            return None
        try:
            logs = container.logs(tail=200).decode('utf-8', errors='ignore').splitlines()
        except Exception:
            return None
        for line in reversed(logs):
            if 'Inserted event.' not in line and 'Deleting event (d-tag).' not in line:
                continue
            try:
                stamp = line.split(' (', 1)[0]
                parsed = datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S.%f')
                return parsed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
        return None

    def _ensure_network(self, client, network_name: str) -> None:
        try:
            client.networks.get(network_name)
        except NotFound:
            client.networks.create(network_name, driver='bridge')

    def _ensure_volume(self, client, volume_name: str) -> None:
        try:
            client.volumes.get(volume_name)
        except NotFound:
            client.volumes.create(name=volume_name)

    def _names(self, mode: str) -> RelayNames:
        return RelayNames(
            mode=mode,
            network=f'{self._base_name}-{mode}-net',
            strfry=f'{self._base_name}-{mode}-strfry',
            nginx=f'{self._base_name}-{mode}-nginx',
            db_volume=f'{self._base_name}-{mode}-db',
            assets_volume=f'{self._base_name}-{mode}-assets',
            nip11_volume=f'{self._base_name}-{mode}-nip11',
            nginx_conf_volume=f'{self._base_name}-{mode}-nginx-conf',
        )

    def _labels(self, mode: str) -> dict[str, str]:
        return {
            'satori.managed': 'true',
            'satori.component': 'relay',
            'satori.mode': mode,
            'satori.ui_port': str(self.startup.uiPort),
        }

    def _render_strfry_conf(self, mode: str, relay_name: str, relay_description: str) -> str:
        plugin = '"/app/write-policy.py"' if mode == 'private' else '""'
        return f'''##\n## Satori Lite managed strfry configuration\n##\n\ndb = "./strfry-db/"\n\ndbParams {{\n    maxreaders = 256\n    mapsize = 10995116277760\n    noReadAhead = false\n}}\n\nevents {{\n    maxEventSize = 65536\n    rejectEventsNewerThanSeconds = 900\n    rejectEventsOlderThanSeconds = 94608000\n    rejectEphemeralEventsOlderThanSeconds = 60\n    ephemeralEventsLifetimeSeconds = 300\n    maxNumTags = 2000\n    maxTagValSize = 1024\n}}\n\nrelay {{\n    bind = "0.0.0.0"\n    port = 7777\n    nofiles = 0\n    realIpHeader = "x-real-ip"\n\n    info {{\n        name = "{relay_name}"\n        description = "{relay_description}"\n        pubkey = "{self.startup.nostrPubkey or ''}"\n        contact = ""\n        icon = ""\n        nips = ""\n    }}\n\n    maxWebsocketPayloadSize = 131072\n    maxReqFilterSize = 200\n    autoPingSeconds = 55\n    enableTcpKeepalive = false\n    queryTimesliceBudgetMicroseconds = 10000\n    maxFilterLimit = 500\n    maxSubsPerConnection = 20\n\n    writePolicy {{\n        plugin = {plugin}\n    }}\n\n    compression {{\n        enabled = true\n        slidingWindow = true\n    }}\n\n    logging {{\n        dumpInAll = false\n        dumpInEvents = false\n        dumpInReqs = false\n        dbScanPerf = false\n        invalidEvents = true\n    }}\n\n    numThreads {{\n        ingester = 3\n        reqWorker = 3\n        reqMonitor = 3\n        negentropy = 2\n    }}\n\n    negentropy {{\n        enabled = true\n        maxSyncEvents = 1000000\n    }}\n}}\n'''

    def _render_nginx_conf(self, upstream_host: str) -> str:
        return f'''upstream strfry {{\n    server {upstream_host}:7777;\n}}\n\nmap $http_accept $is_nip11 {{\n    default 0;\n    "application/nostr+json" 1;\n    "~application/nostr\\+json" 1;\n}}\n\nserver {{\n    listen 80;\n\n    location / {{\n        if ($is_nip11) {{\n            rewrite ^ /nip11 last;\n        }}\n\n        proxy_pass http://strfry;\n        proxy_http_version 1.1;\n        proxy_set_header Upgrade $http_upgrade;\n        proxy_set_header Connection "upgrade";\n        proxy_set_header Host $host;\n        proxy_set_header X-Real-IP $remote_addr;\n        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n        proxy_read_timeout 86400s;\n        proxy_send_timeout 86400s;\n    }}\n\n    location = /nip11 {{\n        internal;\n        alias /usr/share/nginx/nip11/nip11.json;\n        default_type application/nostr+json;\n        add_header Access-Control-Allow-Origin *;\n        add_header Access-Control-Allow-Headers "Accept";\n        add_header Cache-Control "max-age=300";\n    }}\n}}\n'''

    def _render_private_policy(self, pubkey: str) -> str:
        return f'''#!/usr/bin/env python3\n\nimport json\nimport subprocess\nimport sys\nfrom typing import Any\n\n\nMY_PUBKEYS = {{\n    "{pubkey.lower()}",\n}}\n\nALLOWED_SOURCE_TYPES = {{\n    "Stream",\n    "Import",\n    "Sync",\n}}\n\nALLOWED_KINDS = {{\n    10002,\n}}\n\n\ndef eprint(message: str) -> None:\n    print(message, file=sys.stderr, flush=True)\n\n\ndef respond(event_id: str, action: str, msg: str | None = None) -> None:\n    payload = {{\n        "id": event_id,\n        "action": action,\n    }}\n    if msg:\n        payload["msg"] = msg\n    print(json.dumps(payload, separators=(",", ":")), file=sys.stdout, flush=True)\n\n\ndef get_d_tag(event: dict[str, Any]) -> str | None:\n    tags = event.get("tags")\n    if not isinstance(tags, list):\n        return None\n    for tag in tags:\n        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "d":\n            return str(tag[1])\n    return None\n\n\ndef is_prediction_event(event: dict[str, Any]) -> bool:\n    d_tag = get_d_tag(event)\n    return bool(d_tag and d_tag.endswith("_pred"))\n\n\ndef source_stream_from_prediction(d_tag: str | None) -> str | None:\n    if not d_tag or not d_tag.endswith("_pred"):\n        return None\n    return d_tag[:-5]\n\n\ndef parse_kind(event: dict[str, Any]) -> int | None:\n    kind = event.get("kind")\n    try:\n        return int(kind)\n    except (TypeError, ValueError):\n        return None\n\n\ndef relay_has_my_source_stream(source_stream: str) -> bool:\n    try:\n        raw = subprocess.check_output(\n            ["/app/strfry", "export"],\n            text=True,\n            stderr=subprocess.DEVNULL,\n        )\n    except Exception as exc:\n        eprint(f"error: failed to query relay db: {{exc}}")\n        return False\n\n    for line in raw.splitlines():\n        line = line.strip()\n        if not line.startswith("{{"):\n            continue\n        try:\n            event = json.loads(line)\n        except json.JSONDecodeError:\n            continue\n\n        event_pubkey = event.get("pubkey")\n        kind = parse_kind(event)\n        d_tag = get_d_tag(event)\n\n        if event_pubkey not in MY_PUBKEYS:\n            continue\n        if d_tag != source_stream:\n            continue\n        if kind not in {{34600, 34601}}:\n            continue\n\n        return True\n\n    return False\n\n\ndef accept(event_id: str, reason: str) -> None:\n    eprint(f"accept: {{reason}}")\n    respond(event_id, "accept")\n\n\ndef reject(event_id: str, reason: str) -> None:\n    eprint(f"reject: {{reason}}")\n    respond(event_id, "reject", reason)\n\n\ndef handle_request(request: dict[str, Any]) -> None:\n    request_type = request.get("type")\n\n    if request_type == "lookback":\n        return\n\n    if request_type != "new":\n        eprint(f"ignore: unexpected request type={{request_type!r}}")\n        return\n\n    event = request.get("event")\n    if not isinstance(event, dict):\n        eprint("reject: malformed request without event object")\n        return\n\n    event_id = event.get("id")\n    if not isinstance(event_id, str) or not event_id:\n        eprint("reject: missing event id")\n        return\n\n    event_pubkey = event.get("pubkey")\n    if not isinstance(event_pubkey, str) or not event_pubkey:\n        reject(event_id, "blocked: missing pubkey")\n        return\n\n    kind = parse_kind(event)\n    d_tag = get_d_tag(event)\n    source_type = request.get("sourceType")\n    source_info = request.get("sourceInfo")\n\n    if event_pubkey in MY_PUBKEYS:\n        accept(event_id, f"own pubkey={{event_pubkey[:12]}}")\n        return\n\n    if kind in ALLOWED_KINDS:\n        accept(event_id, f"allowed kind={{kind}}")\n        return\n\n    if source_type in ALLOWED_SOURCE_TYPES:\n        accept(event_id, f"allowed sourceType={{source_type}}")\n        return\n\n    if is_prediction_event(event):\n        source_stream = source_stream_from_prediction(d_tag)\n        if source_stream and relay_has_my_source_stream(source_stream):\n            accept(\n                event_id,\n                f"foreign prediction for owned source stream pubkey={{event_pubkey[:12]}} source={{source_stream}}",\n            )\n            return\n\n        reject(\n            event_id,\n            (\n                "blocked: foreign prediction does not target an owned source stream "\n                f"(pubkey={{event_pubkey[:12]}}, d={{d_tag}}, source={{source_stream}}, "\n                f"sourceType={{source_type}}, sourceInfo={{source_info}})"\n            ),\n        )\n        return\n\n    reject(\n        event_id,\n        (\n            "blocked: foreign pubkey may not publish non-prediction streams "\n            f"(pubkey={{event_pubkey[:12]}}, kind={{kind}}, d={{d_tag}}, "\n            f"sourceType={{source_type}}, sourceInfo={{source_info}})"\n        ),\n    )\n\n\ndef main() -> None:\n    for line in sys.stdin:\n        line = line.strip()\n        if not line:\n            continue\n\n        try:\n            request = json.loads(line)\n        except json.JSONDecodeError as exc:\n            eprint(f"ignore: invalid json input: {{exc}}")\n            continue\n\n        if not isinstance(request, dict):\n            eprint("ignore: request is not a JSON object")\n            continue\n\n        try:\n            handle_request(request)\n        except Exception as exc:\n            event = request.get("event", {{}})\n            event_id = event.get("id") if isinstance(event, dict) else None\n            eprint(f"error: unexpected exception: {{exc}}")\n            if isinstance(event_id, str) and event_id:\n                reject(event_id, "blocked: internal write policy error")\n\n\nif __name__ == "__main__":\n    main()\n'''
