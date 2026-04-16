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
        self._predictionListeners: dict = {}  # relay_url -> asyncio.Task
        self._accessRequestListeners: dict = {}  # relay_url -> asyncio.Task
        self._settledChannels: set = set()  # p2sh addresses settled this session (race guard)
        self._paymentCooldowns: dict = {}  # (stream, provider) -> last payment timestamp
        self._paymentDeferred: dict = {}   # (stream, provider) -> asyncio.TimerHandle
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
        """Reconciliation loop: ensures we are subscribed to all desired streams
        and fetches data sources on their cadence.

        Every 5 minutes:
        1. Reconcile subscriptions (connect, discover, subscribe)
        2. Ensure relay connections exist for active publications
        3. Fetch any data sources that are due
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
        self._predictionListeners.clear()
        self._accessRequestListeners.clear()
        self._settledChannels.clear()
        self._networkSubscribed.clear()
        self._networkFirstRun = True

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
                await self._networkFetchDataSources()
            except Exception as e:
                logging.error(f'Network data source fetch error: {e}')
            try:
                await self._channelExpiryCheck()
            except Exception as e:
                logging.error(f'Channel expiry check error: {e}')
            await asyncio.sleep(300)

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
            # Hosts need prediction + access-request listeners on every
            # relay where they publish, even when the reconcile loop
            # (subscription path) never touches that relay.
            if relay_url in self._networkClients:
                self._networkEnsurePredictionListener(relay_url)
                self._networkEnsureAccessRequestListener(relay_url)

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
        ptask = self._predictionListeners.pop(relay_url, None)
        if ptask and not ptask.done():
            ptask.cancel()
        artask = self._accessRequestListeners.pop(relay_url, None)
        if artask and not artask.done():
            artask.cancel()
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
            # Score competition predictions and pay predictors if we host one
            await self._competitionScoreAndPay(
                stream_name=obs.stream_name,
                provider_pubkey=obs.nostr_pubkey,
                seq_num=obs.observation.seq_num,
                raw_value=obs.observation.value,
            )

    async def _competitionScoreAndPay(
        self,
        stream_name: str,
        provider_pubkey: str,
        seq_num: int,
        raw_value,
    ) -> None:
        """Score predictions and pay winners when an observation arrives (host side).

        Only runs if this node is the host of an active competition for the stream.
        Delegates scoring logic to competition_scoring.compute_payouts(), then
        calls _competitionPayPredictor for each non-zero payout.

        raw_value is the observation value as-is; the cast to float happens
        only after the competition-host gate, so non-numeric streams do not
        crash the observation pipeline.
        """
        try:
            competition = await asyncio.to_thread(
                self.networkDB.get_competition,
                stream_name, provider_pubkey, self.nostrPubkey)
            if not competition or not competition.get('active'):
                return

            # Dedup: skip if we've already scored this seq_num
            already_scored = await asyncio.to_thread(
                self.networkDB.is_seq_already_scored,
                stream_name, provider_pubkey, seq_num)
            if already_scored:
                return

            try:
                observed_value = float(raw_value)
            except (TypeError, ValueError):
                logging.warning(
                    f'Competition: skipping score for {stream_name} seq={seq_num} '
                    f'— non-numeric observation value')
                return

            predictions = await asyncio.to_thread(
                self.networkDB.get_competition_predictions,
                stream_name, provider_pubkey, seq_num)
            if not predictions:
                return

            # Fetch observation timestamps so scoring modules can enforce
            # timing windows (e.g. disqualify predictions submitted too
            # close to the observation they're predicting).
            cur_obs = await asyncio.to_thread(
                self.networkDB.get_observation_by_seq,
                stream_name, provider_pubkey, seq_num)
            observed_at = (cur_obs or {}).get('observed_at', 0)
            prev_obs = None
            if seq_num > 1:
                prev_obs = await asyncio.to_thread(
                    self.networkDB.get_observation_by_seq,
                    stream_name, provider_pubkey, seq_num - 1)
            prev_observed_at = (prev_obs or {}).get('observed_at', 0)

            from satorineuron.competition_scoring import compute_payouts
            payouts = await asyncio.to_thread(
                compute_payouts, competition, predictions, observed_value,
                observed_at, prev_observed_at)

            # Index predictions by pubkey so we can look up wallet pubkey
            preds_by_pubkey = {
                row['predictor_pubkey']: row for row in predictions}

            for pubkey, sats in payouts.items():
                row = preds_by_pubkey.get(pubkey)
                wallet_pubkey = (row or {}).get('predictor_wallet_pubkey')
                await self._competitionPayPredictor(
                    pubkey, wallet_pubkey, sats,
                    stream_name, provider_pubkey, seq_num)
        except Exception as e:
            logging.warning(f'Competition: score-and-pay failed: {e}')

    async def _competitionScoreLateArrival(
        self,
        stream_name: str,
        provider_pubkey: str,
        seq_num: int,
    ) -> None:
        """Re-run scoring when a prediction arrives after its observation.

        No-op if the observation hasn't been received yet (the normal
        _networkProcessObservation path will score once it arrives) or
        if this node isn't hosting the competition.
        """
        try:
            observation = await asyncio.to_thread(
                self.networkDB.get_observation_by_seq,
                stream_name, provider_pubkey, seq_num)
        except Exception:
            return
        if not observation:
            return
        raw = observation.get('value')
        if raw is None:
            return
        # Observations are stored as the JSON-serialised payload; try to
        # extract the numeric value whether the row was saved as raw or JSON.
        candidate = raw
        try:
            import json as _json
            parsed = _json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict) and 'value' in parsed:
                candidate = parsed['value']
            else:
                candidate = parsed
        except (ValueError, TypeError):
            pass
        await self._competitionScoreAndPay(
            stream_name=stream_name,
            provider_pubkey=provider_pubkey,
            seq_num=seq_num,
            raw_value=candidate,
        )

    async def _competitionPayPredictor(
        self,
        predictor_nostr_pubkey: str,
        predictor_wallet_pubkey: Optional[str],
        sats: int,
        stream_name: str,
        provider_pubkey: str,
        seq_num: int,
    ) -> None:
        """Score and pay a predictor for a competition (host side).

        Always records the payment obligation in the DB (for leaderboard /
        accountability) even if the actual channel payment fails.  The channel
        payment is best-effort — wallet balance or network issues should not
        prevent scoring results from being persisted.
        """
        import time as _time
        # Always record the scoring result first
        try:
            await asyncio.to_thread(
                self.networkDB.record_competition_payment,
                stream_name,
                provider_pubkey,
                predictor_nostr_pubkey,
                seq_num,
                sats,
                int(_time.time()),
            )
            logging.info(
                f'Competition: scored {sats} sats for '
                f'{predictor_nostr_pubkey[:12]}… seq={seq_num}',
                color='green')
        except Exception as e:
            logging.warning(
                f'Competition: failed to record payment for '
                f'{predictor_nostr_pubkey[:12]}…: {e}')
            return

        # Now attempt the actual channel payment (best-effort)
        if not predictor_wallet_pubkey:
            logging.warning(
                f'Competition: no wallet pubkey for predictor '
                f'{predictor_nostr_pubkey[:12]}…, payment recorded but '
                f'channel transfer skipped')
            return
        try:
            # Find an open sender channel with enough remainder
            channels = await asyncio.to_thread(
                self.networkDB.get_channels_as_sender)
            channel = next(
                (c for c in channels
                 if c['receiver_pubkey'] == predictor_wallet_pubkey
                 and c['remainder_sats'] >= sats),
                None)
            if not channel:
                fund_sats = self._channelFundSats()
                timeout_minutes = self._channelTimeoutMinutes()
                logging.info(
                    f'Competition: opening channel to predictor '
                    f'{predictor_wallet_pubkey[:16]}… '
                    f'fund={fund_sats} sats',
                    color='cyan')
                p2sh = await self.openChannel(
                    receiver_pubkey=predictor_wallet_pubkey,
                    amount_sats=fund_sats,
                    minutes=timeout_minutes,
                    receiver_nostr_pubkey=predictor_nostr_pubkey,
                )
                channel = await asyncio.to_thread(
                    self.networkDB.get_channel, p2sh)
                if not channel or channel['remainder_sats'] < sats:
                    logging.warning(
                        f'Competition: channel insufficient for '
                        f'{sats} sats to {predictor_wallet_pubkey[:16]}… '
                        f'(payment recorded, transfer pending)')
                    return

            await self.sendChannelPayment(
                channel['p2sh_address'], sats, stream_name)
            logging.info(
                f'Competition: channel payment sent — {sats} sats to '
                f'{predictor_nostr_pubkey[:12]}… for seq={seq_num}',
                color='green')
        except Exception as e:
            logging.warning(
                f'Competition: channel transfer failed for '
                f'{predictor_nostr_pubkey[:12]}… (payment recorded, '
                f'transfer pending): {e}')

    async def _channelPayForObservation(
        self,
        stream_name: str,
        provider_pubkey: str,
        seq_num: int,
    ) -> None:
        """Pay for an observation via a channel (sender/buyer side).

        Rate-limited: never pays more than once per cadence/2 seconds.
        If an observation arrives during the cooldown, schedules exactly one
        deferred payment at cooldown end. The deferred payment signals the
        seller that the buyer is still subscribed. Streams with no cadence
        (null/0) are not rate-limited.
        """
        try:
            sub = await asyncio.to_thread(
                self.networkDB.is_subscribed, stream_name, provider_pubkey)
            if not sub:
                return
            conn_rows = await asyncio.to_thread(self.networkDB.get_active)
            subscription = next(
                (s for s in conn_rows
                 if s['stream_name'] == stream_name
                 and s['provider_pubkey'] == provider_pubkey),
                None)
            if not subscription or subscription.get('price_per_obs', 0) == 0:
                return  # free stream
            price_sats = subscription['price_per_obs']
            cadence = subscription.get('cadence_seconds') or 0
            cooldown = cadence / 2 if cadence > 0 else 0
            key = (stream_name, provider_pubkey)
            now = time.time()
            last_paid = self._paymentCooldowns.get(key, 0)
            if cooldown > 0 and (now - last_paid) < cooldown:
                # Inside cooldown — schedule one deferred payment at cooldown end
                if key not in self._paymentDeferred:
                    delay = cooldown - (now - last_paid)
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
        """
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
        if channel and channel['remainder_sats'] < price_sats:
            fund_sats = self._channelFundSats()
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
            fund_sats = self._channelFundSats()
            timeout_minutes = self._channelTimeoutMinutes()
            logging.info(
                f'Channel: auto-opening to {provider_wallet_pubkey[:16]}… '
                f'fund={fund_sats} sats timeout={timeout_minutes} min',
                color='cyan')
            p2sh = await self.openChannel(
                receiver_pubkey=provider_wallet_pubkey,
                amount_sats=fund_sats,
                minutes=timeout_minutes,
                receiver_nostr_pubkey=provider_pubkey,
            )
            channel = await asyncio.to_thread(
                self.networkDB.get_channel, p2sh)
            if not channel or channel['remainder_sats'] < price_sats:
                return
        await self.sendChannelPayment(
            channel['p2sh_address'], price_sats, stream_name)
        key = (stream_name, provider_pubkey)
        self._paymentCooldowns[key] = time.time()

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

        # Submit this prediction privately (encrypted DM) to every competition
        # host we've joined for this stream. The engine's prediction is public
        # via {stream}_pred above; competition DMs are in addition to that,
        # private to each host.
        try:
            joined = await asyncio.to_thread(
                self.networkDB.get_joined_competitions_for_stream,
                stream_name, provider_pubkey)
            if joined:
                try:
                    predicted_float = float(value_str)
                except (TypeError, ValueError):
                    predicted_float = None
                if predicted_float is not None:
                    for j in joined:
                        await self.submitPrediction(
                            stream_name=stream_name,
                            stream_provider_pubkey=provider_pubkey,
                            host_pubkey=j['host_pubkey'],
                            seq_num=observation.seq_num,
                            predicted_value=predicted_float,
                        )
        except Exception as e:
            logging.warning(
                f'Network: competition DM submit failed for {stream_name}: {e}')

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
        self._networkEnsurePredictionListener(relay_url)
        self._networkEnsureAccessRequestListener(relay_url)

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
        try:
            async for inbound in client.settlements():
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
            # Update to new UTXO; cumulative tracking resets to 0
            await asyncio.to_thread(
                self.networkDB.update_channel_funding,
                s.p2sh_address,
                s.claim_txid,
                s.new_funding_vout,
                s.new_locked_sats)
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

    def _networkEnsurePredictionListener(self, relay_url: str):
        """Start an incoming-predictions listener for a relay if one isn't running."""
        task = self._predictionListeners.get(relay_url)
        if task and not task.done():
            return
        client = self._networkClients.get(relay_url)
        if not client:
            return
        self._predictionListeners[relay_url] = asyncio.ensure_future(
            self._incomingPredictionsLoop(client, relay_url))

    def _networkEnsureAccessRequestListener(self, relay_url: str):
        """Start an incoming-access-requests listener for a relay if one isn't running."""
        task = self._accessRequestListeners.get(relay_url)
        if task and not task.done():
            return
        client = self._networkClients.get(relay_url)
        if not client:
            return
        self._accessRequestListeners[relay_url] = asyncio.ensure_future(
            self._incomingAccessRequestsLoop(client, relay_url))

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
        """
        if p2sh_address in self._settledChannels:
            self._settledChannels.discard(p2sh_address)
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
        if existing:
            return  # already know about this channel
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
        )
        logging.info(
            f'Channel: registered inbound channel {co.p2sh_address} '
            f'from {co.sender_pubkey[:16]}…',
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
            logging.warning(
                f'Channel: commitment for unknown channel '
                f'{commitment.p2sh_address} — ignoring')
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
        channel = await asyncio.to_thread(
            self.networkDB.get_channel, p2sh_address)
        if not channel:
            raise ValueError(f'Unknown channel: {p2sh_address}')
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

        if deficit <= 0:
            # ── PATH A: fee already embedded ────────────────────────────────
            our_sig = await asyncio.to_thread(
                self.wallet.paymentChannelMultisigTransactionMiddle,
                tx, redeem_script, 0, SIGHASH_ALL)
            await asyncio.to_thread(
                self.wallet._compileClaimOnP2SHMultiSigEnd,
                tx, redeem_script, _make_redeem_params(our_sig), 1, None)
            tx_hex = tx.serialize().hex()
            txid = await asyncio.to_thread(self.wallet.broadcast, tx_hex)
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
                txid = await asyncio.to_thread(self.wallet.broadcast, tx_hex)
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
        # ── Grant access for exactly this payment, not cumulative total ───────
        await self._grantChannelAccess(
            sender_nostr_pubkey=channel.get('sender_nostr_pubkey'),
            total_paid_sats=commitment.pay_amount_sats,
            stream_name=commitment.stream_name)
        # ── Post-broadcast: update DB ───────────────────────────────────────
        logging.info(
            f'Channel: claimed {commitment.pay_amount_sats} sats '
            f'from {p2sh_address} — txid={txid}',
            color='green')
        # Find the P2SH change output (change goes back to P2SH for next round)
        redeem_bytes = bytes.fromhex(channel['redeem_script'])
        script_hash = hashlib.new(
            'ripemd160', hashlib.sha256(redeem_bytes).digest()).digest()
        p2sh_script = bytes([0xa9, 0x14]) + script_hash + bytes([0x87])
        change_vout = -1
        # SATORI outputs have nValue=0 in the Python Evrmore library;
        # use commitment.remainder_sats for the canonical SATORI amount.
        change_sats = commitment.remainder_sats
        # Determine which vout carries the P2SH change by script comparison.
        # For PATH A/B we have tx in memory; for PATH C rebuild from commitment.
        check_tx = CMutableTransaction.deserialize(
            bytes.fromhex(commitment.partial_tx_hex))
        for i, out in enumerate(check_tx.vout):
            if bytes(out.scriptPubKey) == p2sh_script:
                change_vout = i
                break
        if change_vout >= 0 and change_sats > 0:
            await asyncio.to_thread(
                self.networkDB.update_channel_funding,
                p2sh_address, txid, change_vout, change_sats)
            logging.info(
                f'Channel: {p2sh_address} new UTXO '
                f'{txid[:12]}…:{change_vout} ({change_sats} sats)',
                color='cyan')
        else:
            # No change output — channel fully drained on receiver side
            logging.info(
                f'Channel: {p2sh_address} fully drained, awaiting refund',
                color='yellow')
        await asyncio.to_thread(
            self.networkDB.clear_pending_commitment, p2sh_address)
        # Notify sender so they can update their funding UTXO
        sender_nostr_pubkey = channel.get('sender_nostr_pubkey')
        if sender_nostr_pubkey:
            from satorilib.satori_nostr.models import ChannelSettlement
            settlement = ChannelSettlement(
                p2sh_address=p2sh_address,
                claim_txid=txid,
                new_funding_vout=change_vout,
                new_locked_sats=change_sats,
                timestamp=int(time.time()),
            )
            for client in self._networkClients.values():
                try:
                    await client.publish_settlement(settlement, sender_nostr_pubkey)
                except Exception as e:
                    logging.warning(f'Channel: failed to publish settlement: {e}')
        # Tombstone the commitment on all relays
        for client in self._networkClients.values():
            try:
                await client.remove_commitment(p2sh_address)
            except Exception:
                pass
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

        Flow:
          1. Find receiver's SATORI UTXO.
          2. Compute final tx shape and request Mundo fee params.
          3. Rebuild tx: P2SH inputs + SATORI input + existing outputs
             + SATORI fee output + SATORI change output + EVR change output.
          4. Sign P2SH vin[0] and SATORI input with SIGHASH_ALL|ANYONECANPAY (0x81).
          5. POST to Mundo (signOnly=true) — Mundo adds EVR input and returns
             the fully signed tx hex.
          6. Broadcast the returned tx and return the txid.
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

        # Step 5: serialize and POST to Mundo (signOnly=true)
        incomplete_hex = new_tx.serialize().hex()

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

        # Step 6: broadcast the fully-signed tx and return txid
        txid = await asyncio.to_thread(self.wallet.broadcast, signed_hex)
        return txid

    async def _grantChannelAccess(
        self,
        sender_nostr_pubkey: str,
        total_paid_sats: int,
        stream_name: str = '',
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
        """
        if not sender_nostr_pubkey or total_paid_sats <= 0:
            return
        pubs = await asyncio.to_thread(self.networkDB.get_active_publications)
        for pub in pubs:
            price_per_obs = pub.get('price_per_obs', 0)
            if price_per_obs <= 0:
                continue
            pub_stream = pub['stream_name']
            # If the commitment names a specific stream, skip all others
            if stream_name and pub_stream != stream_name:
                continue
            paid_count = total_paid_sats // price_per_obs
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
                client.record_payment(pub_stream, sender_nostr_pubkey, grant_to_seq)
            logging.info(
                f'Channel: granted {sender_nostr_pubkey[:16]}… access to '
                f'{pub_stream} up to seq={grant_to_seq} '
                f'({paid_count} obs)',
                color='cyan')

    async def _channelExpiryCheck(self):
        """Auto-claim receiver channels that expire within 24 hours.

        Called from the reconcile loop but throttled to run at most every
        12 hours. Protects the receiver from losing accrued micropayments
        when a channel times out.
        """
        now = time.time()
        last = getattr(self, '_lastChannelExpiryCheck', 0)
        if now - last < 43200:  # 12 hours
            return
        self._lastChannelExpiryCheck = now
        try:
            near_expiry = await asyncio.to_thread(
                self.networkDB.get_channels_near_expiry, 86400)
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

    def _channelFundSats(self) -> int:
        """Return the configured channel funding amount in sats (default 10,000)."""
        return int(config.get().get('channel_fund_sats', 10_000))

    def _channelTimeoutMinutes(self) -> int:
        """Return the configured channel lifetime in minutes (default 90 days)."""
        return int(config.get().get('channel_timeout_minutes', 129600))

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
        await asyncio.to_thread(
            self.networkDB.save_channel,
            p2sh_address,
            self.wallet.pubkey,
            receiver_pubkey,
            script_payload['redeem_script_hex'],
            script_payload['funding_txid'],
            script_payload['funding_vout'] or 0,
            amount_sats,
            amount_sats,  # remainder starts equal to locked
            True,         # is_sender
            blocks,
            minutes,
            sender_nostr_pubkey=self.nostrPubkey or '',
            receiver_nostr_pubkey=receiver_nostr_pubkey or '',
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
                locked_sats=amount_sats,
                blocks=blocks,
                minutes=minutes,
                timestamp=int(time.time()),
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
        await asyncio.to_thread(
            self.networkDB.update_channel_funding,
            p2sh_address, txid, funding_vout, amount_sats)
        logging.info(
            f'Channel: refunded {p2sh_address} '
            f'amount={amount_sats} sats txid={txid}',
            color='green')

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
            fee = TxUtils.defaultFee
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

        try:
            txid = await asyncio.to_thread(_build_standard_reclaim)
        except TransactionFailure:
            # PATH B: sender has no EVR — fall back to Mundo
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
        return txid

    async def _reclaimChannelViaMundo(
        self,
        channel: dict,
        redeem_script,
        csv_value: int,
        sat_sats: int,
    ) -> str:
        """PATH B reclaim: sender has no EVR — pay SATORI fee to Mundo.

        Flow:
          1. Find sender's SATORI UTXO for the fee.
          2. Request Mundo fee params for final tx shape.
          3. Build tx: [P2SH vin, SATORI vin] +
             [SATORI→sender, SATORI fee→Mundo, SATORI change, EVR change].
          4. Set nVersion=2, nSequence=csv_value on the tx (CSV requirement).
          5. Sign P2SH vin[0] with 0x81 (SIGHASH_ALL|ANYONECANPAY, CSV branch).
          6. Sign SATORI vin[1] with 0x81.
          7. POST to Mundo (signOnly=true) — Mundo adds its EVR input.
          8. Broadcast the returned fully-signed hex and return the txid.
        """
        import requests as _requests
        from evrmore.core import (
            CMutableTransaction, CMutableTxIn, CMutableTxOut, COutPoint, lx)
        from evrmore.core.script import (
            CScript, SIGHASH_ALL, SIGHASH_ANYONECANPAY,
            OP_EVR_ASSET, OP_DROP, OP_FALSE)
        from evrmore.wallet import CEvrmoreAddress
        from satorilib.wallet.concepts.transaction import AssetTransaction
        from satorilib.wallet.utils.transaction import TxUtils

        MUNDO_URL = os.environ.get('MUNDO_URL', 'https://mundo.satorinet.org')

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

        # Step 7: serialize and POST to Mundo (signOnly=true)
        incomplete_hex = new_tx.serialize().hex()

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

        # Step 8: broadcast the fully-signed tx
        return await asyncio.to_thread(self.wallet.broadcast, signed_hex)

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
        for relay_url, client in list(self._networkClients.items()):
            try:
                await client.publish_observation(observation, metadata)
                logging.info(
                    f'Network: published {stream_name} seq={seq_num} '
                    f'value={value} to {relay_url}', color='green')
            except Exception as e:
                logging.warning(
                    f'Network: publish failed on {relay_url}: {e}')
        # Score competition predictions for this observation (host side).
        # The host publishes observations but doesn't subscribe to its own
        # stream, so _networkProcessObservation never fires for its own data.
        await self._competitionScoreAndPay(
            stream_name=stream_name,
            provider_pubkey=self.nostrPubkey,
            seq_num=seq_num,
            raw_value=value,
        )

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

    # ── Competition sync wrappers ──────────────────────────────────

    def announceCompetitionSync(self, competition_data: dict) -> None:
        """Announce a competition on all connected relays (sync context)."""
        from satorilib.satori_nostr.models import CompetitionAnnouncement
        if not hasattr(self, '_networkSecretHex') or not self._networkClients:
            return
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            return
        import json as _json
        competition = CompetitionAnnouncement(
            stream_name=competition_data['stream_name'],
            stream_provider_pubkey=competition_data['stream_provider_pubkey'],
            host_pubkey=self.nostrPubkey,
            pay_per_obs_sats=int(competition_data['pay_per_obs_sats']),
            paid_predictors=int(competition_data['paid_predictors']),
            competing_predictors=int(competition_data['competing_predictors']),
            scoring_metric=competition_data['scoring_metric'],
            scoring_params=competition_data.get('scoring_params', {}),
            horizon=int(competition_data.get('horizon', 1)),
            active=True,
            timestamp=int(time.time()),
        )
        async def _announce():
            for client in self._networkClients.values():
                try:
                    await client.announce_competition(competition)
                except Exception as e:
                    logging.warning(f'Competition: announce failed: {e}')
        asyncio.run_coroutine_threadsafe(_announce(), loop)

    def closeCompetitionSync(
        self, stream_name: str, stream_provider_pubkey: str
    ) -> None:
        """Close a competition on all connected relays (sync context)."""
        from satorilib.satori_nostr.models import CompetitionAnnouncement
        if not hasattr(self, '_networkSecretHex') or not self._networkClients:
            return
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            return
        row = self.networkDB.get_competition(
            stream_name, stream_provider_pubkey, self.nostrPubkey)
        if not row:
            return
        import json as _json
        competition = CompetitionAnnouncement(
            stream_name=row['stream_name'],
            stream_provider_pubkey=row['stream_provider_pubkey'],
            host_pubkey=row['host_pubkey'],
            pay_per_obs_sats=row['pay_per_obs_sats'],
            paid_predictors=row['paid_predictors'],
            competing_predictors=row['competing_predictors'],
            scoring_metric=row['scoring_metric'],
            scoring_params=_json.loads(row.get('scoring_params', '{}')),
            horizon=row.get('horizon', 1),
            active=False,
            timestamp=int(time.time()),
        )
        async def _close():
            for client in self._networkClients.values():
                try:
                    await client.close_competition(competition)
                except Exception as e:
                    logging.warning(f'Competition: close failed: {e}')
        asyncio.run_coroutine_threadsafe(_close(), loop)

    def discoverCompetitionsSync(self, active_only: bool = True) -> list:
        """Discover competitions from connected relays (sync context)."""
        if not self._networkClients:
            return self.networkDB.get_all_competitions(active_only=active_only)
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            return self.networkDB.get_all_competitions(active_only=active_only)
        async def _discover():
            seen: dict[str, tuple[int, dict]] = {}
            for client in self._networkClients.values():
                try:
                    comps = await client.discover_competitions(
                        active_only=active_only)
                    for c in comps:
                        d = c.d_tag()
                        if d not in seen or c.timestamp > seen[d][0]:
                            seen[d] = (c.timestamp, c.to_dict())
                except Exception as e:
                    logging.warning(f'Competition: discover failed: {e}')
            return [v for _, v in seen.values()]
        future = asyncio.run_coroutine_threadsafe(_discover(), loop)
        try:
            return future.result(timeout=15)
        except Exception:
            return self.networkDB.get_all_competitions(active_only=active_only)

    async def _incomingPredictionsLoop(self, client, relay_url: str):
        """Consume incoming prediction submissions from a relay client (host side).

        Runs as a background async task. Stores each received prediction in the
        DB keyed by (stream_name, stream_provider_pubkey, predictor_pubkey, seq_num).
        The scoring pipeline reads from the DB when an observation arrives.

        When a prediction arrives after its target observation is already
        in hand, it is scored immediately (late-arrival recovery).
        """
        logging.info(f'Competition: listening for predictions on {relay_url}')
        try:
            async for inbound in client.incoming_predictions():
                try:
                    p = inbound.prediction
                    await asyncio.to_thread(
                        self.networkDB.save_competition_prediction,
                        stream_name=p.stream_name,
                        stream_provider_pubkey=p.stream_provider_pubkey,
                        predictor_pubkey=p.predictor_pubkey,
                        host_pubkey=self.nostrPubkey,
                        seq_num=p.seq_num,
                        predicted_value=str(p.predicted_value),
                        received_at=p.timestamp,
                        predictor_wallet_pubkey=getattr(
                            p, 'predictor_wallet_pubkey', None),
                    )
                    logging.info(
                        f'Competition: received prediction from '
                        f'{p.predictor_pubkey[:12]}… for {p.stream_name} '
                        f'seq={p.seq_num}')
                    # Late-arrival: if we already have the observation this
                    # prediction was meant to predict, score it now.
                    await self._competitionScoreLateArrival(
                        stream_name=p.stream_name,
                        provider_pubkey=p.stream_provider_pubkey,
                        seq_num=p.seq_num,
                    )
                except Exception as e:
                    logging.warning(f'Competition: failed to save prediction: {e}')
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(
                f'Competition: prediction listener stopped on {relay_url}: {e}')

    async def submitPrediction(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
        seq_num: int,
        predicted_value: float,
    ) -> None:
        """Submit a prediction to a competition host on all connected relays (predictor side).

        The predictor's own wallet pubkey is attached so the host can open a
        payment channel back without needing a separate directory lookup.
        """
        wallet_pubkey = getattr(
            getattr(self, 'wallet', None), 'pubkey', None) or ''
        for client in self._networkClients.values():
            try:
                await client.submit_prediction(
                    stream_name=stream_name,
                    stream_provider_pubkey=stream_provider_pubkey,
                    host_pubkey=host_pubkey,
                    seq_num=seq_num,
                    predicted_value=predicted_value,
                    predictor_wallet_pubkey=wallet_pubkey,
                )
            except Exception as e:
                logging.warning(f'Competition: submit prediction failed: {e}')

    def submitPredictionSync(
        self,
        stream_name: str,
        stream_provider_pubkey: str,
        host_pubkey: str,
        seq_num: int,
        predicted_value: float,
    ) -> None:
        """Submit a prediction from a sync context (e.g. prediction engine callback)."""
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed() or not self._networkClients:
            return
        asyncio.run_coroutine_threadsafe(
            self.submitPrediction(
                stream_name, stream_provider_pubkey,
                host_pubkey, seq_num, predicted_value),
            loop)

    # ── Access Request handling (producer side) ─────────────────────────────

    async def _incomingAccessRequestsLoop(self, client, relay_url: str):
        """Consume incoming access requests from a relay client (producer side).

        Runs as a background async task. Stores each request in the DB as
        pending. The producer approves or rejects via the UI/API.
        """
        logging.info(f'AccessGate: listening for access requests on {relay_url}')
        try:
            async for inbound in client.incoming_access_requests():
                try:
                    req = inbound.access_request
                    await asyncio.to_thread(
                        self.networkDB.add_access_request,
                        stream_name=req.stream_name,
                        requester_pubkey=req.requester_pubkey,
                        message=req.message,
                        requested_at=req.timestamp,
                    )
                    logging.info(
                        f'AccessGate: received request from '
                        f'{req.requester_pubkey[:12]}… for {req.stream_name}')
                except Exception as e:
                    logging.warning(f'AccessGate: failed to save request: {e}')
        except asyncio.CancelledError:
            return
        except Exception as e:
            logging.warning(
                f'AccessGate: request listener stopped on {relay_url}: {e}')

    def approveAccessRequestSync(
        self,
        stream_name: str,
        requester_pubkey: str,
    ) -> None:
        """Approve an access request (sync context, e.g. Flask route).

        Adds the requester to the approved_subscribers table and records
        them in the in-memory subscriber list so they start receiving
        encrypted observations immediately.
        """
        self.networkDB.approve_access_request(stream_name, requester_pubkey)
        # Also register in the in-memory subscriber list on all clients
        for client in self._networkClients.values():
            client.record_subscription(stream_name, requester_pubkey)

    def rejectAccessRequestSync(
        self,
        stream_name: str,
        requester_pubkey: str,
    ) -> None:
        """Reject an access request (sync context)."""
        self.networkDB.reject_access_request(stream_name, requester_pubkey)

    def revokeSubscriberSync(
        self,
        stream_name: str,
        subscriber_pubkey: str,
    ) -> None:
        """Revoke a previously approved subscriber (sync context).

        Removes from the approved_subscribers table. The producer simply
        stops encrypting for them on the next observation — no key
        rotation needed since NIP-04 encrypts individually per subscriber.
        """
        self.networkDB.revoke_subscriber(stream_name, subscriber_pubkey)

    def requestAccessSync(
        self,
        stream_name: str,
        producer_pubkey: str,
        message: str = '',
    ) -> None:
        """Request access to an approval-gated stream (subscriber, sync context)."""
        if not self._networkClients:
            return
        loop = getattr(self, '_networkLoop', None)
        if loop is None or loop.is_closed():
            return
        async def _request():
            for client in self._networkClients.values():
                try:
                    await client.request_access(
                        stream_name=stream_name,
                        producer_pubkey=producer_pubkey,
                        message=message,
                    )
                except Exception as e:
                    logging.warning(f'AccessGate: request failed: {e}')
        asyncio.run_coroutine_threadsafe(_request(), loop)

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
        import random
        try:
            relays = await asyncio.to_thread(self.server.getRelays)
            relay_urls = [r['relay_url'] for r in relays]
            random.shuffle(relay_urls)
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
