from typing import Union, Optional
import math
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
from satorineuron import VERSION
from satorineuron.relay_manager import LocalRelayManager
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
        self.localRelay = LocalRelayManager(self)
        self._networkClients: dict = {}  # relay_url -> SatoriNostr client
        self._networkSubscribed: dict = {}  # relay_url -> set of (stream_name, provider_pubkey)
        self._networkListeners: dict = {}  # relay_url -> asyncio.Task
        self._channelListeners: dict = {}  # relay_url -> asyncio.Task
        self._channelOpenListeners: dict = {}  # relay_url -> asyncio.Task
        self._channelSettlementListeners: dict = {}  # relay_url -> asyncio.Task
        self._channelTombstoneListeners: dict = {}  # relay_url -> asyncio.Task
        self._settledChannels: set = set()  # p2sh addresses settled this session (race guard)
        self._paymentCooldowns: dict = {}  # (stream, provider) -> last payment timestamp
        self._paymentDeferred: dict = {}   # (stream, provider) -> asyncio.TimerHandle
        self._channelPayLocks: dict = {}   # p2sh_address -> asyncio.Lock (Fix H)
        self._mundoCache: dict = {}        # p2sh_address -> {signed_hex, incomplete_hex, ...} (Fix J)
        self._dataSourceTasks: dict = {}  # stream_name -> (asyncio.Task, cadence)
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
            crashed = False
            try:
                asyncio.run(self._networkReconcileLoop())
            except Exception as e:
                logging.error(f'Network client thread crashed: {e}')
                crashed = True
            if crashed:
                delay = random.randint(60, 600)
                logging.info(
                    f'Network: restarting in {delay}s', color='yellow')
                time.sleep(delay)

    async def _networkReconcileLoop(self):
        """Reconciliation loop: ensures we are subscribed to all desired streams.

        Every hour:
        1. Reconcile subscriptions (connect, discover, subscribe)
        2. Ensure relay connections exist for active publications
        3. Restore subscriber access
        4. Check channel expiries

        Each data source gets its own asyncio task (managed by
        _networkDataSourceManager) that fires at exactly its cadence.
        """
        from satorilib.satori_nostr import SatoriNostr, SatoriNostrConfig

        # Store the running event loop so sync callers can submit coroutines
        # to it safely via asyncio.run_coroutine_threadsafe().
        self._networkLoop = asyncio.get_running_loop()

        # Clear any stale SatoriNostr clients that were bound to a previous
        # (now-dead) event loop.  Keeping them would cause cross-loop
        # contamination and silent crashes on every await.
        self._networkClients.clear()
        self._networkListeners.clear()
        self._channelListeners.clear()
        self._channelOpenListeners.clear()
        self._channelSettlementListeners.clear()
        self._channelTombstoneListeners.clear()
        self._settledChannels.clear()
        self._networkSubscribed.clear()
        for _sn, (task, _cad) in list(self._dataSourceTasks.items()):
            task.cancel()
        self._dataSourceTasks.clear()
        self._networkFirstRun = True

        fetch_task = None
        try:
            while True:
                try:
                    await self._networkReconcile(SatoriNostrConfig)
                except Exception as e:
                    logging.error(f'Network reconcile error: {e}')
                try:
                    await self._networkEnsurePublisherConnections(SatoriNostrConfig)
                except Exception as e:
                    logging.error(f'Network publisher connect error: {e}')
                try:
                    await self._networkRestoreSubscriberAccess()
                except Exception as e:
                    logging.error(f'Network subscriber access restore error: {e}')
                # Start the data source manager after the first reconcile
                # cycle so publisher relay connections exist before the
                # first fetch attempts to publish.
                if fetch_task is None or fetch_task.done():
                    fetch_task = asyncio.create_task(
                        self._networkDataSourceManager())
                try:
                    await self._channelExpiryCheck()
                except Exception as e:
                    logging.error(f'Channel expiry check error: {e}')
                await asyncio.sleep(3600)
        finally:
            if fetch_task is not None and not fetch_task.done():
                fetch_task.cancel()
                try:
                    await fetch_task
                except BaseException:
                    pass
            for _sn, (task, _cad) in list(self._dataSourceTasks.items()):
                task.cancel()
            self._dataSourceTasks.clear()

    async def _networkEnsurePublisherConnections(self, ConfigClass):
        """Connect to all known relays if we have active publications.

        Ensures _networkClients is populated for the fetch loop even when
        there are no active subscriptions keeping connections open.
        """
        pubs = await asyncio.to_thread(self.networkDB.get_active_publications)
        if not pubs:
            return
        # Sync relay URLs from the server into the DB so we always publish
        # to the canonical relay (avoids stale wss:// vs ws:// mismatches).
        try:
            server_relays = await asyncio.to_thread(self.server.getRelays)
            for r in server_relays:
                await asyncio.to_thread(
                    self.networkDB.upsert_relay, r['relay_url'])
        except Exception:
            pass
        relays = await asyncio.to_thread(self.networkDB.get_relays)
        for r in relays:
            relay_url = r['relay_url']
            if relay_url not in self._networkClients:
                client = await self._networkConnect(relay_url, ConfigClass)
                if client:
                    await self._networkAnnouncePublications(relay_url)

    async def _networkRestoreSubscriberAccess(self):
        """Restore persisted subscriber access into connected clients.

        On provider restart, _subscribers in each SatoriNostr client is empty.
        This reloads the subscriber_access table and populates every connected
        client so that subscribers who already paid don't lose their access
        rights while waiting for a new subscription announcement.

        Called every reconcile cycle but is idempotent — record_payment only
        advances last_paid_seq, never regresses it.
        """
        records = await asyncio.to_thread(
            self.networkDB.load_subscriber_access)
        if not records:
            return
        restored = 0
        for rec in records:
            stream_name = rec['stream_name']
            nostr_pubkey = rec['nostr_pubkey']
            last_paid_seq = rec['last_paid_seq']
            if not nostr_pubkey or last_paid_seq <= 0:
                continue
            for client in self._networkClients.values():
                if nostr_pubkey not in client._subscribers.get(
                        stream_name, {}):
                    client.record_subscription(stream_name, nostr_pubkey)
                client.record_payment(stream_name, nostr_pubkey, last_paid_seq)
            restored += 1
        if restored:
            logging.info(
                f'Channel: restored {restored} subscriber access records '
                f'from DB',
                color='cyan')

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
            # Start channel-related listeners immediately on connect — they
            # must run for pure publishers (no stream subscriptions) too, so
            # senders can receive settlement/tombstone notifications from
            # receivers who claim their channels.
            self._networkEnsureChannelListener(relay_url)
            self._networkEnsureChannelOpenListener(relay_url)
            self._networkEnsureSettlementListener(relay_url)
            self._networkEnsureTombstoneListener(relay_url)
            asyncio.ensure_future(
                self._channelPublishStaleTombstones(relay_url))
            return client
        except Exception as e:
            logging.warning(f'Network: failed to connect to {relay_url}: {e}')
            return None

    async def _channelPublishStaleTombstones(self, relay_url: str) -> None:
        """On reconnect, tombstone relay commitments whose UTXO is outdated.

        If a prior session claimed a channel but was killed before publishing
        the tombstone, the relay retains the stale commitment event. We detect
        this by comparing each stored pending_commitment's UTXO against the
        channel's current funding_txid (which was updated by the claim). A
        mismatch means the commitment is stale: clear it from the DB and publish
        a tombstone so the relay drops it and won't replay it again.
        """
        client = self._networkClients.get(relay_url)
        if not client:
            return
        try:
            channels = await asyncio.to_thread(
                self.networkDB.get_channels_as_receiver)
            for channel in channels:
                prior_json = channel.get('pending_commitment')
                if not prior_json:
                    continue
                try:
                    from satorilib.satori_nostr.models import ChannelCommitment
                    prior = ChannelCommitment.from_json(prior_json)
                    raw = bytes.fromhex(prior.partial_tx_hex)
                    commitment_txid = raw[5:37][::-1].hex()
                    if commitment_txid == channel['funding_txid']:
                        continue
                    p2sh = channel['p2sh_address']
                    logging.warning(
                        f'Channel: stale commitment detected for {p2sh} on '
                        f'reconnect — clearing DB and tombstoning relay '
                        f'({commitment_txid[:12]}… != {channel["funding_txid"][:12]}…)',
                        color='yellow')
                    await asyncio.to_thread(
                        self.networkDB.clear_pending_commitment, p2sh)
                    await client.remove_commitment(p2sh)
                except Exception as e:
                    logging.warning(
                        f'Channel: stale tombstone check failed for '
                        f'{channel.get("p2sh_address", "?")}: {e}')
        except Exception as e:
            logging.warning(
                f'Channel: stale tombstone scan failed on {relay_url}: {e}')

    async def _networkDisconnect(self, relay_url: str):
        """Disconnect from a relay and cancel its listeners."""
        task = self._networkListeners.pop(relay_url, None)
        if task and not task.done():
            task.cancel()
        ctask = self._channelListeners.pop(relay_url, None)
        if ctask and not ctask.done():
            ctask.cancel()
        otask = self._channelOpenListeners.pop(relay_url, None)
        if otask and not otask.done():
            otask.cancel()
        stask = self._channelSettlementListeners.pop(relay_url, None)
        if stask and not stask.done():
            stask.cancel()
        ttask = self._channelTombstoneListeners.pop(relay_url, None)
        if ttask and not ttask.done():
            ttask.cancel()
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
        is_new = await asyncio.to_thread(
            self.networkDB.save_observation,
            obs.stream_name,
            obs.nostr_pubkey,
            obs_json,
            obs.event_id,
            obs.observation.seq_num if obs.observation else None,
            obs.observation.timestamp if obs.observation else None)
        # Run engine only if this is a new observation (not a duplicate)
        if is_new and obs.observation:
            predicting = await asyncio.to_thread(
                self.networkDB.is_predicting,
                obs.stream_name, obs.nostr_pubkey)
            if predicting:
                await self._networkRunEngine(
                    obs.stream_name,
                    obs.nostr_pubkey,
                    obs.observation)
            # Pay for this observation if the stream has a price and we have
            # an open channel to this provider
            await self._channelPayForObservation(
                obs.stream_name, obs.nostr_pubkey, obs.observation.seq_num)

    async def _channelPayForObservation(
        self,
        stream_name: str,
        provider_pubkey: str,
        seq_num: int,
    ) -> None:
        """Pay for an observation via a channel (sender/buyer side).

        Fix F — state-based trigger: fires a payment iff the subscriber's
        persisted last_paid_seq is behind the incoming seq_num. This avoids
        both over-paying on bursts and under-paying on replays/stale data.
        The cooldown stays as a rate-limiter inside, not the entrance gate.
        """
        try:
            conn_rows = await asyncio.to_thread(self.networkDB.get_active)
            subscription = next(
                (s for s in conn_rows
                 if s['stream_name'] == stream_name
                 and s['provider_pubkey'] == provider_pubkey),
                None)
            if not subscription or subscription.get('price_per_obs', 0) == 0:
                return  # free stream
            # State gate: only pay if we're behind
            last_paid = subscription.get('last_paid_seq', 0) or 0
            if seq_num <= last_paid:
                return
            price_sats = subscription['price_per_obs']
            cadence = subscription.get('cadence_seconds') or 0
            cooldown = cadence / 2 if cadence > 0 else 0
            key = (stream_name, provider_pubkey)
            now = time.time()
            last_paid_time = self._paymentCooldowns.get(key, 0)
            if cooldown > 0 and (now - last_paid_time) < cooldown:
                # Inside cooldown — schedule one deferred payment at cooldown end
                if key not in self._paymentDeferred:
                    delay = cooldown - (now - last_paid_time)
                    loop = asyncio.get_event_loop()
                    self._paymentDeferred[key] = loop.call_later(
                        delay,
                        lambda k=key, s=stream_name, p=provider_pubkey:
                            asyncio.ensure_future(
                                self._channelPayDeferred(s, p)))
                    logging.debug(
                        f'Channel: deferred payment for {stream_name} '
                        f'in {delay:.1f}s (cooldown)')
                return
            await self._channelPayNow(stream_name, provider_pubkey, price_sats)
            # Advance last_paid_seq so we don't double-pay
            await asyncio.to_thread(
                self.networkDB.update_last_paid_seq,
                stream_name, provider_pubkey, seq_num)
        except Exception as e:
            logging.warning(f'Channel: pay-for-observation failed: {e}')

    async def _channelPayDeferred(
        self,
        stream_name: str,
        provider_pubkey: str,
    ) -> None:
        """Execute a deferred payment scheduled during cooldown."""
        key = (stream_name, provider_pubkey)
        self._paymentDeferred.pop(key, None)
        try:
            conn_rows = await asyncio.to_thread(self.networkDB.get_active)
            subscription = next(
                (s for s in conn_rows
                 if s['stream_name'] == stream_name
                 and s['provider_pubkey'] == provider_pubkey),
                None)
            if not subscription or subscription.get('price_per_obs', 0) == 0:
                return
            price_sats = subscription['price_per_obs']
            await self._channelPayNow(stream_name, provider_pubkey, price_sats)
            # Advance last_paid_seq to the highest observation we've received
            max_seq = await asyncio.to_thread(
                self.networkDB.max_observation_seq,
                stream_name, provider_pubkey)
            if max_seq > 0:
                await asyncio.to_thread(
                    self.networkDB.update_last_paid_seq,
                    stream_name, provider_pubkey, max_seq)
        except Exception as e:
            logging.warning(f'Channel: deferred payment failed: {e}')

    async def _channelPayNow(
        self,
        stream_name: str,
        provider_pubkey: str,
        price_sats: int,
    ) -> None:
        """Send a channel payment immediately and reset the cooldown timer.

        Handles channel lookup, refund if exhausted, and open if none exists.
        Fix H: the read-build-write is serialized per channel with an
        asyncio.Lock so two concurrent observations don't produce duplicate
        commitments referencing the same prior state.
        """
        await self._channelEnsureWallet()
        conn_rows = await asyncio.to_thread(self.networkDB.get_active)
        subscription = next(
            (s for s in conn_rows
             if s['stream_name'] == stream_name
             and s['provider_pubkey'] == provider_pubkey),
            None)
        provider_wallet_pubkey = (
            subscription.get('provider_wallet_pubkey') if subscription else None)
        if not provider_wallet_pubkey:
            return
        channels = await asyncio.to_thread(
            self.networkDB.get_channels_as_sender)
        channel = next(
            (c for c in channels
             if c['receiver_pubkey'] == provider_wallet_pubkey),
            None)
        # Acquire a per-channel lock for the remainder read → build → write
        p2sh = channel['p2sh_address'] if channel else None
        if p2sh and p2sh not in self._channelPayLocks:
            self._channelPayLocks[p2sh] = asyncio.Lock()
        lock = self._channelPayLocks.get(p2sh) if p2sh else None
        if lock:
            await lock.acquire()
        try:
            # Re-read channel inside the lock so we see the latest remainder
            if channel:
                channel = await asyncio.to_thread(
                    self.networkDB.get_channel, channel['p2sh_address'])
            if channel and channel['remainder_sats'] < price_sats:
                fund_sats = self._channelFundSats(price_sats)
                logging.info(
                    f'Channel: refunding {channel["p2sh_address"]} '
                    f'(remainder={channel["remainder_sats"]}) '
                    f'with {fund_sats} sats',
                    color='cyan')
                await self.refundChannel(channel['p2sh_address'], fund_sats)
                channel = await asyncio.to_thread(
                    self.networkDB.get_channel, channel['p2sh_address'])
                if not channel or channel['remainder_sats'] < price_sats:
                    return
            elif not channel:
                fund_sats = self._channelFundSats(price_sats)
                timeout_minutes = self._channelTimeoutMinutes()
                logging.info(
                    f'Channel: auto-opening to {provider_wallet_pubkey[:16]}… '
                    f'fund={fund_sats} sats timeout={timeout_minutes} min',
                    color='cyan')
                new_p2sh = await self.openChannel(
                    receiver_pubkey=provider_wallet_pubkey,
                    amount_sats=fund_sats,
                    minutes=timeout_minutes,
                    receiver_nostr_pubkey=provider_pubkey,
                )
                channel = await asyncio.to_thread(
                    self.networkDB.get_channel, new_p2sh)
                if not channel or channel['remainder_sats'] < price_sats:
                    return
            await self.sendChannelPayment(
                channel['p2sh_address'], price_sats, stream_name)
            key = (stream_name, provider_pubkey)
            self._paymentCooldowns[key] = time.time()
        finally:
            if lock:
                lock.release()

    async def _networkCheckFreshness(self, client, stream_name, metadata):
        """Check if a stream is actively publishing. Returns (last_obs_time, is_active).

        Also saves the latest observation to the DB if it's new (only when
        the observation content can be decrypted, i.e. for free streams or
        paid streams to which we are a paying subscriber).

        For paid streams we are NOT a subscriber to, decryption fails — but
        we can still infer freshness from the public event header timestamp.
        """
        # First try a full parse. Successful for free streams; also gives us
        # the observation to cache for streams we're a subscriber to.
        try:
            obs = await client.get_last_observation(stream_name)
            if obs and obs.observation:
                last_obs = obs.observation.timestamp
                await self._networkProcessObservation(obs)
                return last_obs, metadata.is_likely_active(last_obs)
        except Exception:
            pass
        # Fallback: read just the event header timestamp. Works for paid
        # streams as long as the publisher has at least one paying subscriber
        # — the relay still holds the (encrypted) event whose created_at we
        # can read without any key.
        try:
            ts = await client.get_last_observation_event_time(stream_name)
            if ts:
                return ts, metadata.is_likely_active(ts)
        except Exception:
            pass
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
                subscribed = await asyncio.to_thread(
                    self.networkDB.is_subscribed,
                    obs.stream_name, obs.nostr_pubkey)
                if not subscribed:
                    continue
                logging.info(
                    f'Network: received data from {relay_url} '
                    f'stream={obs.stream_name} value={obs.observation.value if obs.observation else None}',
                    color='cyan')
                await self._networkProcessObservation(obs)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(
                f'Network: listener stopped on {relay_url}: {e}')

    async def _networkRunEngine(self, stream_name: str, provider_pubkey: str,
                                observation):
        """Lite engine: predict from recent observations, echo fallback.

        Saves to predictions table, then publishes to the network
        on the corresponding _pred publication stream.
        """
        import json
        from satorineuron.lite_engine import LiteEngine

        # Fetch recent observations and run lite prediction
        observations = await asyncio.to_thread(
            self.networkDB.get_observations,
            stream_name, provider_pubkey, limit=30)
        prediction = LiteEngine().predict(observations)

        if prediction is not None:
            value_str = prediction
            method = 'lite'
        else:
            # Fallback to echo for non-numeric data
            value = observation.value
            value_str = json.dumps(value) if not isinstance(
                value, str) else value
            method = 'echo'

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
                f'({method})', color='cyan')
            # Publish prediction to all connected relays
            pred_stream = stream_name + '_pred'
            await self._networkPublishObservation(pred_stream, value_str)
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
        self._networkEnsureChannelListener(relay_url)
        self._networkEnsureChannelOpenListener(relay_url)
        self._networkEnsureSettlementListener(relay_url)
        self._networkEnsureTombstoneListener(relay_url)

    # ── Channel support ───────────────────────────────────────────────────────

    def _networkEnsureChannelListener(self, relay_url: str):
        """Start a channel commitment listener for a relay if one isn't running."""
        task = self._channelListeners.get(relay_url)
        if task and not task.done():
            return
        self._channelListeners[relay_url] = asyncio.ensure_future(
            self._channelListen(relay_url))

    def _networkEnsureChannelOpenListener(self, relay_url: str):
        """Start a channel open announcement listener for a relay if one isn't running."""
        task = self._channelOpenListeners.get(relay_url)
        if task and not task.done():
            return
        self._channelOpenListeners[relay_url] = asyncio.ensure_future(
            self._channelOpenListen(relay_url))

    def _networkEnsureSettlementListener(self, relay_url: str):
        """Start a channel settlement listener for a relay if one isn't running."""
        task = self._channelSettlementListeners.get(relay_url)
        if task and not task.done():
            return
        self._channelSettlementListeners[relay_url] = asyncio.ensure_future(
            self._channelSettleListen(relay_url))

    async def _channelSettleListen(self, relay_url: str):
        """Listen for settlement notifications on channels we opened (sender side)."""
        client = self._networkClients.get(relay_url)
        if not client:
            return
        logging.info(
            f'Channel: settlement listener started on {relay_url}', color='cyan')
        try:
            async for inbound in client.settlements():
                logging.info(
                    f'Channel: received settlement on {relay_url} '
                    f'p2sh={inbound.settlement.p2sh_address} '
                    f'new_vout={inbound.settlement.new_funding_vout}',
                    color='cyan')
                await self._channelHandleSettlement(inbound)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(
                f'Channel: settlement listener stopped on {relay_url}: {e}')

    async def _channelHandleSettlement(self, inbound):
        """Handle a settlement notification: update DB with new funding UTXO (sender side)."""
        s = inbound.settlement
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, s.p2sh_address)
        if not channel or not channel.get('is_sender'):
            return
        # Mark as settled so the tombstone fallback doesn't double-reset
        self._settledChannels.add(s.p2sh_address)
        if s.new_funding_vout == -1 or s.new_locked_sats == 0:
            # Channel fully drained — remainder is already 0 from sender's
            # micropayment tracking; next observation will trigger refund.
            logging.info(
                f'Channel: {s.p2sh_address} fully drained, awaiting refund',
                color='yellow')
        else:
            # Update to new UTXO; cumulative tracking resets to 0.
            # Use the settlement's timestamp so the sender's CSV-timer
            # anchor matches the receiver's (Nostr delivery can be delayed).
            await asyncio.to_thread(
                self.networkDB.update_channel_funding,
                s.p2sh_address,
                s.claim_txid,
                s.new_funding_vout,
                s.new_locked_sats,
                int(getattr(s, 'timestamp', 0)) or None)
            logging.info(
                f'Channel: {s.p2sh_address} settled, new UTXO '
                f'{s.claim_txid[:12]}…:{s.new_funding_vout} '
                f'({s.new_locked_sats} sats)',
                color='green')

    def _networkEnsureTombstoneListener(self, relay_url: str):
        """Start a tombstone listener for a relay if one isn't running."""
        task = self._channelTombstoneListeners.get(relay_url)
        if task and not task.done():
            return
        self._channelTombstoneListeners[relay_url] = asyncio.ensure_future(
            self._channelTombstoneListen(relay_url))

    async def _channelTombstoneListen(self, relay_url: str):
        """Listen for commitment tombstones as a fallback reset (sender side).

        KIND_CHANNEL_SETTLED is the primary mechanism. This handles the case
        where that event is not received (network issue, offline sender, etc.)
        by using the tombstone the receiver always publishes after claiming.
        """
        client = self._networkClients.get(relay_url)
        if not client:
            return
        try:
            async for p2sh in client.tombstones():
                await self._channelHandleTombstone(p2sh)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(
                f'Channel: tombstone listener stopped on {relay_url}: {e}')

    async def _channelHandleTombstone(self, p2sh_address: str):
        """Fallback reset for sender when a tombstone arrives.

        If KIND_CHANNEL_SETTLED was already received and processed, the channel
        is in _settledChannels and we skip. Otherwise this is the safety net:
        Alice claimed (spending the UTXO) but we missed the settlement, so
        our funding_txid is stale. Zero remainder_sats so the next observation
        triggers a refund instead of building txs against a spent UTXO.

        Note: do NOT discard the _settledChannels marker here — multiple
        tombstones arrive (one per relay) for the same claim, and popping the
        marker after the first would cause subsequent tombstones to wrongly
        zero out a channel the settlement already refreshed.
        """
        if p2sh_address in self._settledChannels:
            return  # settlement already handled this properly
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, p2sh_address)
        if not channel or not channel.get('is_sender'):
            return
        await asyncio.to_thread(
            self.networkDB.update_channel_remainder, p2sh_address, 0)
        logging.info(
            f'Channel: {p2sh_address} tombstone fallback — '
            f'remainder zeroed, next observation will refund',
            color='yellow')

    async def _channelOpenListen(self, relay_url: str):
        """Listen for inbound channel open announcements on a relay.

        When a sender opens a channel to us, they publish a KIND_CHANNEL_OPEN
        event that we receive here. We save the channel to our DB so we can
        process future commitments from that sender.
        """
        client = self._networkClients.get(relay_url)
        if not client:
            return
        try:
            async for inbound in client.channel_opens():
                await self._channelHandleOpen(inbound)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(
                f'Channel: open listener stopped on {relay_url}: {e}')

    async def _channelHandleOpen(self, inbound):
        """Save an incoming channel open announcement to the DB (receiver side).

        Only saves if the receiver_pubkey in the announcement matches our own
        wallet pubkey. Silently ignores announcements intended for others.
        """
        co = inbound.channel_open
        if co.receiver_pubkey != self.wallet.pubkey:
            return
        existing = await asyncio.to_thread(
            self.networkDB.get_channel, co.p2sh_address)
        # Ignore replays: same P2SH + same funding_txid means we already have it.
        # A different funding_txid is the authoritative reset signal (Fix D) —
        # the sender refunded/reopened with a new UTXO. Accept unconditionally;
        # the on-chain UTXO change is proof that this is a real state transition
        # regardless of timestamp ordering (clock skew, relay delivery latency,
        # or missing timestamps can all make the old timestamp check unreliable).
        if existing:
            if existing.get('funding_txid') == co.funding_txid:
                return
        await asyncio.to_thread(
            self.networkDB.save_channel,
            co.p2sh_address,
            co.sender_pubkey,
            co.receiver_pubkey,
            co.redeem_script,
            co.funding_txid,
            co.funding_vout,
            co.locked_sats,
            co.locked_sats,  # remainder = locked at time of open
            False,            # is_sender = False
            co.blocks,
            co.minutes,
            sender_nostr_pubkey=co.sender_nostr_pubkey,
            receiver_nostr_pubkey=self.nostrPubkey or '',
            created_at=int(getattr(co, 'timestamp', 0)) or None,
        )
        # If the funding UTXO changed (refund/reopen), clear the stale
        # pending commitment from the previous funding round — otherwise the
        # next legitimate commitment looks like a remainder-rollback attack.
        if existing and existing.get('funding_txid') != co.funding_txid:
            await asyncio.to_thread(
                self.networkDB.clear_pending_commitment, co.p2sh_address)
        logging.info(
            f'Channel: {"updated" if existing else "registered inbound"} '
            f'channel {co.p2sh_address} from {co.sender_pubkey[:16]}… '
            f'txid={co.funding_txid[:12]}…',
            color='green')

    async def _channelListen(self, relay_url: str):
        """Listen for inbound channel commitments on a relay.

        When the buyer publishes a partial tx addressed to us, we receive it
        here, sign it, and broadcast — completing the payment.
        """
        client = self._networkClients.get(relay_url)
        if not client:
            return
        try:
            async for inbound in client.commitments():
                logging.info(
                    f'Channel: received commitment on {relay_url} '
                    f'p2sh={inbound.commitment.p2sh_address} '
                    f'pay={inbound.commitment.pay_amount_sats} sats',
                    color='cyan')
                await self._channelProcessCommitment(inbound.commitment)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(f'Channel: listener stopped on {relay_url}: {e}')

    async def _channelProcessCommitment(self, commitment):
        """Store an inbound channel commitment for later claiming (receiver side).

        Called when the buyer has published a half-signed transaction to Nostr.
        We store the latest commitment in the DB. The receiver claims manually
        (or automatically 24 h before channel expiry) to pay a single fee for
        all accumulated micropayments.
        """
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, commitment.p2sh_address)
        if not channel:
            # Channel open event may still be in-flight — retry once after a
            # short delay before dropping (open and commit arrive simultaneously)
            await asyncio.sleep(3)
            channel = await asyncio.to_thread(
                self.networkDB.get_channel, commitment.p2sh_address)
        if not channel:
            logging.warning(
                f'Channel: commitment for unknown channel '
                f'{commitment.p2sh_address} — ignoring')
            return
        # Only the receiver should process inbound commitments.
        # The sender also knows about the channel (it opened it) and sees the
        # same commitment events on the relay — skip if we're the sender.
        if channel.get('receiver_pubkey') != self.wallet.pubkey:
            return
        # Fix I: require stream_name to prevent cross-stream over-granting
        if not commitment.stream_name:
            logging.warning(
                f'Channel: rejecting commitment for {commitment.p2sh_address} — '
                f'missing stream_name',
                color='yellow')
            return
        # Bind the commitment envelope to the channel's known pubkeys. Nostr
        # kind 34604 is parameterized-replaceable by (kind, author, d_tag), so
        # any publisher can emit an event with d=p2sh_address; we must verify
        # the payload claims the right sender/receiver before accepting it,
        # otherwise a spoofed commitment can DoS the real pending state.
        if commitment.sender_pubkey != channel['sender_pubkey']:
            logging.warning(
                f'Channel: rejecting commitment for {commitment.p2sh_address} — '
                f'sender_pubkey mismatch (got {commitment.sender_pubkey[:16]}…, '
                f'expected {channel["sender_pubkey"][:16]}…)',
                color='yellow')
            return
        if commitment.receiver_pubkey != channel['receiver_pubkey']:
            logging.warning(
                f'Channel: rejecting commitment for {commitment.p2sh_address} — '
                f'receiver_pubkey mismatch',
                color='yellow')
            return
        # Verify the commitment references the current funding UTXO. After a
        # successful claim the DB funding_txid advances to the claim tx output;
        # a relay-replayed commitment still pointing at the old UTXO is stale.
        try:
            raw = bytes.fromhex(commitment.partial_tx_hex)
            # version (4 bytes) + input count varint (1 byte) + txid LE (32 bytes)
            commitment_txid = raw[5:37][::-1].hex()
            if commitment_txid != channel['funding_txid']:
                logging.warning(
                    f'Channel: rejecting stale commitment for '
                    f'{commitment.p2sh_address} — references old UTXO '
                    f'{commitment_txid[:12]}… (current: '
                    f'{channel["funding_txid"][:12]}…); tombstoning relay',
                    color='yellow')
                for client in self._networkClients.values():
                    try:
                        await client.remove_commitment(commitment.p2sh_address)
                    except Exception:
                        pass
                return
        except Exception as e:
            logging.warning(
                f'Channel: could not verify commitment UTXO for '
                f'{commitment.p2sh_address}: {e} — accepting')
        # Bounds + monotonicity. remainder_sats tracks what's still locked in
        # the channel; each new commitment must not exceed what was originally
        # locked, and must not *increase* vs the prior pending commitment (that
        # would roll back a payment the receiver already accepted).
        locked = int(channel.get('locked_sats') or 0)
        if (commitment.remainder_sats < 0
                or commitment.remainder_sats > locked
                or commitment.pay_amount_sats < 0):
            logging.warning(
                f'Channel: rejecting commitment for {commitment.p2sh_address} — '
                f'out-of-range amounts (pay={commitment.pay_amount_sats}, '
                f'remainder={commitment.remainder_sats}, locked={locked})',
                color='yellow')
            return
        prior_json = channel.get('pending_commitment')
        if prior_json:
            try:
                from satorilib.satori_nostr.models import ChannelCommitment
                prior = ChannelCommitment.from_json(prior_json)
                if commitment.remainder_sats > prior.remainder_sats:
                    logging.warning(
                        f'Channel: rejecting commitment for '
                        f'{commitment.p2sh_address} — remainder increased '
                        f'({prior.remainder_sats} → {commitment.remainder_sats}), '
                        f'which would roll back an accepted payment',
                        color='yellow')
                    return
            except Exception as e:
                logging.warning(
                    f'Channel: could not parse prior commitment for '
                    f'{commitment.p2sh_address}: {e} — accepting new one')
        # Cryptographically verify the sender's signature on the partial tx.
        # Without this, a Nostr publisher who knows the sender's public EVR
        # pubkey could forge a commitment (e.g. remainder=0) that passes the
        # monotonicity check and overwrites the real pending commitment —
        # broadcast would later fail, but the legit state is already lost.
        if not self._channelVerifySenderSig(commitment, channel):
            logging.warning(
                f'Channel: rejecting commitment for {commitment.p2sh_address} — '
                f'sender signature failed to verify against stored pubkey',
                color='red')
            return
        try:
            commitment_json = commitment.to_json()
            await asyncio.to_thread(
                self.networkDB.store_pending_commitment,
                commitment.p2sh_address,
                commitment_json)
            logging.info(
                f'Channel: stored commitment {commitment.p2sh_address} '
                f'pay={commitment.pay_amount_sats} sats '
                f'remainder={commitment.remainder_sats} sats',
                color='cyan')
        except Exception as e:
            logging.error(
                f'Channel: failed to store commitment '
                f'{commitment.p2sh_address}: {e}')
        # Grant provisional access immediately on commitment receipt so the
        # subscriber can receive observations without waiting for an on-chain
        # claim. The claim just settles funds; access is based on committed amt.
        sender_nostr_pubkey = channel.get('sender_nostr_pubkey', '')
        if sender_nostr_pubkey and commitment.pay_amount_sats > 0:
            await self._grantChannelAccess(
                sender_nostr_pubkey=sender_nostr_pubkey,
                total_paid_sats=commitment.pay_amount_sats,
                stream_name=commitment.stream_name,
                p2sh_address=commitment.p2sh_address)

    async def claimChannel(self, p2sh_address: str) -> str:
        """Claim accumulated micropayments from a channel (receiver side).

        Signs and broadcasts the latest pending commitment using 3-path fee logic:
        PATH A — fee already embedded in the partial tx → broadcast directly.
        PATH B — fee deficit but receiver has EVR → add EVR input, sign, broadcast.
        PATH C — no EVR at all → pay SATORI fee to Mundo, Mundo adds EVR.

        Updates the channel DB with the new funding UTXO so the sender can
        continue paying. Publishes a KIND_CHANNEL_SETTLED notification.

        Args:
            p2sh_address: The channel to claim from

        Returns:
            The broadcast txid
        """
        from satorilib.satori_nostr.models import ChannelCommitment, ChannelSettlement
        await self._channelEnsureWallet()
        # Refresh UTXOs so PATH B can see our EVR for the mining fee
        await asyncio.to_thread(self.wallet.getUnspents)
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, p2sh_address)
        if not channel:
            raise ValueError(f'Unknown channel: {p2sh_address}')
        expires_at = self._channelExpiresAt(channel)
        if expires_at and int(time.time()) >= expires_at:
            raise ValueError(
                f'Channel {p2sh_address} has expired — the sender can now '
                f'reclaim on-chain, so claiming here would race that reclaim '
                f'and is not safe. Claim before the timeout next time.')
        commitment_json = channel.get('pending_commitment')
        if not commitment_json:
            raise ValueError(f'No pending commitment for channel: {p2sh_address}')
        commitment = ChannelCommitment.from_json(commitment_json)
        from evrmore.core import (
            CMutableTransaction, CMutableTxIn, CMutableTxOut, COutPoint, lx, Hash160)
        from evrmore.core.script import (
            CScript, SIGHASH_ALL, SIGHASH_ANYONECANPAY,
            OP_DUP, OP_HASH160, OP_EQUALVERIFY, OP_CHECKSIG)
        from evrmore.wallet import CEvrmoreAddress
        from satorilib.wallet.utils.transaction import TxUtils
        from satorilib.wallet.evrmore.scripts.channels import unlock
        from functools import partial as funcpartial
        tx = CMutableTransaction.deserialize(
            bytes.fromhex(commitment.partial_tx_hex))
        # Ensure all sub-objects are mutable (deserialize may yield immutable)
        tx.vin = [CMutableTxIn(v.prevout, v.scriptSig, v.nSequence)
                   for v in tx.vin]
        tx.vout = [CMutableTxOut(v.nValue, v.scriptPubKey)
                    for v in tx.vout]
        redeem_script = CScript(bytes.fromhex(channel['redeem_script']))
        sender_sigs = [bytes.fromhex(s) for s in commitment.sender_sigs]
        # ── Pre-compute change_vout from partial_tx (independent of claim path) ──
        # Done here so DB can be updated immediately after broadcast, before any
        # Nostr ops — minimises the crash window between broadcast and DB update.
        redeem_bytes = bytes.fromhex(channel['redeem_script'])
        script_hash = hashlib.new(
            'ripemd160', hashlib.sha256(redeem_bytes).digest()).digest()
        p2sh_script = bytes([0xa9, 0x14]) + script_hash + bytes([0x87])
        change_vout = -1
        change_sats = commitment.remainder_sats
        for i, out in enumerate(tx.vout):
            spk = bytes(out.scriptPubKey)
            if spk == p2sh_script or spk.startswith(p2sh_script):
                change_vout = i
                break
        # ── Fee estimation ──────────────────────────────────────────────────
        # Size of the serialised partial tx (P2SH vin has empty scriptSig yet)
        # plus the space the completed 2-of-2+CSV scriptSig will occupy.
        P2SH_SCRIPTSIG_SIZE = 265   # 2-of-2 + CSV redeemScript + sigs
        EVR_INPUT_SIZE = 148        # P2PKH input
        EVR_CHANGE_SIZE = 34        # P2PKH change output
        partial_size = len(bytes.fromhex(commitment.partial_tx_hex))
        estimated_size = partial_size + P2SH_SCRIPTSIG_SIZE
        existing_fee = commitment.fee   # 0 for EVR-less partial txs
        required_fee_a = math.ceil(estimated_size * TxUtils.feeRate)
        deficit = required_fee_a - existing_fee

        def _make_redeem_params(sig):
            return funcpartial(
                unlock.paymentChannel,
                sender_sig=sender_sigs[0],
                receiver_sig=sig)

        def _check_broadcast(result):
            from satorilib.wallet.concepts.transaction import TransactionFailure
            if isinstance(result, dict) and result.get('code') is not None:
                raise TransactionFailure(
                    f'broadcast rejected: {result.get("message", result)}')
            return result

        if deficit <= 0:
            # ── PATH A: fee already embedded ────────────────────────────────
            our_sig = await asyncio.to_thread(
                self.wallet.paymentChannelMultisigTransactionMiddle,
                tx, redeem_script, 0, SIGHASH_ALL)
            await asyncio.to_thread(
                self.wallet._compileClaimOnP2SHMultiSigEnd,
                tx, redeem_script, _make_redeem_params(our_sig), 1, None)
            tx_hex = tx.serialize().hex()
            txid = _check_broadcast(
                await asyncio.to_thread(self.wallet.broadcast, tx_hex))
            logging.info(
                f'Channel: PATH A claimed {p2sh_address} '
                f'({commitment.pay_amount_sats} sats) — txid={txid}',
                color='green')
        else:
            # Need extra fee — try PATH B first (receiver adds EVR input)
            estimated_size_b = estimated_size + EVR_INPUT_SIZE + EVR_CHANGE_SIZE
            required_fee_b = math.ceil(estimated_size_b * TxUtils.feeRate)
            # Find a usable EVR UTXO: prefer one that covers the fee exactly,
            # fall back to the largest available.
            evr_utxo = None
            for u in sorted(
                (self.wallet.unspentCurrency or []),
                key=lambda x: x.get('value', 0)
            ):
                if u.get('value', 0) >= required_fee_b:
                    evr_utxo = u
                    break
            if evr_utxo is None:
                for u in sorted(
                    (self.wallet.unspentCurrency or []),
                    key=lambda x: -x.get('value', 0)
                ):
                    if u.get('value', 0) > 0:
                        evr_utxo = u
                        break

            # Allow PATH B even if the UTXO can't cover fee+change — any EVR
            # that covers at least the fee-with-input is usable (excess becomes fee).
            required_fee_input_only = math.ceil(
                (estimated_size + EVR_INPUT_SIZE) * TxUtils.feeRate)
            if evr_utxo and evr_utxo.get('value', 0) >= required_fee_input_only:
                # ── PATH B: receiver adds EVR input to cover fee ─────────────
                evr_value = evr_utxo['value']
                evr_change = evr_value - required_fee_b
                # Append EVR input
                evr_txin = CMutableTxIn(
                    COutPoint(lx(evr_utxo['tx_hash']), evr_utxo['tx_pos']))
                tx.vin.append(evr_txin)
                # Append EVR change output if above dust (546 sats)
                if evr_change >= 546:
                    tx.vout.append(CMutableTxOut(
                        evr_change,
                        CEvrmoreAddress(self.wallet.address).to_scriptPubKey()))
                # Receiver signs P2SH vin[0] with SIGHASH_ALL
                our_sig = await asyncio.to_thread(
                    self.wallet.paymentChannelMultisigTransactionMiddle,
                    tx, redeem_script, 0, SIGHASH_ALL)
                # Standard P2PKH scriptPubKey for the EVR input
                evr_script = CScript([
                    OP_DUP, OP_HASH160,
                    Hash160(bytes.fromhex(self.wallet.pubkey)),
                    OP_EQUALVERIFY, OP_CHECKSIG])
                # Compile: sets P2SH scriptSig (vin[0]) and signs EVR (vin[1])
                await asyncio.to_thread(
                    self.wallet._compileClaimOnP2SHMultiSigEnd,
                    tx, redeem_script, _make_redeem_params(our_sig), 1,
                    [evr_script])
                tx_hex = tx.serialize().hex()
                txid = _check_broadcast(
                    await asyncio.to_thread(self.wallet.broadcast, tx_hex))
                logging.info(
                    f'Channel: PATH B claimed {p2sh_address} '
                    f'({commitment.pay_amount_sats} sats, EVR fee {required_fee_b}) '
                    f'— txid={txid}',
                    color='green')
            else:
                # ── PATH C: Mundo — receiver has no EVR, pays SATORI fee ─────
                txid = await self._claimChannelViaMundo(
                    channel=channel,
                    commitment=commitment,
                    partial_tx=tx,
                    redeem_script=redeem_script,
                    sender_sigs=sender_sigs)
                logging.info(
                    f'Channel: PATH C claimed {p2sh_address} via Mundo '
                    f'({commitment.pay_amount_sats} sats) — txid={txid}',
                    color='green')
        # ── DB update: must happen before Nostr ops to survive a crash ─────────
        # If the process is killed after broadcast but before this block, the
        # relay still has the old commitment. On restart, Part 1/2 of the stale
        # tombstone fix will detect the UTXO mismatch and clean it up. By doing
        # DB writes here (before Nostr), we minimise that window.
        settlement_ts = int(time.time())
        if change_vout >= 0 and change_sats > 0:
            await asyncio.to_thread(
                self.networkDB.update_channel_funding,
                p2sh_address, txid, change_vout, change_sats,
                settlement_ts)
            logging.info(
                f'Channel: {p2sh_address} new UTXO '
                f'{txid[:12]}…:{change_vout} ({change_sats} sats)',
                color='cyan')
        else:
            logging.info(
                f'Channel: {p2sh_address} fully drained, awaiting refund',
                color='yellow')
        # ── Grant access for exactly this payment, not cumulative total ───────
        await self._grantChannelAccess(
            sender_nostr_pubkey=channel.get('sender_nostr_pubkey'),
            total_paid_sats=commitment.pay_amount_sats,
            stream_name=commitment.stream_name,
            p2sh_address=p2sh_address)
        logging.info(
            f'Channel: claimed {commitment.pay_amount_sats} sats '
            f'from {p2sh_address} — txid={txid}',
            color='green')
        # Notify sender so they can update their funding UTXO.
        sender_nostr_pubkey = channel.get('sender_nostr_pubkey')
        if sender_nostr_pubkey:
            from satorilib.satori_nostr.models import ChannelSettlement
            settlement = ChannelSettlement(
                p2sh_address=p2sh_address,
                claim_txid=txid,
                new_funding_vout=change_vout,
                new_locked_sats=change_sats,
                timestamp=settlement_ts,
            )
            for relay_url, client in self._networkClients.items():
                try:
                    await client.publish_settlement(settlement, sender_nostr_pubkey)
                    logging.info(
                        f'Channel: published settlement on {relay_url} '
                        f'to sender {sender_nostr_pubkey[:16]}…', color='cyan')
                except Exception as e:
                    logging.warning(
                        f'Channel: failed to publish settlement on {relay_url}: {e}')
        else:
            logging.warning(
                f'Channel: {p2sh_address} has no sender_nostr_pubkey — '
                f'cannot notify sender of settlement')
        # Tombstone the old commitment on all relays
        for client in self._networkClients.values():
            try:
                await client.remove_commitment(p2sh_address)
            except Exception:
                pass
        await asyncio.to_thread(
            self.networkDB.clear_pending_commitment, p2sh_address)
        return txid

    async def _claimChannelViaMundo(
        self,
        channel: dict,
        commitment,
        partial_tx,
        redeem_script,
        sender_sigs: list,
    ) -> str:
        """PATH C claim: receiver has no EVR — pay SATORI fee to Mundo.

        Fix J — idempotent retry: the built tx and Mundo params are cached in
        _mundoCache[p2sh] so a retry after a mid-flow failure reuses the same
        tx rather than rebuilding with potentially different UTXOs/params.

        Three retry tiers:
          a) signed_hex cached → skip to broadcast (Step 6).
          b) incomplete_hex cached → skip to Mundo POST (Step 5).
          c) nothing cached → full build (Steps 1-6).
        """
        import requests as _requests
        from evrmore.core import (
            CMutableTransaction, CMutableTxIn, CMutableTxOut, COutPoint, lx)
        from evrmore.core.script import (
            CScript, SIGHASH_ALL, SIGHASH_ANYONECANPAY)
        from evrmore.wallet import CEvrmoreAddress
        from satorilib.wallet.evrmore.scripts.channels import unlock
        from satorilib.wallet.concepts.transaction import AssetTransaction
        from satorilib.wallet.utils.transaction import TxUtils
        from functools import partial as funcpartial

        MUNDO_URL = os.environ.get('MUNDO_URL', 'https://mundo.satorinet.org')
        p2sh = channel['p2sh_address']
        cached = self._mundoCache.get(p2sh, {})

        # ── Tier A: fully-signed tx from Mundo already cached ───────────
        if cached.get('signed_hex'):
            logging.info(
                f'Channel: Mundo retry tier A — broadcasting cached tx '
                f'for {p2sh}', color='cyan')
            try:
                txid = await asyncio.to_thread(
                    self.wallet.broadcast, cached['signed_hex'])
                self._mundoCache.pop(p2sh, None)
                return txid
            except Exception as e:
                logging.warning(
                    f'Channel: cached signed tx failed: {e} — rebuilding')
                self._mundoCache.pop(p2sh, None)

        # ── Tier B: signed incomplete tx cached, re-post to Mundo ───────
        if cached.get('incomplete_hex') and cached.get('fee_sats_reserved'):
            logging.info(
                f'Channel: Mundo retry tier B — re-posting to Mundo '
                f'for {p2sh}', color='cyan')
            try:
                def _mundo_post_cached():
                    resp = _requests.post(
                        f'{MUNDO_URL}/simple_partial/broadcast/evrmore'
                        f'/{cached["fee_sats_reserved"]}/{cached["fee_sats"]}/0',
                        params={'signOnly': 'true'},
                        data=cached['incomplete_hex'],
                        headers={'Content-Type': 'text/plain'},
                        timeout=30)
                    resp.raise_for_status()
                    return resp.text
                signed_hex = await asyncio.to_thread(_mundo_post_cached)
                self._mundoCache[p2sh] = {
                    **cached, 'signed_hex': signed_hex}
                txid = await asyncio.to_thread(
                    self.wallet.broadcast, signed_hex)
                self._mundoCache.pop(p2sh, None)
                return txid
            except Exception as e:
                logging.warning(
                    f'Channel: Mundo tier B failed: {e} — full rebuild')
                self._mundoCache.pop(p2sh, None)

        # ── Tier C: full build ──────────────────────────────────────────

        # Step 1: find receiver's SATORI UTXO
        satori_utxo = None
        for u in sorted(
            [u for u in (self.wallet.unspentAssets or [])
             if u.get('name', u.get('asset')) == 'SATORI'
             and u.get('value', 0) > 0],
            key=lambda x: x.get('value', 0)
        ):
            satori_utxo = u
            break
        if not satori_utxo:
            raise ValueError(
                f'Channel {channel["p2sh_address"]}: no EVR and no SATORI — '
                f'cannot cover the mining fee to claim this channel')

        # Step 2: compute tx shape for Mundo fee request
        p2sh_input_count = len(partial_tx.vin)
        existing_output_count = len(partial_tx.vout)
        # Inputs: P2SH inputs + 1 SATORI + 1 Mundo EVR (added by Mundo)
        final_input_count = p2sh_input_count + 1 + 1
        # Outputs: existing + SATORI fee + SATORI change (maybe) + EVR change
        final_output_count = existing_output_count + 3

        def _request_mundo():
            resp = _requests.get(
                f'{MUNDO_URL}/simple_partial/request/evrmore',
                params={
                    'inputCount': final_input_count,
                    'outputCount': final_output_count},
                timeout=15)
            resp.raise_for_status()
            return resp.json()

        mundo_data = await asyncio.to_thread(_request_mundo)
        mundo_satori_fee = int(mundo_data['satoriFeeAmount'])
        mundo_satori_fee_addr = mundo_data['satoriFeeAddress']
        mundo_evr_change_addr = mundo_data.get('changeAddress', '')
        mundo_evr_change_amt = int(mundo_data.get('changeAmount', 0))
        fee_sats_reserved = int(mundo_data['feeSatsReserved'])
        fee_sats = int(mundo_data['feeSats'])

        satori_value = satori_utxo['value']
        if satori_value < mundo_satori_fee:
            raise ValueError(
                f'Channel: SATORI UTXO ({satori_value} sats) < '
                f'Mundo fee ({mundo_satori_fee} sats)')

        # Step 3: build new tx with SATORI input + Mundo outputs
        new_vins = list(partial_tx.vin)
        new_vouts = list(partial_tx.vout)

        # Add receiver's SATORI input
        satori_txin = CMutableTxIn(
            COutPoint(lx(satori_utxo['tx_hash']), satori_utxo['tx_pos']))
        satori_vin_idx = len(new_vins)
        new_vins.append(satori_txin)

        # Add SATORI change output to receiver (if any) — must come BEFORE
        # Mundo fee so that vout[-2]=fee matches _verifyClaimAddress expectation
        from evrmore.core.script import OP_EVR_ASSET, OP_DROP
        satori_change = satori_value - mundo_satori_fee
        if satori_change > 0:
            change_script = CScript([
                *CEvrmoreAddress(self.wallet.address).to_scriptPubKey(),
                OP_EVR_ASSET,
                bytes.fromhex(
                    AssetTransaction.satoriHex(self.wallet.symbol) +
                    TxUtils.padHexStringTo8Bytes(
                        TxUtils.intToLittleEndianHex(satori_change))),
                OP_DROP])
            new_vouts.append(CMutableTxOut(0, change_script))

        # Add SATORI fee output to Mundo (vout[-2] position)
        fee_script = CScript([
            *CEvrmoreAddress(mundo_satori_fee_addr).to_scriptPubKey(),
            OP_EVR_ASSET,
            bytes.fromhex(
                AssetTransaction.satoriHex(self.wallet.symbol) +
                TxUtils.padHexStringTo8Bytes(
                    TxUtils.intToLittleEndianHex(mundo_satori_fee))),
            OP_DROP])
        new_vouts.append(CMutableTxOut(0, fee_script))

        # Add EVR change output for Mundo
        if mundo_evr_change_addr and mundo_evr_change_amt > 0:
            new_vouts.append(CMutableTxOut(
                mundo_evr_change_amt,
                CEvrmoreAddress(mundo_evr_change_addr).to_scriptPubKey()))

        new_tx = CMutableTransaction(new_vins, new_vouts)

        # Step 4: sign P2SH vin[0] with 0x81 (SIGHASH_ALL | ANYONECANPAY)
        # This locks all outputs but still allows Mundo to add its EVR input.
        mundo_sighash = SIGHASH_ALL | SIGHASH_ANYONECANPAY  # 0x81
        our_sig = await asyncio.to_thread(
            self.wallet.paymentChannelMultisigTransactionMiddle,
            new_tx, redeem_script, 0, mundo_sighash)
        redeemParams = funcpartial(
            unlock.paymentChannel,
            sender_sig=sender_sigs[0],
            receiver_sig=our_sig)
        new_tx.vin[0].scriptSig = redeemParams() + redeem_script

        # Sign receiver's SATORI input with 0x81
        # _compileInputs returns (txins, txinScripts); we need the script
        _, satori_scripts = await asyncio.to_thread(
            self.wallet._compileInputs,
            [],              # gatheredCurrencyUnspents (positional)
            [satori_utxo],  # gatheredSatoriUnspents (positional)
        )
        satori_script = satori_scripts[0]
        await asyncio.to_thread(
            self.wallet._signInput,
            new_tx, satori_vin_idx, satori_txin, satori_script, mundo_sighash)

        # Cache the signed incomplete tx (tier B on retry)
        incomplete_hex = new_tx.serialize().hex()
        self._mundoCache[p2sh] = {
            'incomplete_hex': incomplete_hex,
            'fee_sats_reserved': fee_sats_reserved,
            'fee_sats': fee_sats,
        }

        # Step 5: POST to Mundo (signOnly=true)
        def _broadcast_via_mundo():
            resp = _requests.post(
                f'{MUNDO_URL}/simple_partial/broadcast/evrmore'
                f'/{fee_sats_reserved}/{fee_sats}/0',
                params={'signOnly': 'true'},
                data=incomplete_hex,
                headers={'Content-Type': 'text/plain'},
                timeout=30)
            resp.raise_for_status()
            return resp.text

        signed_hex = await asyncio.to_thread(_broadcast_via_mundo)
        # Cache the fully-signed tx (tier A on retry)
        self._mundoCache[p2sh] = {
            **self._mundoCache.get(p2sh, {}), 'signed_hex': signed_hex}

        # Step 6: broadcast the fully-signed tx and return txid
        txid = await asyncio.to_thread(self.wallet.broadcast, signed_hex)
        self._mundoCache.pop(p2sh, None)
        return txid

    async def _grantChannelAccess(
        self,
        sender_nostr_pubkey: str,
        total_paid_sats: int,
        stream_name: str = '',
        p2sh_address: str = '',
    ) -> None:
        """Update subscriber access rights after a successful channel claim.

        Computes how many observations the sender has pre-paid for and
        advances their last_paid_seq in every connected SatoriNostr client.

        If stream_name is provided (populated from the ChannelCommitment Nostr
        tag), only that specific stream is updated — prevents over-granting
        access to other streams on the same channel.  If stream_name is empty
        (old commitment without the tag, or manual call), falls back to
        granting access across all active paid publications.

        The provider's publish_observation() only sends encrypted data to
        subscribers whose last_paid_seq >= current seq_num, so this is what
        unlocks the next batch of observations for the paying subscriber.

        When p2sh_address is supplied, the granted last_paid_seq is persisted
        to the subscriber_access table so it survives provider restarts and
        Nostr key rotations (Fix C).
        """
        if not sender_nostr_pubkey or total_paid_sats <= 0:
            return
        pubs = await asyncio.to_thread(self.networkDB.get_active_publications)
        for pub in pubs:
            effective_price = pub.get('price_per_obs', 0)
            if effective_price <= 0:
                continue
            pub_stream = pub['stream_name']
            # If the commitment names a specific stream, skip all others
            if stream_name and pub_stream != stream_name:
                continue
            paid_count = total_paid_sats // effective_price
            if paid_count <= 0:
                continue
            current_seq = pub.get('last_seq_num', 0)
            grant_to_seq = current_seq + paid_count
            for client in self._networkClients.values():
                # Ensure the subscriber is registered before granting access.
                # record_payment is a no-op if they aren't in _subscribers,
                # which can happen if a claim fires before their subscription
                # announcement has been replayed on this relay.
                if sender_nostr_pubkey not in client._subscribers.get(
                        pub_stream, {}):
                    client.record_subscription(pub_stream, sender_nostr_pubkey)
                # Advance from the subscriber's existing last_paid_seq when
                # higher than current_seq — handles multiple commits arriving
                # between publishes, where last_seq_num hasn't moved but each
                # incremental payment should still grant one more observation.
                existing = client._subscribers[pub_stream].get(sender_nostr_pubkey)
                base_seq = current_seq
                if existing and existing.last_paid_seq is not None:
                    base_seq = max(base_seq, existing.last_paid_seq)
                grant_to_seq = base_seq + paid_count
                client.record_payment(pub_stream, sender_nostr_pubkey, grant_to_seq)
            # Persist to DB so access survives provider restarts (Fix C).
            if p2sh_address:
                try:
                    await asyncio.to_thread(
                        self.networkDB.save_subscriber_access,
                        pub_stream, p2sh_address,
                        sender_nostr_pubkey, grant_to_seq)
                except Exception as e:
                    logging.warning(
                        f'Channel: failed to persist subscriber access: {e}')
            logging.info(
                f'Channel: granted {sender_nostr_pubkey[:16]}… access to '
                f'{pub_stream} up to seq={grant_to_seq} '
                f'({paid_count} obs)',
                color='cyan')

    async def _channelExpiryCheck(self):
        """Auto-claim receiver channels every 15 days.

        Called from the reconcile loop but throttled to run at most every
        15 days. Ensures the provider collects accrued micropayments well
        before the 31-day CSV timeout.
        """
        now = time.time()
        last = getattr(self, '_lastChannelExpiryCheck', 0)
        if now - last < 1296000:  # 15 days
            return
        self._lastChannelExpiryCheck = now
        try:
            near_expiry = await asyncio.to_thread(
                self.networkDB.get_channels_near_expiry, 1296000)
            for channel in near_expiry:
                p2sh = channel['p2sh_address']
                logging.info(
                    f'Channel: auto-claiming near-expiry channel {p2sh}',
                    color='yellow')
                try:
                    await self.claimChannel(p2sh)
                except Exception as e:
                    logging.error(
                        f'Channel: auto-claim failed for {p2sh}: {e}')
        except Exception as e:
            logging.warning(f'Channel: expiry check failed: {e}')

    def _channelFundSats(self, price_per_obs: int = 0) -> int:
        """Return channel funding amount in sats, scaled by observation economics.

        Fix K: instead of a hardcoded 1M sats, compute from price_per_obs *
        observations_per_refill (configurable, default 500). Clamped between
        100k (min) and 10M (max) sats. Falls back to the static config value
        or 1M when price_per_obs is not supplied.
        """
        static = int(config.get().get('channel_fund_sats', 1_000_000))
        if price_per_obs <= 0:
            return static
        obs_per_refill = int(config.get().get('observations_per_refill', 500))
        computed = price_per_obs * obs_per_refill
        return max(100_000, min(computed, 1_000_000))

    def _channelFetchUtxoSatori(
        self,
        txid: str,
        vout: int,
        fallback_sats: int,
    ) -> int:
        """Parse the broadcast tx's SATORI asset output to get the exact
        on-chain amount. Falls back to the requested amount on any error.
        """
        try:
            raw = self.wallet.electrumx.api.sendRequest(
                method='blockchain.transaction.get',
                params=[txid, True])
            if not isinstance(raw, dict):
                return fallback_sats
            vouts = raw.get('vout') or []
            if vout >= len(vouts):
                return fallback_sats
            asm = vouts[vout].get('scriptPubKey', {}).get('asm', '')
            # Asset data hex comes after 'OP_EVR_ASSET '. Format:
            # <len><'evrt'><asset_name_len><asset_name><8-byte LE amount>...
            marker = 'OP_EVR_ASSET '
            idx = asm.find(marker)
            if idx < 0:
                return fallback_sats
            asset_hex = asm[idx + len(marker):].split()[0]
            # skip 1 byte length, 4 bytes 'evrt' (total 5 bytes = 10 hex)
            data = asset_hex[10:]
            # skip asset name: 1 byte length + name
            name_len = int(data[:2], 16)
            amount_hex = data[2 + 2 * name_len : 2 + 2 * name_len + 16]
            # little-endian to int
            amount = int.from_bytes(bytes.fromhex(amount_hex), 'little')
            return amount
        except Exception as e:
            logging.warning(f'Channel: could not parse UTXO amount for {txid[:12]}: {e}')
            return fallback_sats

    async def _channelVerifyUtxo(self, channel: dict) -> dict:
        """Check on-chain that the channel's funding UTXO still exists.

        Queries electrum for unspent outputs at the P2SH address. If the
        recorded funding_txid:funding_vout is missing, looks for the newest
        SATORI UTXO at that address and updates the DB to point at it.
        Always stamps utxo_checked_at so we don't re-check for 24 hours.

        Returns the (possibly updated) channel dict.
        """
        p2sh = channel['p2sh_address']
        funding_txid = channel['funding_txid']
        funding_vout = channel['funding_vout']
        try:
            scripthash = EvrmoreWallet.p2shScripthash(p2sh)
            unspents = await asyncio.to_thread(
                self.wallet.electrumx.api.getUnspentAssets,
                scripthash, 'SATORI')
            unspents = unspents or []
            found = any(
                u.get('tx_hash') == funding_txid
                and u.get('tx_pos') == funding_vout
                for u in unspents)
            if found:
                await asyncio.to_thread(
                    self.networkDB.update_utxo_checked_at, p2sh)
                channel = await asyncio.to_thread(
                    self.networkDB.get_channel, p2sh)
                return channel
            # UTXO is gone — find the replacement (newest by block height)
            if unspents:
                best = max(unspents, key=lambda u: u.get('height', 0))
                new_txid = best['tx_hash']
                new_vout = best['tx_pos']
                new_sats = self._channelFetchUtxoSatori(
                    new_txid, new_vout, best.get('value', 0))
                await asyncio.to_thread(
                    self.networkDB.update_channel_funding,
                    p2sh, new_txid, new_vout, new_sats)
                logging.info(
                    f'Channel: UTXO liveness check — {p2sh} funding moved '
                    f'from {funding_txid[:12]}…:{funding_vout} to '
                    f'{new_txid[:12]}…:{new_vout} ({new_sats} sats)',
                    color='yellow')
            else:
                # No SATORI UTXOs at this address at all — channel is drained
                await asyncio.to_thread(
                    self.networkDB.update_channel_remainder, p2sh, 0)
                logging.info(
                    f'Channel: UTXO liveness check — {p2sh} has no '
                    f'unspent SATORI, zeroing remainder',
                    color='yellow')
            await asyncio.to_thread(
                self.networkDB.update_utxo_checked_at, p2sh)
            channel = await asyncio.to_thread(
                self.networkDB.get_channel, p2sh)
        except Exception as e:
            logging.warning(
                f'Channel: UTXO liveness check failed for {p2sh}: {e}')
        return channel

    def _channelTimeoutMinutes(self) -> int:
        """Return the configured channel lifetime in minutes (default 7 days).

        Fix L: lowered from 90 days (129,600 min) to 7 days (10,080 min) so
        subscriber funds aren't idle for a quarter year in the failure case.
        """
        return int(config.get().get('channel_timeout_minutes', 44640))

    @staticmethod
    def _channelVerifySenderSig(commitment, channel: dict) -> bool:
        """Verify the sender's ECDSA signature on the commitment's partial tx.

        The commitment carries `sender_sigs[0]` — a DER signature with the
        sighash-flag byte appended, covering vin[0] of `partial_tx_hex` under
        the channel's redeem script. We recompute the sighash and check it
        against the stored sender EVR pubkey. This closes the forgery path
        where a third-party publisher fills in the correct-looking sender
        pubkey fields but cannot sign for the real sender.
        """
        try:
            from evrmore.core import CMutableTransaction
            from evrmore.core.script import CScript, SignatureHash
            from evrmore.core.key import CPubKey
            sigs = getattr(commitment, 'sender_sigs', None) or []
            if not sigs:
                return False
            raw_sig = bytes.fromhex(sigs[0])
            if len(raw_sig) < 2:
                return False
            sighash_flag = raw_sig[-1]
            der_sig = raw_sig[:-1]
            tx = CMutableTransaction.deserialize(
                bytes.fromhex(commitment.partial_tx_hex))
            if not tx.vin:
                return False
            redeem_script = CScript(bytes.fromhex(channel['redeem_script']))
            sighash = SignatureHash(redeem_script, tx, 0, sighash_flag)
            pubkey = CPubKey(bytes.fromhex(channel['sender_pubkey']))
            return bool(pubkey.verify(sighash, der_sig))
        except Exception as e:
            logging.warning(
                f'Channel: signature verification errored for '
                f'{getattr(commitment, "p2sh_address", "?")}: {e}')
            return False

    @staticmethod
    def _channelExpiresAt(channel: dict) -> int:
        """Unix time after which the CSV lock is (best-effort) satisfied.

        Uses the real CSV-rounded seconds (512-second granularity for time-
        locks, ~60-second approximation for block-locks). Returns 0 if the
        channel lacks a created_at or a timeout spec.
        """
        created_at = int(channel.get('created_at') or 0)
        if not created_at:
            return 0
        minutes = channel.get('minutes')
        blocks = channel.get('blocks')
        if minutes:
            units = max(1, math.ceil(float(minutes) * 60 / 512))
            return created_at + units * 512
        if blocks:
            return created_at + int(blocks) * 60
        return 0

    async def _channelEnsureWallet(self) -> None:
        """Ensure the wallet has a live Electrumx connection (Fix E).

        Channel operations (open, refund, claim, pay) need the wallet online.
        On startup the connection may not be ready yet; this retries once after
        a short delay. Raises RuntimeError if the wallet is still unreachable
        so the caller gets a clear error instead of a swallowed warning.
        """
        if self.walletManager.isConnected():
            return
        if await asyncio.to_thread(self.walletManager.connect):
            return
        # Retry once after a short wait — the wallet often recovers quickly
        await asyncio.sleep(3)
        if await asyncio.to_thread(self.walletManager.connect):
            logging.info('Channel: wallet connected on retry', color='green')
            return
        raise RuntimeError(
            'Wallet is not connected — channel operation aborted. '
            'The Electrumx connection will be retried on the next cycle.')

    async def openChannel(
        self,
        receiver_pubkey: str,
        amount_sats: int,
        minutes: int = None,
        blocks: int = None,
        receiver_nostr_pubkey: str = None,
    ) -> str:
        """Fund a new payment channel to a receiver (sender/buyer side).

        Locks funds in a 2-of-2 P2SH multisig with a CSV timeout so the
        sender can reclaim if the receiver never claims.  If receiver_nostr_pubkey
        is provided, a KIND_CHANNEL_OPEN announcement is published to all
        connected relays so the receiver can register the channel automatically.

        Args:
            receiver_pubkey: Receiver's Satori wallet public key (hex)
            amount_sats: Amount to lock in the channel in satoshis
            minutes: CSV timeout in minutes (mutually exclusive with blocks)
            blocks: CSV timeout in blocks (mutually exclusive with minutes)
            receiver_nostr_pubkey: Receiver's Nostr pubkey for push delivery

        Returns:
            The P2SH address of the new channel
        """
        await self._channelEnsureWallet()
        await asyncio.to_thread(self.wallet.getUnspents)
        amount_satori = amount_sats / 1e8
        txid, script_payload = await asyncio.to_thread(
            self.wallet.producePaymentChannel,
            receiver_pubkey,
            None,       # sender defaults to our own pubkey
            blocks,
            minutes,
            None,       # memo
            amount_satori,
            True,       # broadcast
        )
        p2sh_address = script_payload['p2sh_address']
        # Persist the actual SATORI amount that landed in the funding UTXO, not
        # the user's requested amount. Query the broadcast tx's output to get
        # the exact on-chain value — producePaymentChannel's internal rounding
        # can shift a sat, and every subsequent commitment would fail to
        # balance inputs/outputs on-chain if we stored the request.
        funding_vout = script_payload['funding_vout'] or 0
        actual_locked = await asyncio.to_thread(
            self._channelFetchUtxoSatori,
            script_payload['funding_txid'],
            funding_vout,
            amount_sats)
        # Single shared timestamp for local DB + announcement so sender and
        # receiver agree on the CSV-timer anchor (mirrors refundChannel).
        open_ts = int(time.time())
        await asyncio.to_thread(
            self.networkDB.save_channel,
            p2sh_address,
            self.wallet.pubkey,
            receiver_pubkey,
            script_payload['redeem_script_hex'],
            script_payload['funding_txid'],
            script_payload['funding_vout'] or 0,
            actual_locked,
            actual_locked,  # remainder starts equal to locked
            True,            # is_sender
            blocks,
            minutes,
            sender_nostr_pubkey=self.nostrPubkey or '',
            receiver_nostr_pubkey=receiver_nostr_pubkey or '',
            created_at=open_ts,
        )
        logging.info(
            f'Channel: opened {p2sh_address} '
            f'amount={amount_sats} sats receiver={receiver_pubkey}',
            color='green')
        # Announce to receiver via Nostr so they can register the channel
        if receiver_nostr_pubkey:
            from satorilib.satori_nostr.models import ChannelOpen
            channel_open = ChannelOpen(
                p2sh_address=p2sh_address,
                sender_pubkey=self.wallet.pubkey,
                receiver_pubkey=receiver_pubkey,
                redeem_script=script_payload['redeem_script_hex'],
                funding_txid=script_payload['funding_txid'],
                funding_vout=script_payload['funding_vout'] or 0,
                locked_sats=actual_locked,
                blocks=blocks,
                minutes=minutes,
                timestamp=open_ts,
                sender_nostr_pubkey=self.nostrPubkey or '',
            )
            for client in self._networkClients.values():
                try:
                    await client.publish_channel_open(
                        channel_open, receiver_nostr_pubkey)
                except Exception as e:
                    logging.warning(
                        f'Channel: failed to announce open: {e}')
        return p2sh_address

    async def refundChannel(
        self,
        p2sh_address: str,
        amount_sats: int = None,
    ) -> None:
        """Send new SATORI to an existing channel's P2SH address (sender side).

        Channels are persistent — when drained, they are refunded rather than
        deleted and reopened.  Uses the stored redeem_script to call
        producePaymentChannelFromScript, then updates the DB with the new
        funding UTXO.

        Args:
            p2sh_address: The channel to refund
            amount_sats: Amount to send (defaults to _channelFundSats())
        """
        await self._channelEnsureWallet()
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, p2sh_address)
        if not channel:
            raise ValueError(f'Unknown channel: {p2sh_address}')
        amount_sats = amount_sats or self._channelFundSats()
        amount_satori = amount_sats / 1e8
        # Refresh wallet UTXOs so _gatherSatoriUnspents sees current state
        await asyncio.to_thread(self.wallet.getReadyToSend)
        from evrmore.core.script import CScript
        redeem_script = CScript(bytes.fromhex(channel['redeem_script']))
        script_payload = {
            'redeem_script': str(redeem_script),
            'redeem_script_hex': channel['redeem_script'],
            'redeem_script_size': len(redeem_script),
            'p2sh_address': p2sh_address,
            'amount': amount_satori,
        }
        _txhex, txid, result = await asyncio.to_thread(
            self.wallet.producePaymentChannelFromScript,
            redeemScript=redeem_script,
            scriptPayload=script_payload,
            broadcast=True,
        )
        funding_vout = result.get('funding_vout') or 0
        # Use the actual on-chain UTXO amount to avoid divisibility rounding
        # discrepancies between our request and what actually landed on-chain.
        actual_locked = await asyncio.to_thread(
            self._channelFetchUtxoSatori, txid, funding_vout, amount_sats)
        # Single shared timestamp for local DB + announcement so sender
        # and receiver agree on the CSV-timer anchor.
        refund_ts = int(time.time())
        await asyncio.to_thread(
            self.networkDB.update_channel_funding,
            p2sh_address, txid, funding_vout, actual_locked, refund_ts)
        logging.info(
            f'Channel: refunded {p2sh_address} '
            f'amount={amount_sats} sats txid={txid}',
            color='green')
        # Announce to receiver so they pick up the new funding UTXO and
        # reset their CSV timer. Reuses the channel_open kind since the
        # receiver's handler now upserts on funding_txid change.
        receiver_nostr_pubkey = channel.get('receiver_nostr_pubkey')
        if receiver_nostr_pubkey:
            from satorilib.satori_nostr.models import ChannelOpen
            channel_open = ChannelOpen(
                p2sh_address=p2sh_address,
                sender_pubkey=channel['sender_pubkey'],
                receiver_pubkey=channel['receiver_pubkey'],
                redeem_script=channel['redeem_script'],
                funding_txid=txid,
                funding_vout=funding_vout,
                locked_sats=actual_locked,
                blocks=channel.get('blocks'),
                minutes=channel.get('minutes'),
                timestamp=refund_ts,
                sender_nostr_pubkey=self.nostrPubkey or '',
            )
            for client in self._networkClients.values():
                try:
                    await client.publish_channel_open(
                        channel_open, receiver_nostr_pubkey)
                except Exception as e:
                    logging.warning(
                        f'Channel: failed to announce refund: {e}')

    async def sendChannelPayment(
        self,
        p2sh_address: str,
        pay_amount_sats: int,
        stream_name: str = '',
    ) -> None:
        """Issue a payment commitment to the receiver over Nostr (sender side).

        Builds a half-signed transaction and publishes it to all connected
        relays. The receiver's _channelListen loop picks it up and broadcasts.

        The partial tx contains only the P2SH channel input and SATORI outputs
        (no EVR inputs). The receiver handles the mining fee via 3-path logic:
        PATH A (fee embedded), PATH B (receiver adds EVR), PATH C (Mundo).

        Args:
            p2sh_address: The channel to pay from
            pay_amount_sats: Amount to pay the receiver this round (sats)
        """
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, p2sh_address)
        if not channel:
            raise ValueError(f'Unknown channel: {p2sh_address}')
        expires_at = self._channelExpiresAt(channel)
        if expires_at and int(time.time()) >= expires_at:
            raise ValueError(
                f'Channel {p2sh_address} has expired — the sender can now '
                f'reclaim, so new commitments would race that reclaim and '
                f'are not safe to send')
        # UTXO liveness check: if we haven't verified the funding UTXO
        # on-chain in the last 24 hours, query electrum to make sure it's
        # still unspent. If the receiver claimed and we missed the Nostr
        # settlement, this catches it from the source of truth.
        UTXO_CHECK_INTERVAL = 86400  # 24 hours
        utxo_checked_at = channel.get('utxo_checked_at', 0) or 0
        if int(time.time()) - utxo_checked_at > UTXO_CHECK_INTERVAL:
            channel = await self._channelVerifyUtxo(channel)
        if pay_amount_sats > channel['remainder_sats']:
            raise ValueError(
                f'Pay amount {pay_amount_sats} exceeds channel remainder '
                f'{channel["remainder_sats"]}')
        from evrmore.core.script import CScript
        from satorilib.satori_nostr.models import ChannelCommitment
        from satorilib.wallet.utils.transaction import TxUtils
        redeem_script = CScript(bytes.fromhex(channel['redeem_script']))
        locked_sats = channel['locked_sats']
        cumulative_sats = (
            locked_sats - channel['remainder_sats'] + pay_amount_sats)
        # The channel stores the receiver's 33-byte EVR wallet pubkey, but the
        # partial tx output needs a P2PKH address string.
        receiver_address = EvrmoreWallet.generateAddress(
            channel['receiver_pubkey'])
        # Build partial tx with ONLY the P2SH input and SATORI outputs.
        # No EVR inputs are included — the receiver resolves the fee via
        # 3-path logic so the sender doesn't need EVR to send micropayments.
        if self.wallet.divisibility == 0:
            await asyncio.to_thread(self.wallet.getReadyToSend)
        def _build_partial_tx():
            sat_sats = TxUtils.roundSatsDownToDivisibility(
                sats=cumulative_sats,
                divisibility=self.wallet.divisibility)
            change_out = self.wallet._compileSatoriChangeOutput(
                satoriSats=sat_sats,
                gatheredSatoriSats=locked_sats,
                changeAddress=channel['p2sh_address'])
            return self.wallet._compileClaimOnP2SHMultiSigStart(
                toAddress=receiver_address,
                satoriSats=sat_sats,
                feeOverride=None,
                fundingTxIds=[channel['funding_txid']],
                fundingVouts=[channel['funding_vout']],
                extraVins=[],
                extraVouts=[change_out] if change_out else [])
        tx = await asyncio.to_thread(_build_partial_tx)
        sig = await asyncio.to_thread(
            self.wallet.paymentChannelMultisigTransactionMiddle,
            tx,
            redeem_script,
            0,    # vinIndex
            None, # sighashFlag — use default SIGHASH_SINGLE|ANYONECANPAY (0x83)
        )
        remainder = channel['remainder_sats'] - pay_amount_sats
        commitment = ChannelCommitment(
            p2sh_address=p2sh_address,
            sender_pubkey=self.wallet.pubkey,
            receiver_pubkey=channel['receiver_pubkey'],
            partial_tx_hex=tx.serialize().hex(),
            sender_sigs=[sig.hex()],
            pay_amount_sats=pay_amount_sats,
            remainder_sats=remainder,
            fee=0,  # no fee embedded; receiver handles via 3-path
            timestamp=int(time.time()),
            stream_name=stream_name,
        )
        # The Nostr `p` tag must be a 32-byte x-only Nostr pubkey — that is a
        # different value from the 33-byte EVR wallet pubkey stored in
        # channel['receiver_pubkey'], so we thread the persisted nostr pubkey
        # from the channel row into the library call.
        receiver_nostr_pubkey = channel.get('receiver_nostr_pubkey') or ''
        for client in self._networkClients.values():
            try:
                await client.publish_commitment(
                    commitment, receiver_nostr_pubkey)
            except Exception as e:
                logging.warning(f'Channel: failed to publish commitment: {e}')
        # Update sender's remainder so cumulative tracking is correct
        await asyncio.to_thread(
            self.networkDB.update_channel_remainder,
            p2sh_address,
            remainder)
        logging.info(
            f'Channel: published commitment {p2sh_address} '
            f'pay={pay_amount_sats} sats remainder={remainder} sats',
            color='cyan')

    async def reclaimChannel(self, p2sh_address: str) -> str:
        """Reclaim locked SATORI after the CSV timeout has expired (sender side).

        PATH A: sender has EVR → gather EVR for fees, build reclaim tx using
                the CSV single-sig unlock path (OP_FALSE branch), sign with
                SIGHASH_ALL, and broadcast directly.
        PATH B: no EVR → _reclaimChannelViaMundo pays the mining fee via a
                SATORI fee output, signing with SIGHASH_ALL|ANYONECANPAY so
                Mundo can add its EVR input.

        Args:
            p2sh_address: The channel to reclaim

        Returns:
            The broadcast txid
        """
        import math
        from evrmore.core.script import CScript, OP_FALSE, SIGHASH_ALL
        from satorilib.wallet.concepts.transaction import TransactionFailure
        from satorilib.wallet.utils.transaction import TxUtils

        await asyncio.to_thread(self.wallet.getUnspents)
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, p2sh_address)
        if not channel:
            raise ValueError(f'Unknown channel: {p2sh_address}')

        redeem_script = CScript(bytes.fromhex(channel['redeem_script']))

        # Compute CSV sequence value from stored timeout
        CSV_TIME_BIT = 0x00400000
        CSV_UNIT_SECS = 512
        if channel.get('blocks'):
            csv_value = int(channel['blocks'])
        elif channel.get('minutes'):
            csv_value = CSV_TIME_BIT | max(
                1, math.ceil(int(channel['minutes']) * 60 / CSV_UNIT_SECS))
        else:
            raise ValueError(
                f'Channel {p2sh_address}: no timeout (blocks/minutes) defined')

        sat_sats = TxUtils.roundSatsDownToDivisibility(
            sats=channel['locked_sats'],
            divisibility=self.wallet.divisibility)

        def _build_standard_reclaim():
            """PATH A: gather EVR, build csv reclaim tx, sign, broadcast."""
            from evrmore.core.script import CScript, OP_FALSE, SIGHASH_ALL
            # P2SH scriptSig is large (~200 bytes); use size-aware fee estimate
            # (2 inputs: P2SH + EVR, 2 outputs: SATORI + EVR change)
            fee = TxUtils.estimatedFee(inputCount=2, outputCount=2)
            # raises TransactionFailure if not enough EVR
            gathered_utxos, gathered_sats = self.wallet._gatherCurrencyUnspents(
                feeOverride=fee)
            txins_evr, txin_scripts_evr = self.wallet._compileInputs(
                gatheredCurrencyUnspents=gathered_utxos)
            evr_change_out = self.wallet._compileCurrencyChangeOutput(
                gatheredCurrencySats=gathered_sats,
                fee=fee)
            # Build bare tx: P2SH vin + EVR vins, SATORI output + EVR change
            tx = self.wallet._compileClaimOnP2SHMultiSigStart(
                toAddress=self.wallet.address,
                satoriSats=sat_sats,
                fundingTxIds=[channel['funding_txid']],
                fundingVouts=[channel['funding_vout']],
                extraVins=txins_evr,
                extraVouts=[evr_change_out] if evr_change_out else [])
            # Set CSV fields BEFORE signing (they are part of the sighash)
            tx.nVersion = 2
            tx.vin[0].nSequence = csv_value
            # Sign P2SH vin[0] with SIGHASH_ALL (single-sig CSV path)
            sig = self.wallet._compileClaimOnP2SHMultiSigMiddle(
                tx, redeem_script, 0, SIGHASH_ALL)
            # redeemParams() must return just the unlock prefix (no sig arg)
            def redeem_params_csv():
                return CScript([sig, OP_FALSE])
            # Set P2SH scriptSig and sign EVR vins
            self.wallet._compileClaimOnP2SHMultiSigEnd(
                tx, redeem_script, redeem_params_csv, 1, txin_scripts_evr)
            return self.wallet.broadcast(self.wallet._txToHex(tx))

        def _check_broadcast(result):
            from satorilib.wallet.concepts.transaction import TransactionFailure
            if isinstance(result, dict) and result.get('code') is not None:
                raise TransactionFailure(
                    f'broadcast rejected: {result.get("message", result)}')
            return result

        try:
            txid = _check_broadcast(await asyncio.to_thread(_build_standard_reclaim))
        except TransactionFailure as e:
            err = str(e).lower()
            # Chain-side rejections that Mundo fallback cannot help with
            if 'non-final' in err or 'nonfinal' in err:
                raise TransactionFailure(
                    'reclaim not yet valid on-chain — the CSV lock uses '
                    'median-time-past which lags real time by ~30-60 min. '
                    'The UI shows "expired" at the nominal timeout, but the '
                    'network will only accept the reclaim once enough blocks '
                    'have confirmed past that time. Try again later.')
            if 'broadcast rejected' in err:
                raise
            # Otherwise assume EVR gathering failed → PATH B via Mundo
            txid = await self._reclaimChannelViaMundo(
                channel=channel,
                redeem_script=redeem_script,
                csv_value=csv_value,
                sat_sats=sat_sats)

        # Zero the remainder so no payments are attempted against the spent UTXO.
        # The rest of the channel row (keys, redeem_script, etc.) stays intact.
        await asyncio.to_thread(
            self.networkDB.update_channel_remainder, p2sh_address, 0)
        logging.info(
            f'Channel: reclaimed {p2sh_address} — txid={txid}',
            color='yellow')
        # Refresh UTXOs so wallet balance reflects the reclaimed funds
        await asyncio.to_thread(self.wallet.getUnspents)
        return txid

    async def _reclaimChannelViaMundo(
        self,
        channel: dict,
        redeem_script,
        csv_value: int,
        sat_sats: int,
    ) -> str:
        """PATH B reclaim: sender has no EVR — pay SATORI fee to Mundo.

        Fix J — idempotent retry: same tier A/B/C cache pattern as
        _claimChannelViaMundo. Uses 'reclaim_' prefix in _mundoCache key.
        """
        import requests as _requests
        from evrmore.core import (
            CMutableTransaction, CMutableTxIn, CMutableTxOut, COutPoint, lx)
        from evrmore.core.script import (
            CScript, SIGHASH_ALL, SIGHASH_ANYONECANPAY,
            OP_EVR_ASSET, OP_DROP, OP_FALSE)
        from evrmore.wallet import CEvrmoreAddress
        from satorilib.wallet.concepts.transaction import AssetTransaction, TransactionFailure
        from satorilib.wallet.utils.transaction import TxUtils

        MUNDO_URL = os.environ.get('MUNDO_URL', 'https://mundo.satorinet.org')
        cache_key = f'reclaim_{channel["p2sh_address"]}'
        cached = self._mundoCache.get(cache_key, {})

        # ── Tier A: fully-signed tx cached ──────────────────────────────
        if cached.get('signed_hex'):
            logging.info(
                f'Channel: Mundo reclaim retry tier A for '
                f'{channel["p2sh_address"]}', color='cyan')
            try:
                result = await asyncio.to_thread(
                    self.wallet.broadcast, cached['signed_hex'])
                if isinstance(result, dict) and result.get('code') is not None:
                    raise TransactionFailure(
                        f'broadcast rejected: {result.get("message", result)}')
                self._mundoCache.pop(cache_key, None)
                return result
            except Exception as e:
                logging.warning(
                    f'Channel: cached reclaim tx failed: {e} — rebuilding')
                self._mundoCache.pop(cache_key, None)

        # ── Tier B: incomplete tx cached, re-post to Mundo ──────────────
        if cached.get('incomplete_hex') and cached.get('fee_sats_reserved'):
            logging.info(
                f'Channel: Mundo reclaim retry tier B for '
                f'{channel["p2sh_address"]}', color='cyan')
            try:
                def _mundo_post_cached():
                    resp = _requests.post(
                        f'{MUNDO_URL}/simple_partial/broadcast/evrmore'
                        f'/{cached["fee_sats_reserved"]}/{cached["fee_sats"]}/0',
                        params={'signOnly': 'true'},
                        data=cached['incomplete_hex'],
                        headers={'Content-Type': 'text/plain'},
                        timeout=30)
                    resp.raise_for_status()
                    return resp.text
                signed_hex = await asyncio.to_thread(_mundo_post_cached)
                self._mundoCache[cache_key] = {
                    **cached, 'signed_hex': signed_hex}
                result = await asyncio.to_thread(
                    self.wallet.broadcast, signed_hex)
                if isinstance(result, dict) and result.get('code') is not None:
                    raise TransactionFailure(
                        f'broadcast rejected: {result.get("message", result)}')
                self._mundoCache.pop(cache_key, None)
                return result
            except Exception as e:
                logging.warning(
                    f'Channel: Mundo reclaim tier B failed: {e} — full rebuild')
                self._mundoCache.pop(cache_key, None)

        # ── Tier C: full build ──────────────────────────────────────────

        # Step 1: find sender's SATORI UTXO
        satori_utxo = None
        for u in sorted(
            [u for u in (self.wallet.unspentAssets or [])
             if u.get('name', u.get('asset')) == 'SATORI'
             and u.get('value', 0) > 0],
            key=lambda x: x.get('value', 0)
        ):
            satori_utxo = u
            break
        if not satori_utxo:
            raise ValueError(
                f'Channel {channel["p2sh_address"]}: no EVR and no SATORI — '
                f'cannot cover the mining fee to reclaim this channel')

        # Step 2: compute final tx shape for Mundo fee request
        # Inputs: 1 P2SH + 1 SATORI + 1 Mundo EVR
        # Outputs: 1 SATORI→sender + 1 SATORI fee + 1 SATORI change + 1 EVR change
        final_input_count = 3
        final_output_count = 4

        def _request_mundo():
            resp = _requests.get(
                f'{MUNDO_URL}/simple_partial/request/evrmore',
                params={
                    'inputCount': final_input_count,
                    'outputCount': final_output_count},
                timeout=15)
            resp.raise_for_status()
            return resp.json()

        mundo_data = await asyncio.to_thread(_request_mundo)
        mundo_satori_fee = int(mundo_data['satoriFeeAmount'])
        mundo_satori_fee_addr = mundo_data['satoriFeeAddress']
        mundo_evr_change_addr = mundo_data.get('changeAddress', '')
        mundo_evr_change_amt = int(mundo_data.get('changeAmount', 0))
        fee_sats_reserved = int(mundo_data['feeSatsReserved'])
        fee_sats = int(mundo_data['feeSats'])

        satori_value = satori_utxo['value']
        if satori_value < mundo_satori_fee:
            raise ValueError(
                f'Channel: SATORI UTXO ({satori_value} sats) < '
                f'Mundo fee ({mundo_satori_fee} sats)')

        # Step 3: build tx
        p2sh_txin = CMutableTxIn(
            COutPoint(lx(channel['funding_txid']), channel['funding_vout']))
        satori_txin = CMutableTxIn(
            COutPoint(lx(satori_utxo['tx_hash']), satori_utxo['tx_pos']))
        satori_vin_idx = 1  # vin[0]=P2SH, vin[1]=SATORI

        def _satori_asset_script(address: str, amount_sats: int) -> CScript:
            return CScript([
                *CEvrmoreAddress(address).to_scriptPubKey(),
                OP_EVR_ASSET,
                bytes.fromhex(
                    AssetTransaction.satoriHex(self.wallet.symbol) +
                    TxUtils.padHexStringTo8Bytes(
                        TxUtils.intToLittleEndianHex(amount_sats))),
                OP_DROP])

        vouts = [
            # SATORI back to sender
            CMutableTxOut(0, _satori_asset_script(self.wallet.address, sat_sats)),
            # SATORI fee to Mundo
            CMutableTxOut(0, _satori_asset_script(mundo_satori_fee_addr, mundo_satori_fee)),
        ]
        satori_change = satori_value - mundo_satori_fee
        if satori_change > 0:
            vouts.append(CMutableTxOut(
                0, _satori_asset_script(self.wallet.address, satori_change)))
        if mundo_evr_change_addr and mundo_evr_change_amt > 0:
            vouts.append(CMutableTxOut(
                mundo_evr_change_amt,
                CEvrmoreAddress(mundo_evr_change_addr).to_scriptPubKey()))

        new_tx = CMutableTransaction([p2sh_txin, satori_txin], vouts)

        # Step 4: set CSV fields BEFORE signing
        new_tx.nVersion = 2
        new_tx.vin[0].nSequence = csv_value

        # Step 5: sign P2SH vin[0] with 0x81 (CSV single-sig branch)
        mundo_sighash = SIGHASH_ALL | SIGHASH_ANYONECANPAY  # 0x81
        p2sh_sig = await asyncio.to_thread(
            self.wallet.paymentChannelMultisigTransactionMiddle,
            new_tx, redeem_script, 0, mundo_sighash)
        new_tx.vin[0].scriptSig = CScript([p2sh_sig, OP_FALSE]) + redeem_script

        # Step 6: sign SATORI vin[1] with 0x81
        _, satori_scripts = await asyncio.to_thread(
            self.wallet._compileInputs,
            [],              # gatheredCurrencyUnspents
            [satori_utxo],  # gatheredSatoriUnspents
        )
        await asyncio.to_thread(
            self.wallet._signInput,
            new_tx, satori_vin_idx, satori_txin,
            satori_scripts[0], mundo_sighash)

        # Cache the signed incomplete tx (tier B on retry)
        incomplete_hex = new_tx.serialize().hex()
        self._mundoCache[cache_key] = {
            'incomplete_hex': incomplete_hex,
            'fee_sats_reserved': fee_sats_reserved,
            'fee_sats': fee_sats,
        }

        # Step 7: POST to Mundo (signOnly=true)
        def _broadcast_via_mundo():
            resp = _requests.post(
                f'{MUNDO_URL}/simple_partial/broadcast/evrmore'
                f'/{fee_sats_reserved}/{fee_sats}/0',
                params={'signOnly': 'true'},
                data=incomplete_hex,
                headers={'Content-Type': 'text/plain'},
                timeout=30)
            resp.raise_for_status()
            return resp.text

        signed_hex = await asyncio.to_thread(_broadcast_via_mundo)
        # Cache the fully-signed tx (tier A on retry)
        self._mundoCache[cache_key] = {
            **self._mundoCache.get(cache_key, {}), 'signed_hex': signed_hex}

        # Step 8: broadcast the fully-signed tx
        result = await asyncio.to_thread(self.wallet.broadcast, signed_hex)
        if isinstance(result, dict) and result.get('code') is not None:
            raise TransactionFailure(
                f'broadcast rejected: {result.get("message", result)}')
        self._mundoCache.pop(cache_key, None)
        return result

    # ── End channel support ───────────────────────────────────────────────────

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
                # Include our Satori wallet pubkey so subscribers can auto-open
                # payment channels without any manual configuration.
                meta_dict = source or {}
                if hasattr(self, 'wallet') and self.wallet and self.wallet.pubkey:
                    meta_dict['wallet_pubkey'] = self.wallet.pubkey
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
                    metadata=meta_dict or None,
                )
                await client.announce_datastream(metadata)
                logging.info(
                    f'Network: announced {pub["stream_name"]} on '
                    f'{relay_url}', color='green')
            except Exception as e:
                logging.warning(
                    f'Network: announce failed {pub["stream_name"]}: {e}')

    async def _networkPublishObservation(self, stream_name: str, value) -> int:
        """Publish an observation to all connected relays.

        Returns the number of relays that accepted the publish. Zero means
        the seq_num advanced locally but nothing reached the network — the
        caller should surface that as a warning rather than a success.
        """
        from satorilib.satori_nostr.models import (
            DatastreamObservation, DatastreamMetadata)
        pub = await asyncio.to_thread(
            lambda: next(
                (p for p in self.networkDB.get_active_publications()
                 if p['stream_name'] == stream_name), None))
        if not pub:
            logging.warning(
                f'Network: cannot publish {stream_name}: not registered')
            return 0
        seq_num = await asyncio.to_thread(
            self.networkDB.mark_published, stream_name)
        ts = int(time.time())
        await asyncio.to_thread(
            self.networkDB.save_observation,
            stream_name, self.nostrPubkey, str(value), None, seq_num, ts)
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
        delivered = 0
        for relay_url, client in list(self._networkClients.items()):
            try:
                await client.publish_observation(observation, metadata)
                delivered += 1
                logging.info(
                    f'Network: published {stream_name} seq={seq_num} '
                    f'value={value} to {relay_url}', color='green')
            except Exception as e:
                logging.warning(
                    f'Network: publish failed on {relay_url}: {e}')
        if delivered == 0:
            logging.warning(
                f'Network: {stream_name} seq={seq_num} not delivered '
                f'(no connected relays) — seq advanced locally only')
        return delivered

    def publishObservation(self, stream_name: str, value):
        """Publish an observation from a sync context (e.g. Flask route, engine).

        Broadcasts to all connected relays. Non-blocking.
        """
        if not hasattr(self, '_networkSecretHex'):
            return
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            logging.warning('Network: cannot publish — network loop not running')
            return
        asyncio.run_coroutine_threadsafe(
            self._networkPublishObservation(stream_name, value),
            loop)

    def publishUnsubscribeSync(
        self, stream_name: str, provider_pubkey: str
    ) -> None:
        """Publish an unsubscribe announcement to all connected relays (Fix G).

        Non-blocking — submits to the network event loop.
        """
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            logging.warning(
                'Network: cannot publish unsubscribe — loop not running')
            return
        asyncio.run_coroutine_threadsafe(
            self._publishUnsubscribeNow(stream_name, provider_pubkey),
            loop)

    async def _publishUnsubscribeNow(
        self, stream_name: str, provider_pubkey: str
    ) -> None:
        """Publish unsubscribe to all connected relays."""
        for relay_url, client in self._networkClients.items():
            try:
                await client.unsubscribe_datastream(
                    stream_name, provider_pubkey)
                logging.info(
                    f'Network: published unsubscribe for {stream_name} '
                    f'on {relay_url}', color='green')
            except Exception as e:
                logging.warning(
                    f'Network: unsubscribe publish failed on {relay_url}: {e}')

    def tombstonePublicationSync(self, stream_name: str):
        """Publish a tombstone (deleted) Kind 34600 announcement for a removed
        publication to all known relays. Non-blocking — runs in a background thread.

        Replaces the original announcement so other nodes stop discovering the stream.
        """
        from satorilib.satori_nostr import SatoriNostrConfig
        if not hasattr(self, '_networkSecretHex'):
            return
        def run():
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    self._tombstoneNow(stream_name, SatoriNostrConfig))
            finally:
                loop.close()
        threading.Thread(target=run, daemon=True).start()

    async def _tombstoneNow(self, stream_name: str, ConfigClass):
        """Connect to all known relays and publish a tombstone for stream_name."""
        from satorilib.satori_nostr.models import DatastreamMetadata
        relay_urls = []
        try:
            server_relays = await asyncio.to_thread(self.server.getRelays)
            relay_urls = [r['relay_url'] for r in server_relays]
        except Exception:
            pass
        db_relays = await asyncio.to_thread(self.networkDB.get_relays)
        for r in db_relays:
            if r['relay_url'] not in relay_urls:
                relay_urls.append(r['relay_url'])
        # Minimal metadata — only stream_name and nostr_pubkey matter for the tombstone
        metadata = DatastreamMetadata(
            stream_name=stream_name,
            nostr_pubkey=self.nostrPubkey,
            name='',
            description='',
            encrypted=False,
            price_per_obs=0,
            created_at=0,
            cadence_seconds=None,
            tags=[],
        )
        for relay_url in relay_urls:
            try:
                client = await self._networkConnect(relay_url, ConfigClass)
                if not client:
                    continue
                await client.announce_datastream(metadata, deleted=True)
                logging.info(
                    f'Network: tombstoned {stream_name} on {relay_url}',
                    color='yellow')
            except Exception as e:
                logging.warning(
                    f'Network: tombstone failed on {relay_url} for {stream_name}: {e}')
            finally:
                if relay_url not in self._neededRelays():
                    await self._networkDisconnect(relay_url)

    def publishNowSync(self, stream_name: str, value: str):
        """Connect to all known relays, announce publications, and publish one
        observation. Non-blocking — runs in a background thread.

        Used on first save so the stream is immediately visible on the relay
        even when no subscriptions are keeping connections open.
        """
        from satorilib.satori_nostr import SatoriNostrConfig
        if not hasattr(self, '_networkSecretHex'):
            return
        loop = getattr(self, '_networkLoop', None)
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self._publishNow(stream_name, value, SatoriNostrConfig),
                loop)
            return
        def run():
            new_loop = asyncio.new_event_loop()
            try:
                new_loop.run_until_complete(
                    self._publishNow(stream_name, value, SatoriNostrConfig))
            finally:
                new_loop.close()
        threading.Thread(target=run, daemon=True).start()

    async def _publishNow(self, stream_name: str, value: str, ConfigClass):
        """Connect to all known relays, announce publications, publish observation."""
        relay_urls = []
        try:
            server_relays = await asyncio.to_thread(self.server.getRelays)
            relay_urls = [r['relay_url'] for r in server_relays]
        except Exception:
            pass
        db_relays = await asyncio.to_thread(self.networkDB.get_relays)
        for r in db_relays:
            if r['relay_url'] not in relay_urls:
                relay_urls.append(r['relay_url'])
        for relay_url in relay_urls:
            try:
                client = await self._networkConnect(relay_url, ConfigClass)
                if not client:
                    continue
                await self._networkAnnouncePublications(relay_url)
                await self._networkPublishObservation(stream_name, value)
                await asyncio.sleep(2)  # allow relay to acknowledge before loop closes
                logging.info(
                    f'Network: publish-now {stream_name} to {relay_url}',
                    color='green')
            except Exception as e:
                logging.warning(
                    f'Network: publish-now failed on {relay_url}: {e}')
            finally:
                if relay_url not in self._neededRelays():
                    await self._networkDisconnect(relay_url)

    async def _networkDataSourceManager(self):
        """Reconcile per-source fetch tasks with the DB every 60s.

        Each active data source gets its own asyncio.Task that fires at
        exactly its cadence.  This manager spawns new tasks, cancels tasks
        for removed/deactivated sources, and respawns crashed workers.
        """
        while True:
            try:
                sources = await asyncio.to_thread(
                    self.networkDB.get_active_data_sources)
                pubs = await asyncio.to_thread(
                    self.networkDB.get_active_publications)
                pub_map = {p['stream_name']: p for p in pubs}

                desired = {}  # stream_name -> (source_row, pub_row)
                for src in sources:
                    sn = src['stream_name']
                    if not src.get('url') or not src.get('cadence_seconds'):
                        continue
                    pub = pub_map.get(sn)
                    if not pub:
                        continue
                    desired[sn] = (src, pub)

                # Cancel tasks for sources no longer desired
                for sn in list(self._dataSourceTasks):
                    if sn not in desired:
                        task, _ = self._dataSourceTasks.pop(sn)
                        task.cancel()

                # Spawn or respawn tasks
                for sn, (src, pub) in desired.items():
                    cadence = src['cadence_seconds']
                    existing = self._dataSourceTasks.get(sn)
                    if existing is not None:
                        task, prev_cadence = existing
                        if not task.done() and prev_cadence == cadence:
                            continue  # healthy, no change
                        task.cancel()  # crashed or cadence changed
                    new_task = asyncio.create_task(
                        self._networkDataSourceWorker(src, pub))
                    self._dataSourceTasks[sn] = (new_task, cadence)

            except Exception as e:
                logging.error(f'Network data source manager error: {e}')
            await asyncio.sleep(60)

    async def _networkDataSourceWorker(self, src: dict, pub: dict):
        """Fetch a single data source at its exact cadence.

        Scheduling is clock-anchored to UTC: the fire-time grid is
        defined by offset_seconds (position within each cadence cycle
        relative to UTC epoch 0).  Drift never accumulates regardless
        of fetch duration or sleep overshoot.
        """
        import json as json_mod
        import requests as http_requests

        stream_name = src['stream_name']
        cadence = src['cadence_seconds']
        offset = src.get('offset_seconds') or 0

        # Build the UTC-anchored fire grid:
        # fire at every T where T % cadence == offset % cadence
        now = time.time()
        remainder = offset % cadence
        next_fire = now - (now % cadence) + remainder
        if next_fire <= now:
            next_fire += cadence
        # Don't re-fire a cycle we already published in
        last = pub.get('last_published_at') or 0
        while next_fire <= last:
            next_fire += cadence

        while True:
            delay = max(0, next_fire - time.time())
            await asyncio.sleep(delay)
            await self._networkFetchOneDataSource(src)
            next_fire += cadence

    async def _networkFetchOneDataSource(self, src: dict):
        """Fetch, parse, and publish a single data source once.

        Returns True on successful publish, False otherwise.
        """
        import json as json_mod
        import requests as http_requests

        stream_name = src['stream_name']

        # -- Fetch --
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
            return False

        # -- Parse --
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
                return False
        except Exception as e:
            logging.warning(
                f'Network: parse failed for {stream_name}: {e}')
            return False

        # -- Publish --
        try:
            delivered = await self._networkPublishObservation(
                stream_name, value)
            if delivered > 0:
                logging.info(
                    f'Network: data source {stream_name} published '
                    f'to {delivered} relay(s)',
                    color='green')
            else:
                logging.warning(
                    f'Network: data source {stream_name} parsed but not '
                    f'delivered — no connected relays')
            return delivered > 0
        except Exception as e:
            logging.warning(
                f'Network: publish failed for {stream_name}: {e}')
            return False

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
                streams = await client.discover_datastreams(limit=1000)
                # Run freshness checks concurrently — each is a network RTT.
                results = await asyncio.gather(
                    *[self._networkCheckFreshness(client, s.stream_name, s)
                      for s in streams],
                    return_exceptions=True)
                for s, res in zip(streams, results):
                    d = s.to_dict()
                    d['relay_url'] = relay_url
                    if isinstance(res, Exception):
                        d['last_observation_at'] = None
                        d['active'] = False
                    else:
                        d['last_observation_at'], d['active'] = res
                    all_streams.append(d)
                logging.info(
                    f'Network discover: {len(streams)} streams from '
                    f'{relay_url}', color='green')
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
            streams = await client.discover_datastreams(limit=1000)
            results = await asyncio.gather(
                *[self._networkCheckFreshness(client, s.stream_name, s)
                  for s in streams],
                return_exceptions=True)
            for s, res in zip(streams, results):
                d = s.to_dict()
                d['relay_url'] = relay_url
                if isinstance(res, Exception):
                    d['last_observation_at'] = None
                    d['active'] = False
                else:
                    d['last_observation_at'], d['active'] = res
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
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            return []
        future = asyncio.run_coroutine_threadsafe(
            self._networkDiscoverRelay(relay_url, SatoriNostrConfig), loop)
        return future.result(timeout=30)

    def _neededRelays(self) -> set:
        """Return set of relay URLs that have active subscriptions."""
        subs = self.networkDB.get_active()
        return {s['relay_url'] for s in subs}

    def scheduleChannelPay(
        self, stream_name: str, provider_pubkey: str, price_sats: int
    ):
        """Trigger an initial channel payment from a sync context (e.g. Flask).

        Called when subscribing to a paid stream so the subscriber pre-pays
        before any observations arrive, breaking the circular dependency where
        observations require payment and payment requires observations.
        Fire-and-forget.
        """
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._channelPayNow(stream_name, provider_pubkey, price_sats),
            loop)

    def triggerNetworkDiscover(self):
        """Trigger on-demand discovery from a sync context (e.g. Flask route).

        Schedules the coroutine on the live network event loop so it shares
        the existing WS clients. Fire-and-forget.
        """
        from satorilib.satori_nostr import SatoriNostrConfig
        if not hasattr(self, '_networkSecretHex'):
            return
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(
            self._networkDiscover(SatoriNostrConfig), loop)

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
        # Don't subscribe to streams we publish ourselves
        my_pub_names = {
            p['stream_name']
            for p in await asyncio.to_thread(self.networkDB.get_active_publications)
        }
        desired = [s for s in desired if s['stream_name'] not in my_pub_names]
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

        # 3. Build hunt list: inactive subs not recently marked stale.
        # Paid subs bypass the cooldown — an open channel is an authoritative
        # declaration of intent, and relay freshness for a paid stream is a
        # chicken-and-egg signal (see Fix M).
        hunting = {}  # stream_name -> sub dict
        for sub in inactive:
            stale_since = sub.get('stale_since')
            is_paid = int(sub.get('price_per_obs', 0) or 0) > 0
            if (stale_since and not is_paid
                    and not self.networkDB.should_recheck_stale(stale_since)):
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
                streams = await client.discover_datastreams(limit=1000)
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
                # Paid subscriptions: skip freshness — the provider only
                # publishes to known subscribers, so the relay may have no
                # recent events even though the provider is alive. Connect
                # and announce so the provider learns about us again.
                sub_info = hunting.get(stream_name, {})
                is_paid = int(sub_info.get('price_per_obs', 0) or 0) > 0
                if not is_paid:
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
                # Refresh price from the provider's metadata
                discovered_price = getattr(metadata, 'price_per_obs', None)
                if discovered_price is not None:
                    await asyncio.to_thread(
                        self.networkDB.update_subscription_price,
                        stream_name, sub['provider_pubkey'],
                        discovered_price)
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
        self.localRelay.ensure_state_async()
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
        self.localRelay.ensure_state_async()
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
                    nostrPubkey=self.nostrPubkey,
                    version=VERSION)

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
