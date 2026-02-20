from typing import Union, Optional
import os
import time
import json
import asyncio
import threading
import hashlib
import yaml
from satorilib.concepts.structs import StreamId, Stream
from satorilib.concepts import constants
from satorilib.wallet import EvrmoreWallet
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.server import SatoriServerClient
from satorineuron import logging
from satorineuron import config
from satorineuron.init.wallet import WalletManager
from satorineuron.structs.start import RunMode, StartupDagStruct
# from satorilib.utils.ip import getPublicIpv4UsingCurl  # Removed - not needed
from satoriengine.veda.engine import Engine


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(SingletonMeta, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class StartupDag(StartupDagStruct, metaclass=SingletonMeta):
    """a DAG of startup tasks."""


    @classmethod
    def create(
        cls,
        *args,
        env: str = 'prod',
        runMode: str = None,
        isDebug: bool = False,
    ) -> 'StartupDag':
        '''Factory method to create and initialize StartupDag'''
        startupDag = cls(
            *args,
            env=env,
            runMode=runMode,
            isDebug=isDebug)
        startupDag.startFunction()
        return startupDag

    def __init__(
        self,
        *args,
        env: str = 'dev',
        runMode: str = None,
        isDebug: bool = False,
    ):
        super(StartupDag, self).__init__(*args)
        self.env = env
        self.runMode = RunMode.choose(runMode or config.get().get('mode', None))
        self.uiPort = self.getUiPort()
        self.walletManager: WalletManager
        self.isDebug: bool = isDebug
        self.balances: dict = {}
        self.aiengine: Union[Engine, None] = None
        self.publications: list[Stream] = []  # Keep for engine
        self.subscriptions: list[Stream] = []  # Keep for engine
        self.identity: EvrmoreIdentity = EvrmoreIdentity(config.walletPath('wallet.yaml'))
        self.nostrPubkey: Optional[str] = self._initNostrKeys()
        self._networkClients: dict = {}  # relay_url -> SatoriNostr client
        self._networkSubscribed: dict = {}  # relay_url -> set of (stream_name, provider_pubkey)
        self._networkListeners: dict = {}  # relay_url -> asyncio.Task
        self._networkFirstRun: bool = True
        self.networkStreams: list = []  # All discovered streams across relays
        self.networkDB = self._initNetworkDB()
        self.latestObservationTime: float = 0
        self.configRewardAddress: str = None
        self.setupWalletManager()
        # Health check thread: monitors observations and restarts if none received in 24 hours
        self.checkinCheckThread = threading.Thread(
            target=self.checkinCheck,
            daemon=True)
        self.checkinCheckThread.start()
        alreadySetup: bool = os.path.exists(config.walletPath("wallet.yaml"))
        if not alreadySetup:
            threading.Thread(target=self.delayedEngine).start()
        self.ranOnce = False
        self.startFunction = self.start
        if self.runMode == RunMode.normal:
            self.startFunction = self.start
        elif self.runMode == RunMode.worker:
            self.startFunction = self.startWorker
        elif self.runMode == RunMode.wallet:
            self.startFunction = self.startWalletOnly
        if not config.get().get("disable restart", False):
            self.restartThread = threading.Thread(
                target=self.restartEverythingPeriodic,
                daemon=True)
            self.restartThread.start()

    def _initNostrKeys(self) -> Optional[str]:
        """Load or generate Nostr keypair, store in nostr.yaml.

        Returns the 64-char lowercase hex public key, or None on failure.
        """
        nostrPath = config.walletPath('nostr.yaml')
        try:
            if os.path.exists(nostrPath):
                with open(nostrPath, 'r') as f:
                    data = yaml.safe_load(f)
                if data and data.get('pubkey_hex'):
                    pubkey = data['pubkey_hex'].lower()
                    logging.info(f'loaded Nostr pubkey: {pubkey[:16]}...', color='green')
                    return pubkey
            # Generate new keypair
            from nostr_sdk import Keys
            keys = Keys.generate()
            pubkey = keys.public_key().to_hex().lower()
            secret = keys.secret_key().to_hex().lower()
            os.makedirs(os.path.dirname(nostrPath), exist_ok=True)
            with open(nostrPath, 'w') as f:
                yaml.dump({
                    'pubkey_hex': pubkey,
                    'secret_hex': secret,
                }, f, default_flow_style=False)
            logging.info(f'generated Nostr keypair, pubkey: {pubkey[:16]}...', color='green')
            return pubkey
        except Exception as e:
            logging.error(f'failed to init Nostr keys: {e}')
            return None

    def _initNetworkDB(self):
        """Initialize the local network subscriptions database."""
        from satorineuron.network_db import NetworkDB
        db_path = os.path.join(config.dataPath(), 'network.db')
        return NetworkDB(db_path)

    def startNetworkClient(self):
        """Start the network reconciliation thread.

        Reads Nostr secret key, then starts a background thread that
        manages relay connections and stream subscriptions.
        """
        if not self.nostrPubkey:
            logging.info('Network client not started: missing keys', color='yellow')
            return
        nostrPath = config.walletPath('nostr.yaml')
        try:
            with open(nostrPath, 'r') as f:
                data = yaml.safe_load(f)
            secret_hex = data.get('secret_hex', '')
        except Exception as e:
            logging.error(f'Cannot read Nostr secret key: {e}')
            return
        if not secret_hex:
            logging.error('Nostr secret key is empty')
            return
        self._networkSecretHex = secret_hex
        self.networkThread = threading.Thread(
            target=self._runNetworkClient,
            daemon=True)
        self.networkThread.start()

    def _runNetworkClient(self):
        """Background thread entry: runs asyncio event loop with crash recovery."""
        import random
        while True:
            try:
                asyncio.run(self._networkReconcileLoop())
            except Exception as e:
                logging.error(f'Network client thread crashed: {e}')
            delay = random.randint(60, 600)
            logging.info(
                f'Network: restarting in {delay}s', color='yellow')
            time.sleep(delay)

    async def _networkReconcileLoop(self):
        """Reconciliation loop: ensures we are subscribed to all desired streams
        and fetches data sources on their cadence.

        Every 5 minutes:
        1. Reconcile subscriptions (connect, discover, subscribe)
        2. Fetch any data sources that are due
        """
        from satorilib.satori_nostr import SatoriNostr, SatoriNostrConfig

        while True:
            try:
                await self._networkReconcile(SatoriNostrConfig)
            except Exception as e:
                logging.error(f'Network reconcile error: {e}')
            try:
                await self._networkFetchDataSources()
            except Exception as e:
                logging.error(f'Network data source fetch error: {e}')
            await asyncio.sleep(300)

    async def _networkConnect(self, relay_url: str, ConfigClass):
        """Connect to a relay if not already connected. Returns client or None."""
        from satorilib.satori_nostr import SatoriNostr
        if relay_url in self._networkClients:
            return self._networkClients[relay_url]
        try:
            cfg = ConfigClass(
                keys=self._networkSecretHex,
                relay_urls=[relay_url])
            client = SatoriNostr(cfg)
            await client.start()
            self._networkClients[relay_url] = client
            self._networkSubscribed[relay_url] = set()
            logging.info(f'Network: connected to {relay_url}', color='green')
            return client
        except Exception as e:
            logging.warning(f'Network: failed to connect to {relay_url}: {e}')
            return None

    async def _networkDisconnect(self, relay_url: str):
        """Disconnect from a relay and cancel its observation listener."""
        task = self._networkListeners.pop(relay_url, None)
        if task and not task.done():
            task.cancel()
        if relay_url in self._networkClients:
            try:
                await self._networkClients[relay_url].stop()
            except Exception:
                pass
            del self._networkClients[relay_url]
            self._networkSubscribed.pop(relay_url, None)
            logging.info(f'Network: disconnected from {relay_url}', color='yellow')

    async def _networkProcessObservation(self, obs):
        """Save an observation to DB and run engine if predicting.

        This is the single processing path for all observations, whether
        received live from a relay listener or fetched during discovery.
        """
        obs_json = (obs.observation.to_json()
                    if obs.observation else None)
        await asyncio.to_thread(
            self.networkDB.save_observation,
            obs.stream_name,
            obs.nostr_pubkey,
            obs_json,
            obs.event_id,
            obs.observation.seq_num if obs.observation else None,
            obs.observation.timestamp if obs.observation else None)
        # Run engine only if we're predicting this stream
        if obs.observation:
            predicting = await asyncio.to_thread(
                self.networkDB.is_predicting,
                obs.stream_name, obs.nostr_pubkey)
            if predicting:
                await self._networkRunEngine(
                    obs.stream_name,
                    obs.nostr_pubkey,
                    obs.observation)

    async def _networkCheckFreshness(self, client, stream_name, metadata):
        """Check if a stream is actively publishing. Returns (last_obs_time, is_active).

        Also saves the latest observation to the DB if it's new.
        """
        try:
            obs = await client.get_last_observation(stream_name)
            if obs and obs.observation:
                last_obs = obs.observation.timestamp
                await self._networkProcessObservation(obs)
                return last_obs, metadata.is_likely_active(last_obs)
            return None, False
        except Exception:
            return None, False

    async def _networkListen(self, relay_url: str):
        """Listen for observations on a relay and save them to the DB.

        After saving each observation, runs the mock engine to produce
        a prediction (echoes the value) and saves it to the predictions table.
        """
        client = self._networkClients.get(relay_url)
        if not client:
            return
        try:
            async for obs in client.observations():
                await self._networkProcessObservation(obs)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(
                f'Network: listener stopped on {relay_url}: {e}')

    async def _networkRunEngine(self, stream_name: str, provider_pubkey: str,
                                observation):
        """Mock engine: echo the observation value as a prediction.

        Saves to predictions table, then publishes to the network
        on the corresponding _pred publication stream.
        """
        import json
        value = observation.value
        value_str = json.dumps(value) if not isinstance(value, str) else value
        try:
            pred_id = await asyncio.to_thread(
                self.networkDB.save_prediction,
                stream_name,
                provider_pubkey,
                value=value_str,
                observation_seq=observation.seq_num,
                observed_at=observation.timestamp)
            logging.info(
                f'Network: prediction #{pred_id} for {stream_name} '
                f'(echo)', color='cyan')
            # Publish prediction to all connected relays
            pred_stream = stream_name + '_pred'
            await self._networkPublishObservation(pred_stream, value)
            await asyncio.to_thread(
                self.networkDB.mark_prediction_published, pred_id)
        except Exception as e:
            logging.warning(f'Network: prediction failed: {e}')

    def _networkEnsureListener(self, relay_url: str):
        """Start an observation listener for a relay if one isn't running."""
        task = self._networkListeners.get(relay_url)
        if task and not task.done():
            return
        self._networkListeners[relay_url] = asyncio.ensure_future(
            self._networkListen(relay_url))

    async def _networkAnnouncePublications(self, relay_url: str):
        """Announce all our published streams to a relay."""
        from satorilib.satori_nostr.models import DatastreamMetadata
        client = self._networkClients.get(relay_url)
        if not client:
            return
        pubs = await asyncio.to_thread(
            self.networkDB.get_active_publications)
        if not pubs:
            return
        for pub in pubs:
            try:
                source = {}
                if pub.get('source_stream_name'):
                    source['source_stream_name'] = pub['source_stream_name']
                    source['source_provider_pubkey'] = pub.get(
                        'source_provider_pubkey', '')
                metadata = DatastreamMetadata(
                    stream_name=pub['stream_name'],
                    nostr_pubkey=self.nostrPubkey,
                    name=pub.get('name', ''),
                    description=pub.get('description', ''),
                    encrypted=bool(pub.get('encrypted', 0)),
                    price_per_obs=pub.get('price_per_obs', 0),
                    created_at=pub['created_at'],
                    cadence_seconds=pub.get('cadence_seconds'),
                    tags=(pub.get('tags') or '').split(',') if pub.get('tags') else [],
                    metadata=source or None,
                )
                await client.announce_datastream(metadata)
                logging.info(
                    f'Network: announced {pub["stream_name"]} on '
                    f'{relay_url}', color='green')
            except Exception as e:
                logging.warning(
                    f'Network: announce failed {pub["stream_name"]}: {e}')

    async def _networkPublishObservation(self, stream_name: str, value):
        """Publish an observation to all connected relays."""
        from satorilib.satori_nostr.models import (
            DatastreamObservation, DatastreamMetadata)
        pub = await asyncio.to_thread(
            lambda: next(
                (p for p in self.networkDB.get_active_publications()
                 if p['stream_name'] == stream_name), None))
        if not pub:
            logging.warning(
                f'Network: cannot publish {stream_name}: not registered')
            return
        seq_num = await asyncio.to_thread(
            self.networkDB.mark_published, stream_name)
        observation = DatastreamObservation(
            stream_name=stream_name,
            timestamp=int(time.time()),
            value=value,
            seq_num=seq_num)
        source = {}
        if pub.get('source_stream_name'):
            source['source_stream_name'] = pub['source_stream_name']
            source['source_provider_pubkey'] = pub.get(
                'source_provider_pubkey', '')
        metadata = DatastreamMetadata(
            stream_name=stream_name,
            nostr_pubkey=self.nostrPubkey,
            name=pub.get('name', ''),
            description=pub.get('description', ''),
            encrypted=bool(pub.get('encrypted', 0)),
            price_per_obs=pub.get('price_per_obs', 0),
            created_at=pub['created_at'],
            cadence_seconds=pub.get('cadence_seconds'),
            tags=(pub.get('tags') or '').split(',') if pub.get('tags') else [],
            metadata=source or None)
        for relay_url, client in list(self._networkClients.items()):
            try:
                await client.publish_observation(observation, metadata)
                logging.info(
                    f'Network: published {stream_name} seq={seq_num} '
                    f'to {relay_url}', color='green')
            except Exception as e:
                logging.warning(
                    f'Network: publish failed on {relay_url}: {e}')

    def publishObservation(self, stream_name: str, value):
        """Publish an observation from a sync context (e.g. Flask route, engine).

        Broadcasts to all connected relays. Non-blocking.
        """
        if not hasattr(self, '_networkSecretHex'):
            return
        loop = asyncio.new_event_loop()
        def run():
            try:
                loop.run_until_complete(
                    self._networkPublishObservation(stream_name, value))
            finally:
                loop.close()
        threading.Thread(target=run, daemon=True).start()

    async def _networkFetchDataSources(self):
        """Poll active data sources and publish values that are due.

        For each active data source, checks whether enough time has passed
        since the last publish (based on cadence_seconds). If due, fetches the
        URL, runs the parser, and publishes the extracted value.
        """
        import json as json_mod
        import requests as http_requests

        sources = await asyncio.to_thread(
            self.networkDB.get_active_data_sources)
        if not sources:
            return

        # Build a lookup of publications by stream_name for last_published_at
        pubs = await asyncio.to_thread(
            self.networkDB.get_active_publications)
        pub_map = {p['stream_name']: p for p in pubs}

        now = int(time.time())

        for src in sources:
            stream_name = src['stream_name']
            cadence = src['cadence_seconds']
            # Skip externally-fed sources (no URL or no cadence)
            if not src.get('url') or not cadence:
                continue
            pub = pub_map.get(stream_name)
            if not pub:
                continue

            last = pub.get('last_published_at') or 0
            if now - last < cadence:
                continue  # not due yet

            # Fetch
            try:
                url = src['url']
                method = src.get('method', 'GET').upper()
                headers = None
                if src.get('headers'):
                    try:
                        headers = json_mod.loads(src['headers'])
                    except Exception:
                        headers = None

                if method == 'POST':
                    resp = await asyncio.to_thread(
                        lambda: http_requests.post(
                            url, headers=headers, timeout=15))
                else:
                    resp = await asyncio.to_thread(
                        lambda: http_requests.get(
                            url, headers=headers, timeout=15))
                resp.raise_for_status()
                raw = resp.text
            except Exception as e:
                logging.warning(
                    f'Network: fetch failed for {stream_name}: {e}')
                continue

            # Parse
            try:
                parser_type = src.get('parser_type', 'json_path')
                parser_config = src.get('parser_config', '')

                if parser_type == 'json_path':
                    obj = json_mod.loads(raw)
                    for key in parser_config.split('.'):
                        if key.isdigit():
                            obj = obj[int(key)]
                        else:
                            obj = obj[key]
                    value = str(obj)
                elif parser_type == 'python':
                    local_vars = {'text': raw}
                    exec_code = parser_config.strip()
                    if ('return ' in exec_code
                            and not exec_code.startswith('def ')):
                        exec_code = (
                            'def _parse(text):\n' +
                            '\n'.join(
                                '    ' + l
                                for l in exec_code.split('\n')) +
                            '\n_result = _parse(text)')
                        exec(exec_code, {}, local_vars)
                        value = str(local_vars.get('_result', ''))
                    else:
                        exec(exec_code, {}, local_vars)
                        value = str(local_vars.get(
                            'result', local_vars.get('_result', '')))
                else:
                    logging.warning(
                        f'Network: unknown parser type '
                        f'{parser_type} for {stream_name}')
                    continue
            except Exception as e:
                logging.warning(
                    f'Network: parse failed for {stream_name}: {e}')
                continue

            # Publish
            try:
                await self._networkPublishObservation(stream_name, value)
                logging.info(
                    f'Network: data source {stream_name} published',
                    color='green')
            except Exception as e:
                logging.warning(
                    f'Network: publish failed for {stream_name}: {e}')

    async def _networkDiscover(self, ConfigClass):
        """On-demand discovery: connect to all relays, find all streams.

        Called from the API when the user loads the streams page.
        Not part of the reconciliation loop.
        """
        try:
            relays = await asyncio.to_thread(self.server.getRelays)
            relay_urls = [r['relay_url'] for r in relays]
        except Exception as e:
            logging.warning(f'Network discover: could not fetch relay list: {e}')
            relay_urls = list(self._neededRelays())
            if not relay_urls:
                return
            logging.info(
                f'Network discover: falling back to {len(relay_urls)} '
                f'known relays', color='yellow')

        all_streams = []
        for relay_url in relay_urls:
            client = await self._networkConnect(relay_url, ConfigClass)
            if not client:
                continue
            try:
                streams = await client.discover_datastreams()
                for s in streams:
                    d = s.to_dict()
                    d['relay_url'] = relay_url
                    last_obs, is_active = await self._networkCheckFreshness(
                        client, s.stream_name, s)
                    d['last_observation_at'] = last_obs
                    d['active'] = is_active
                    all_streams.append(d)
            except Exception as e:
                logging.warning(
                    f'Network discover: failed on {relay_url}: {e}')
            # Disconnect if we only connected for discovery
            if relay_url not in self._neededRelays():
                await self._networkDisconnect(relay_url)

        self.networkStreams = all_streams

    async def _networkDiscoverRelay(self, relay_url: str, ConfigClass):
        """Discover streams on a single relay. Returns list of stream dicts."""
        client = await self._networkConnect(relay_url, ConfigClass)
        if not client:
            return []
        result = []
        try:
            streams = await client.discover_datastreams()
            for s in streams:
                d = s.to_dict()
                d['relay_url'] = relay_url
                last_obs, is_active = await self._networkCheckFreshness(
                    client, s.stream_name, s)
                d['last_observation_at'] = last_obs
                d['active'] = is_active
                result.append(d)
        except Exception as e:
            logging.warning(
                f'Network discover: failed on {relay_url}: {e}')
        if relay_url not in self._neededRelays():
            await self._networkDisconnect(relay_url)
        return result

    def discoverRelaySync(self, relay_url: str) -> list:
        """Discover streams on a single relay from a sync context.

        Blocks until discovery completes and returns the stream list.
        """
        from satorilib.satori_nostr import SatoriNostrConfig
        if not hasattr(self, '_networkSecretHex'):
            return []
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._networkDiscoverRelay(relay_url, SatoriNostrConfig))
        finally:
            loop.close()

    def _neededRelays(self) -> set:
        """Return set of relay URLs that have active subscriptions."""
        subs = self.networkDB.get_active()
        return {s['relay_url'] for s in subs}

    def triggerNetworkDiscover(self):
        """Trigger on-demand discovery from a sync context (e.g. Flask route)."""
        from satorilib.satori_nostr import SatoriNostrConfig
        if not hasattr(self, '_networkSecretHex'):
            return
        loop = asyncio.new_event_loop()
        def run():
            try:
                loop.run_until_complete(
                    self._networkDiscover(SatoriNostrConfig))
            finally:
                loop.close()
        threading.Thread(target=run, daemon=True).start()

    async def _networkReconcile(self, ConfigClass):
        """Single reconciliation pass.

        Every 5 minutes:
        1. Get subscriptions from DB
        2. Check which are inactive (no observation within 1.5 * cadence)
        3. For inactive ones not recently marked stale: hunt relays
        4. If not found anywhere: mark stale
        """
        # 1. Get subscriptions
        desired = await asyncio.to_thread(self.networkDB.get_active)
        if not desired:
            return

        # 2. Find inactive subscriptions
        #    On first run, treat all as inactive to establish connections
        if self._networkFirstRun:
            inactive = list(desired)
            self._networkFirstRun = False
        else:
            inactive = []
            for sub in desired:
                cadence = sub.get('cadence_seconds')
                is_stale = await asyncio.to_thread(
                    self.networkDB.is_locally_stale,
                    sub['stream_name'], sub['provider_pubkey'], cadence)
                if is_stale:
                    inactive.append(sub)

        if not inactive:
            return

        # 3. Build hunt list: inactive subs not recently marked stale
        hunting = {}  # stream_name -> sub dict
        for sub in inactive:
            stale_since = sub.get('stale_since')
            if stale_since and not self.networkDB.should_recheck_stale(
                    stale_since):
                continue
            hunting[sub['stream_name']] = sub

        if not hunting:
            return

        # Get relay list from central, fall back to known relays from DB
        try:
            relays = await asyncio.to_thread(self.server.getRelays)
            relay_urls = [r['relay_url'] for r in relays]
        except Exception as e:
            logging.warning(f'Could not fetch relay list from central: {e}')
            relay_urls = list({sub['relay_url'] for sub in desired})
            if not relay_urls:
                return
            logging.info(
                f'Network: falling back to {len(relay_urls)} known relays',
                color='yellow')

        # 4. Hunt relay by relay — check all wanted streams per relay
        for relay_url in relay_urls:
            if not hunting:
                break  # all found
            client = await self._networkConnect(relay_url, ConfigClass)
            if not client:
                continue
            try:
                streams = await client.discover_datastreams()
            except Exception:
                await self._networkDisconnect(relay_url)
                continue

            # Index this relay's streams by name
            relay_index = {s.stream_name: s for s in streams}

            # Check which of our wanted streams are on this relay and active
            found_any = False
            for stream_name in list(hunting.keys()):
                metadata = relay_index.get(stream_name)
                if not metadata:
                    continue
                _, is_active = await self._networkCheckFreshness(
                    client, stream_name, metadata)
                if not is_active:
                    continue
                # Found active — update DB, subscribe
                sub = hunting.pop(stream_name)
                found_any = True
                await asyncio.to_thread(
                    self.networkDB.update_relay,
                    stream_name, sub['provider_pubkey'], relay_url)
                try:
                    await client.subscribe_datastream(
                        stream_name, sub['provider_pubkey'])
                    logging.info(
                        f'Network: found {stream_name} active on '
                        f'{relay_url}', color='green')
                except Exception as e:
                    logging.warning(
                        f'Network: subscribe failed {stream_name}: {e}')

            if found_any:
                # Start listening for observations on this relay
                self._networkEnsureListener(relay_url)
                # Announce our publications to this relay
                await self._networkAnnouncePublications(relay_url)
            else:
                # Disconnect if this relay had nothing we needed
                await self._networkDisconnect(relay_url)

        # 5. Whatever's left in hunting wasn't found anywhere — mark stale
        for stream_name, sub in hunting.items():
            await asyncio.to_thread(
                self.networkDB.mark_stale,
                stream_name, sub['provider_pubkey'])
            logging.info(
                f'Network: {stream_name} stale everywhere, '
                f'recheck in 24h', color='yellow')

    @staticmethod
    def getUiPort() -> int:
        """Get UI port with priority: config file > environment variable > default (24601)"""
        existing_port = config.get().get('uiport')
        if existing_port is not None:
            return int(existing_port)
        else:
            port = int(os.environ.get('SATORI_UI_PORT', '24601'))
            config.add(data={'uiport': port})
            return port

    @property
    def walletOnlyMode(self) -> bool:
        return self.runMode == RunMode.wallet

    @property
    def rewardAddress(self) -> str:
        return self.configRewardAddress

    @property
    def network(self) -> str:
        return 'main' if self.env in ['prod', 'local', 'testprod'] else 'test'

    @property
    def vault(self) -> EvrmoreWallet:
        return self.walletManager.vault

    @property
    def wallet(self) -> EvrmoreWallet:
        return self.walletManager.wallet

    @property
    def holdingBalance(self) -> float:
        if self.wallet.balance.amount > 0:
            self._holdingBalance = round(
                self.wallet.balance.amount
                + (self.vault.balance.amount if self.vault is not None else 0),
                8)
        else:
            self._holdingBalance = self.getBalance()
        return self._holdingBalance

    def refreshBalance(self, threaded: bool = True, forWallet: bool = True, forVault: bool = True):
        self.walletManager.connect()
        if forWallet and isinstance(self.wallet, EvrmoreWallet):
            if threaded:
                threading.Thread(target=self.wallet.get).start()
            else:
                self.wallet.get()
        if forVault and isinstance(self.vault, EvrmoreWallet):
            if threaded:
                threading.Thread(target=self.vault.get).start()
            else:
                self.vault.get()
        return self.holdingBalance

    def refreshUnspents(self, threaded: bool = True, forWallet: bool = True, forVault: bool = True):
        self.walletManager.connect()
        if forWallet and isinstance(self.wallet, EvrmoreWallet):
            if threaded:
                threading.Thread(target=self.wallet.getReadyToSend).start()
            else:
                self.wallet.getReadyToSend()
        if forVault and isinstance(self.vault, EvrmoreWallet):
            if threaded:
                threading.Thread(target=self.vault.getReadyToSend).start()
            else:
                self.vault.getReadyToSend()
        return self._holdingBalance

    @property
    def holdingBalanceBase(self) -> float:
        """Get Satori from Base with 5-minute interval cache"""
        # TEMPORARY DISABLE
        return 0

    @property
    def ethaddressforward(self) -> str:
        eth_address = self.vault.ethAddress
        if eth_address:
            return eth_address
        else:
            return ""

    def getVaultInfoFromFile(self) -> dict:
        """Read vault info (address and pubkey) from vault.yaml without decrypting.

        The address and pubkey are stored unencrypted in vault.yaml, so we can read them
        even when the vault is locked.

        Returns:
            dict: {'address': str, 'pubkey': str} or empty dict if file doesn't exist
        """
        try:
            import yaml
            vault_path = config.walletPath('vault.yaml')
            if not os.path.exists(vault_path):
                return {}

            with open(vault_path, 'r') as f:
                vault_data = yaml.safe_load(f)

            result = {}
            if vault_data:
                # Address is under evr: section
                if 'evr' in vault_data and 'address' in vault_data['evr']:
                    result['address'] = vault_data['evr']['address']
                # publicKey is at top level
                if 'publicKey' in vault_data:
                    result['pubkey'] = vault_data['publicKey']

            return result
        except Exception as e:
            logging.warning(f"Could not read vault info from file: {e}")
            return {}

    def setupWalletManager(self):
        # Never auto-decrypt the global vault - it should remain encrypted
        self.walletManager = WalletManager.create(useConfigPassword=False)

    def shutdownWallets(self):
        self.walletManager._electrumx = None
        self.walletManager._wallet = None
        self.walletManager._vault = None

    def closeVault(self):
        self.walletManager.closeVault()

    def openVault(self, password: Union[str, None] = None, create: bool = False):
        return self.walletManager.openVault(password=password, create=create)

    def getWallet(self, **kwargs):
        return self.walletManager.wallet

    def getVault(self, password: Union[str, None] = None, create: bool = False) -> Union[EvrmoreWallet, None]:
        return self.walletManager.openVault(password=password, create=create)

    def electrumxCheck(self):
        return self.walletManager.isConnected()

    def collectAndSubmitPredictions(self):
        """Collect predictions from all models and submit in batch."""
        try:
            if not hasattr(self, 'aiengine') or self.aiengine is None:
                logging.warning("AI Engine not initialized, skipping prediction collection", color='yellow')
                return

            # Collect predictions from all models
            predictions_collected = 0
            for stream_uuid, model in self.aiengine.streamModels.items():
                if hasattr(model, '_pending_prediction') and model._pending_prediction:
                    # Queue prediction in engine
                    pred = model._pending_prediction
                    self.aiengine.queuePrediction(
                        stream_uuid=pred['stream_uuid'],
                        stream_name=pred['stream_name'],
                        value=pred['value'],
                        observed_at=pred['observed_at'],
                        hash_val=pred['hash']
                    )
                    predictions_collected += 1
                    # Clear the pending prediction
                    model._pending_prediction = None

            if predictions_collected > 0:
                logging.info(f"Collected {predictions_collected} predictions from models", color='cyan')
                # Submit all queued predictions in batch
                result = self.aiengine.flushPredictionQueue()
                if result:
                    logging.info(f"✓ Batch predictions submitted: {result['successful']}/{result['total_submitted']}", color='green')
                else:
                    logging.warning("Failed to submit batch predictions", color='yellow')
            else:
                logging.debug("No predictions ready to submit")

        except Exception as e:
            logging.error(f"Error collecting and submitting predictions: {e}", color='red')

    def logTrainingQueueStatus(self):
        """Log training queue statistics for monitoring."""
        try:
            if self.aiengine is None:
                return

            # Import the queue manager getter
            from satoriengine.veda.training.queue_manager import get_training_manager

            manager = get_training_manager()
            status = manager.get_queue_status()

            if status['worker_alive']:
                if status['current']:
                    logging.info(
                        f"Training Queue: {status['queued']} waiting, "
                        f"currently training: {status['current']}",
                        color='cyan')
                else:
                    logging.info(
                        f"Training Queue: {status['queued']} waiting, worker idle",
                        color='cyan')
            else:
                logging.warning("Training queue worker is not running!", color='yellow')

        except Exception as e:
            logging.error(f"Error logging training queue status: {e}", color='red')

    def pollObservationsForever(self):
        """
        Poll the central server for new observations.
        Initial delay: random (0-11 hours) to distribute load
        Subsequent polls: every 11 hours
        """
        import pandas as pd
        import random

        def pollForever():
            # First poll: random delay between 5 and 30 minutes
            initial_delay = random.randint(60 * 5, 60 * 30)
            logging.info(f"First observation poll in {initial_delay / 60:.1f} minutes", color='blue')
            time.sleep(initial_delay)

            # Subsequent polls: every 11 hours
            while True:
                try:
                    if not hasattr(self, 'server') or self.server is None:
                        logging.warning("Server not initialized, skipping observation poll", color='yellow')
                        time.sleep(60 * 60 * 11)
                        continue

                    if not hasattr(self, 'aiengine') or self.aiengine is None:
                        logging.warning("AI Engine not initialized, skipping observation poll", color='yellow')
                        time.sleep(60 * 60 * 11)
                        continue

                    # Get latest batch of observations from central-lite
                    # This includes Bitcoin, multi-crypto, and SafeTrade observations
                    storage = getattr(self.aiengine, 'storage', None)
                    observations = self.server.getObservationsBatch(storage=storage)

                    if observations is None or len(observations) == 0:
                        logging.info("No new observations available", color='blue')
                        time.sleep(60 * 60 * 11)
                        continue

                    logging.info(f"Received {len(observations)} observations from server", color='cyan')

                    # Update last observation time
                    self.latestObservationTime = time.time()

                    # Process each observation
                    observations_processed = 0
                    for observation in observations:
                        try:
                            # Extract values
                            value = observation.get('value')
                            hash_val = observation.get('hash') or observation.get('id')
                            stream_uuid = observation.get('stream_uuid')
                            stream = observation.get('stream')
                            stream_name = stream.get('name', 'unknown') if stream else 'unknown'

                            if value is None:
                                logging.warning(f"Skipping observation with no value (stream: {stream_name})", color='yellow')
                                continue

                            # Convert observation to DataFrame for engine
                            df = pd.DataFrame([{
                                'ts': observation.get('observed_at') or observation.get('ts'),
                                'value': float(value),
                                'hash': str(hash_val) if hash_val is not None else None,
                            }])

                            # Store using server-provided stream UUID
                            if stream_uuid:
                                observations_processed += 1

                                # Create stream model if it doesn't exist
                                if stream_uuid not in self.aiengine.streamModels:
                                    try:
                                        # Import required classes
                                        from satoriengine.veda.engine import StreamModel

                                        # Create StreamId objects for subscription and publication
                                        sub_id = StreamId(
                                            source='central-lite',
                                            author='satori',
                                            stream=stream_name,
                                            target=''
                                        )

                                        # Prediction stream uses "_pred" suffix
                                        pub_id = StreamId(
                                            source='central-lite',
                                            author='satori',
                                            stream=f"{stream_name}_pred",
                                            target=''
                                        )

                                        # Create Stream objects
                                        subscriptionStream = Stream(streamId=sub_id)
                                        publicationStream = Stream(streamId=pub_id, predicting=sub_id)

                                        # Create StreamModel using factory method
                                        self.aiengine.streamModels[stream_uuid] = StreamModel.createFromServer(
                                            streamUuid=stream_uuid,
                                            predictionStreamUuid=pub_id.uuid,
                                            server=self.server,
                                            wallet=self.wallet,
                                            subscriptionStream=subscriptionStream,
                                            publicationStream=publicationStream,
                                            pauseAll=self.aiengine.pause,
                                            resumeAll=self.aiengine.resume,
                                            storage=self.aiengine.storage
                                        )

                                        # Choose and initialize appropriate adapter
                                        self.aiengine.streamModels[stream_uuid].chooseAdapter(inplace=True)

                                        # Start training thread for this stream
                                        try:
                                            self.aiengine.streamModels[stream_uuid].run_forever()
                                        except Exception as e:
                                            logging.error(f"Failed to start training thread for {stream_name}: {e}", color='red')
                                    except Exception as e:
                                        logging.error(f"Failed to create model for {stream_name}: {e}", color='red')
                                        import traceback
                                        logging.error(traceback.format_exc())

                                # Pass data to the model
                                if stream_uuid in self.aiengine.streamModels:
                                    try:
                                        self.aiengine.streamModels[stream_uuid].onDataReceived(df)
                                        logging.info(f"✓ Stored {stream_name}: ${float(value):,.2f} (UUID: {stream_uuid[:8]}...)", color='green')
                                    except Exception as e:
                                        logging.error(f"Error passing to engine for {stream_name}: {e}", color='red')
                            else:
                                logging.warning(f"Observation for {stream_name} missing stream_uuid", color='yellow')

                        except Exception as e:
                            logging.error(f"Error processing individual observation: {e}", color='red')

                    logging.info(f"✓ Processed and stored {observations_processed}/{len(observations)} observations", color='cyan')

                    # After processing all observations, collect predictions and submit in batch
                    self.collectAndSubmitPredictions()

                    # Log training queue status
                    self.logTrainingQueueStatus()

                except Exception as e:
                    logging.error(f"Error polling observations: {e}", color='red')

                # Wait 11 hours before next poll
                time.sleep(60 * 60 * 11)

        self.pollObservationsThread = threading.Thread(
            target=pollForever,
            daemon=True)
        self.pollObservationsThread.start()

    def delayedEngine(self):
        time.sleep(60 * 60 * 6)
        self.buildEngine()

    def checkinCheck(self):
        while True:
            time.sleep(60 * 60 * 6)  # Check every 6 hours
            current_time = time.time()
            if self.latestObservationTime and (current_time - self.latestObservationTime > 60*60*24):
                logging.warning("No observations in 24 hours, restarting", print=True)
                self.triggerRestart()
            if hasattr(self, 'server') and hasattr(self.server, 'checkinCheck') and self.server.checkinCheck():
                logging.warning("Server check failed, restarting", print=True)
                self.triggerRestart()

    def networkIsTest(self, network: str = None) -> bool:
        return network.lower().strip() in ("testnet", "test", "ravencoin", "rvn")

    def start(self):
        """start the satori engine."""
        if self.ranOnce:
            time.sleep(60 * 60)
        self.ranOnce = True
        if self.env == 'prod' and self.serverConnectedRecently():
            last_checkin = config.get().get('server checkin')
            elapsed_minutes = (time.time() - last_checkin) / 60
            wait_minutes = max(0, 10 - elapsed_minutes)
            if wait_minutes > 0:
                logging.info(f"Server connected recently, waiting {wait_minutes:.1f} minutes")
                time.sleep(wait_minutes * 60)
        self.recordServerConnection()
        if self.walletOnlyMode:
            self.createServerConn()
            self.authWithCentral()
            self.setRewardAddress(globally=True)  # Sync reward address with server
            logging.info("in WALLETONLYMODE")
            startWebUI(self, port=self.uiPort)  # Start web UI after sync
            return
        self.setMiningMode()
        self.createServerConn()
        self.authWithCentral()
        self.setRewardAddress(globally=True)  # Sync reward address with server
        self.startNetworkClient()
        self.setupDefaultStream()
        self.spawnEngine()
        startWebUI(self, port=self.uiPort)  # Start web UI after sync

    def startWalletOnly(self):
        """start the satori engine."""
        logging.info("running in walletOnly mode", color="blue")
        self.createServerConn()
        return

    def startWorker(self):
        """start the satori engine."""
        logging.info("running in worker mode", color="blue")
        if self.env == 'prod' and self.serverConnectedRecently():
            last_checkin = config.get().get('server checkin')
            elapsed_minutes = (time.time() - last_checkin) / 60
            wait_minutes = max(0, 10 - elapsed_minutes)
            if wait_minutes > 0:
                logging.info(f"Server connected recently, waiting {wait_minutes:.1f} minutes")
                time.sleep(wait_minutes * 60)
        self.recordServerConnection()
        self.setMiningMode()
        self.createServerConn()
        self.authWithCentral()
        self.setRewardAddress(globally=True)  # Sync reward address with server
        self.startNetworkClient()
        self.setupDefaultStream()
        self.spawnEngine()
        startWebUI(self, port=self.uiPort)  # Start web UI after sync
        threading.Event().wait()

    def serverConnectedRecently(self, threshold_minutes: int = 10) -> bool:
        """Check if server was connected to recently without side effects."""
        last_checkin = config.get().get('server checkin')
        if last_checkin is None:
            return False
        elapsed_seconds = time.time() - last_checkin
        return elapsed_seconds < (threshold_minutes * 60)

    def recordServerConnection(self) -> None:
        """Record the current time as the last server connection time."""
        config.add(data={'server checkin': time.time()})

    def createServerConn(self):
        # logging.debug(self.urlServer, color="teal")
        self.server = SatoriServerClient(self.wallet)

    def authWithCentral(self):
        """Register peer with central-lite server."""
        x = 30
        attempt = 0
        while True:
            attempt += 1
            try:
                # Get vault info from vault.yaml (available even when encrypted)
                vault_info = self.getVaultInfoFromFile()

                # Build vaultInfo dict for registration
                vaultInfo = None
                if vault_info.get('address') or vault_info.get('pubkey'):
                    vaultInfo = {
                        'vaultaddress': vault_info.get('address'),
                        'vaultpubkey': vault_info.get('pubkey')
                    }

                # Register peer with central server
                self.server.checkin(
                    vaultInfo=vaultInfo,
                    nostrPubkey=self.nostrPubkey)

                logging.info("authenticated with central-lite", color="green")
                break
            except Exception as e:
                logging.warning(f"connecting to central err: {e}")
            x = x * 1.5 if x < 60 * 60 * 6 else 60 * 60 * 6
            logging.warning(f"trying again in {x}")
            time.sleep(x)

    def getBalance(self, currency: str = 'currency') -> float:
        return self.balances.get(currency, 0)

    def setRewardAddress(
        self,
        address: Union[str, None] = None,
        globally: bool = False
    ) -> bool:
        """
        Set or sync reward address between local config and central server.

        Args:
            address: Reward address to set. If None, loads from config or syncs from server.
            globally: If True, also syncs with central server (requires production env).

        Returns:
            True if successfully set/synced, False otherwise.
        """
        # If address is provided, validate and save to config
        if EvrmoreWallet.addressIsValid(address):
            self.configRewardAddress = address
            config.add(data={'reward address': address})

            # If globally=True, check if server needs update
            if globally and self.env in ['prod', 'local', 'testprod', 'dev']:
                try:
                    serverAddress = self.server.mineToAddressStatus()
                    # Only send to server if addresses differ
                    if address != serverAddress:
                        self.server.setRewardAddress(address=address)
                        logging.info(f"Updated server reward address: {address[:8]}...", color="green")
                except Exception as e:
                    logging.debug(f"Could not sync reward address with server: {e}")
            return True
        else:
            # No address provided - load from config
            self.configRewardAddress: str = str(config.get().get('reward address', ''))

            # If we need to sync with server, check if addresses match
            if (
                hasattr(self, 'server') and
                self.server is not None and
                self.env in ['prod', 'local', 'testprod', 'dev']
            ):
                try:
                    serverAddress = self.server.mineToAddressStatus()

                    # If config is empty but server has address, fetch and save
                    if not self.configRewardAddress and serverAddress and EvrmoreWallet.addressIsValid(serverAddress):
                        self.configRewardAddress = serverAddress
                        config.add(data={'reward address': serverAddress})
                        logging.info(f"Synced reward address from server: {serverAddress[:8]}...", color="green")
                        return True

                    # If config has address and globally=True, check if server needs update
                    if (
                        globally and
                        EvrmoreWallet.addressIsValid(self.configRewardAddress) and
                        self.configRewardAddress != serverAddress
                    ):
                        # Only send to server if addresses differ
                        self.server.setRewardAddress(address=self.configRewardAddress)
                        logging.info(f"Updated server reward address: {self.configRewardAddress[:8]}...", color="green")
                        return True

                except Exception as e:
                    logging.debug(f"Could not sync reward address with server: {e}")

        return False

    @staticmethod
    def predictionStreams(streams: list[Stream]):
        """filter down to prediciton publications"""
        return [s for s in streams if s.predicting is not None]

    @staticmethod
    def oracleStreams(streams: list[Stream]):
        """filter down to prediciton publications"""
        return [s for s in streams if s.predicting is None]

    def removePair(self, pub: StreamId, sub: StreamId):
        self.publications = [p for p in self.publications if p.streamId != pub]
        self.subscriptions = [s for s in self.subscriptions if s.streamId != sub]

    def addToEngine(self, stream: Stream, publication: Stream):
        if self.aiengine is not None:
            self.aiengine.addStream(stream, publication)

    def getMatchingStream(self, streamId: StreamId) -> Union[StreamId, None]:
        for stream in self.publications:
            if stream.streamId == streamId:
                return stream.predicting
            if stream.predicting == streamId:
                return stream.streamId
        return None

    def setupDefaultStream(self):
        """Setup hard-coded default stream for central-lite.

        Central-lite has a single observation stream, so we create one
        subscription/publication pair for the engine to work with.
        """
        # Create subscription stream (input observations)
        sub_id = StreamId(
            source="central-lite",
            author="satori",
            stream="observations",
            target=""
        )
        subscription = Stream(streamId=sub_id)

        # Create publication stream (output predictions)
        pub_id = StreamId(
            source="central-lite",
            author="satori",
            stream="predictions",
            target=""
        )
        publication = Stream(streamId=pub_id, predicting=sub_id)

        # Assign to neuron
        self.subscriptions = [subscription]
        self.publications = [publication]

        # Suppress log for default stream to reduce noise
        # logging.info(f"Default stream configured: {sub_id.uuid}", color="green")

    def spawnEngine(self):
        """Spawn the AI Engine with stream assignments from Neuron"""
        if not self.subscriptions or not self.publications:
            logging.warning("No stream assignments available, skipping Engine spawn")
            return

        # logging.info("Spawning AI Engine...", color="blue")
        try:
            self.aiengine = Engine.createFromNeuron(
                subscriptions=self.subscriptions,
                publications=self.publications,
                server=self.server,
                wallet=self.wallet)

            def runEngine():
                try:
                    self.aiengine.initializeFromNeuron()

                    # Start training threads for initial stream models only
                    # Additional models will be created dynamically when observations arrive
                    for stream_uuid, model in self.aiengine.streamModels.items():
                        try:
                            model.run_forever()
                        except Exception as e:
                            logging.error(f"Failed to start training thread for {stream_uuid}: {e}")

                    logging.info("Models will be created dynamically when observations arrive", color="cyan")

                    # Keep engine thread alive
                    while True:
                        time.sleep(60)
                except Exception as e:
                    logging.error(f"Engine error: {e}")

            engineThread = threading.Thread(target=runEngine, daemon=True)
            engineThread.start()

            # Start polling for observations from central-lite
            self.pollObservationsForever()

            logging.info("AI Engine spawned successfully", color="green")
        except Exception as e:
            logging.error(f"Failed to spawn AI Engine: {e}")

    def delayedStart(self):
        alreadySetup: bool = os.path.exists(config.walletPath("wallet.yaml"))
        if alreadySetup:
            threading.Thread(target=self.delayedEngine).start()

    def triggerRestart(self, return_code=1):
        os._exit(return_code)

    def emergencyRestart(self):
        import time
        logging.warning("restarting in 10 minutes", print=True)
        time.sleep(60 * 10)
        self.triggerRestart()

    def restartEverythingPeriodic(self):
        import random
        restartTime = time.time() + config.get().get(
            "restartTime", random.randint(60 * 60 * 21, 60 * 60 * 24)
        )
        while True:
            if time.time() > restartTime:
                self.triggerRestart()
            time.sleep(random.randint(60 * 60, 60 * 60 * 4))

    def performStakeCheck(self):
        self.stakeStatus = self.server.stakeCheck()
        return self.stakeStatus

    def setMiningMode(self, miningMode: Union[bool, None] = None):
        miningMode = (
            miningMode
            if isinstance(miningMode, bool)
            else config.get().get('mining mode', True))
        self.miningMode = miningMode
        config.add(data={'mining mode': self.miningMode})
        if hasattr(self, 'server') and self.server is not None:
            self.server.setMiningMode(miningMode)
        return self.miningMode

    # Removed setInvitedBy - central-lite doesn't use referrer system

    def poolAccepting(self, status: bool):
        success, result = self.server.poolAccepting(status)
        if success:
            self.poolIsAccepting = status
        return success, result

    @property
    def stakeRequired(self) -> float:
        return constants.stakeRequired


def startWebUI(startupDag: StartupDag, host: str = '0.0.0.0', port: int = 24601):
    """Start the Flask web UI in a background thread."""
    try:
        from web.app import create_app
        from web.routes import set_vault, set_startup

        app = create_app()

        # Connect vault and startup to web routes
        set_vault(startupDag.walletManager)
        set_startup(startupDag)  # Set startup immediately - initialization is complete

        def run_flask():
            # Suppress Flask/werkzeug logging
            import logging as stdlib_logging
            werkzeug_logger = stdlib_logging.getLogger('werkzeug')
            werkzeug_logger.setLevel(stdlib_logging.ERROR)
            # Use werkzeug server (not for production, but fine for local use)
            app.run(host=host, port=port, debug=False, use_reloader=False)

        web_thread = threading.Thread(target=run_flask, daemon=True)
        web_thread.start()
        logging.info(f"Web UI started at http://{host}:{port}", color="green")
        return web_thread
    except ImportError as e:
        logging.warning(f"Web UI not available: {e}")
        return None
    except Exception as e:
        logging.error(f"Failed to start Web UI: {e}")
        return None


def getStart() -> Union[StartupDag, None]:
    """Get the singleton instance of StartupDag.

    Returns:
        The singleton StartupDag instance if it exists, None otherwise.
    """
    return StartupDag._instances.get(StartupDag, None)


if __name__ == "__main__":
    logging.info("Starting Satori Neuron", color="green")

    # Web UI will be started after initialization completes
    # (called from start() or startWorker() methods after reward address sync)
    startup = StartupDag.create(env=os.environ.get('SATORI_ENV', 'prod'), runMode='worker')

    threading.Event().wait()
