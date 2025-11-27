from typing import Union
import os
import time
import json
import threading
from satorilib.concepts.structs import StreamId, Stream
from satorilib.concepts import constants
from satorilib.wallet import EvrmoreWallet
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.server import SatoriServerClient
from satorilib.server.api import CheckinDetails
from satorineuron import VERSION
from satorineuron import logging
from satorineuron import config
from satorineuron.init.tag import LatestTag, Version
from satorineuron.init.wallet import WalletManager
from satorineuron.structs.start import RunMode, StartupDagStruct
from satorilib.utils.ip import getPublicIpv4UsingCurl
from satoriengine.veda.engine import Engine


def getStart():
    """returns StartupDag singleton"""
    return StartupDag()


class SingletonMeta(type):
    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super(SingletonMeta, cls).__call__(*args, **kwargs)
        return cls._instances[cls]


class StartupDag(StartupDagStruct, metaclass=SingletonMeta):
    """a DAG of startup tasks."""

    _holdingBalanceBase_cache = None
    _holdingBalanceBase_timestamp = 0

    @classmethod
    def create(
        cls,
        *args,
        env: str = 'dev',
        runMode: str = None,
        urlServer: str = None,
        urlMundo: str = None,
        isDebug: bool = False,
    ) -> 'StartupDag':
        '''Factory method to create and initialize StartupDag'''
        startupDag = cls(
            *args,
            env=env,
            runMode=runMode,
            urlServer=urlServer,
            urlMundo=urlMundo,
            isDebug=isDebug)
        startupDag.startFunction()
        return startupDag

    def __init__(
        self,
        *args,
        env: str = 'dev',
        runMode: str = None,
        urlServer: str = None,
        urlMundo: str = None,
        isDebug: bool = False,
    ):
        super(StartupDag, self).__init__(*args)
        self.needsRestart: Union[str, None] = None
        self.version = Version(VERSION)
        self.env = env
        self.runMode = RunMode.choose(runMode or config.get().get('mode', None))
        logging.debug(f'mode: {self.runMode.name}', print=True)
        self.userInteraction = time.time()
        self.walletManager: WalletManager
        self.isDebug: bool = isDebug
        self.urlServer: str = urlServer
        self.urlMundo: str = urlMundo
        self.paused: bool = False
        self.pauseThread: Union[threading.Thread, None] = None
        self.details: CheckinDetails = CheckinDetails(raw={})
        self.balances: dict = {}
        self.key: str
        self.oracleKey: str
        self.idKey: str
        self.subscriptionKeys: str
        self.publicationKeys: str
        self.aiengine: Union[Engine, None] = None
        self.publications: list[Stream] = []
        self.subscriptions: list[Stream] = []
        self.identity: EvrmoreIdentity = EvrmoreIdentity(config.walletPath('wallet.yaml'))
        self.stakeStatus: bool = False
        self.miningMode: bool = False
        self.lastBlockTime = time.time()
        self.poolIsAccepting: bool = False
        self.invitedBy: str = None
        self.setInvitedBy()
        self.latestObservationTime: float = 0
        self.configRewardAddress: str = None
        self.setRewardAddress()
        self.setEngineVersion()
        self.setupWalletManager()
        self.ip = getPublicIpv4UsingCurl()
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

    @property
    def walletOnlyMode(self) -> bool:
        return self.runMode == RunMode.wallet

    @property
    def rewardAddress(self) -> str:
        if isinstance(self.details, CheckinDetails):
            return self.details.get('rewardaddress')
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

    @property
    def evrvaultaddressforward(self) -> str:
        evrvaultaddress = self.details.wallet.get('vaultaddress', '')
        if evrvaultaddress:
            return evrvaultaddress
        else:
            return ""

    def setupWalletManager(self):
        self.walletManager = WalletManager.create()

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

    def watchForVersionUpdates(self):
        """
        if we notice the code version has updated, download code restart
        in order to restart we have to kill the main thread manually.
        """

        def getPidByName(name: str) -> Union[int, None]:
            import psutil
            for proc in psutil.process_iter(["pid", "cmdline"]):
                try:
                    if name in " ".join(proc.info["cmdline"]):
                        return proc.info["pid"]
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            return None

        def terminatePid(pid: int):
            import signal
            os.kill(pid, signal.SIGTERM)

        def watchForever():
            latestTag = LatestTag(self.version, serverURL=self.urlServer)
            while True:
                time.sleep(60 * 60 * 6)
                if latestTag.mustUpdate():
                    terminatePid(getPidByName("satori.py"))

        self.watchVersionThread = threading.Thread(
            target=watchForever,
            daemon=True)
        self.watchVersionThread.start()

    def delayedEngine(self):
        time.sleep(60 * 60 * 6)
        self.buildEngine()

    def checkinCheck(self):
        while True:
            time.sleep(60 * 60 * 6)
            current_time = time.time()
            if self.latestObservationTime and (current_time - self.latestObservationTime > 60*60*6):
                logging.warning("No observations in 6 hours, restarting")
                self.triggerRestart()
            if hasattr(self, 'server') and hasattr(self.server, 'checkinCheck') and self.server.checkinCheck():
                logging.warning("Server check failed, restarting")
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
            self.checkin()
            self.getBalances()
            logging.info("in WALLETONLYMODE")
            return
        self.setMiningMode()
        self.createServerConn()
        self.checkin()
        self.getBalances()
        self.spawnEngine()

    def startWalletOnly(self):
        """start the satori engine."""
        logging.info("running in walletOnly mode", color="blue")
        self.createServerConn()
        return

    def startWorker(self):
        """start the satori engine."""
        logging.info("running in worker mode", color="blue")
        self.setMiningMode()
        self.createServerConn()
        self.checkin()
        self.getBalances()
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
        logging.debug(self.urlServer, color="teal")
        self.server = SatoriServerClient(
            self.wallet, url=self.urlServer, sendingUrl=self.urlMundo
        )

    def checkin(self):
        try:
            referrer = (
                open(config.root("config", "referral.txt"), mode="r")
                .read()
                .strip())
        except Exception as _:
            referrer = None
        x = 30
        attempt = 0
        while True:
            attempt += 1
            try:
                vault = self.getVault()
                self.details = CheckinDetails(
                    self.server.checkin(
                        referrer=referrer,
                        ip=self.ip,
                        vaultInfo={
                            'vaultaddress': vault.address,
                            'vaultpubkey': vault.pubkey,
                        } if isinstance(vault, EvrmoreWallet) else None))

                if self.details.get('sponsor') != self.invitedBy:
                    if self.invitedBy is None:
                        self.setInvitedBy(self.details.get('sponsor'))
                    if isinstance(self.invitedBy, str) and len(self.invitedBy) == 34 and self.invitedBy.startswith('E'):
                        self.server.invitedBy(self.invitedBy)

                if config.get().get('prediction stream', 'notExisting') == 'notExisting':
                    config.add(data={'prediction stream': None})

                if self.details.get('rewardaddress') != self.configRewardAddress:
                    if self.configRewardAddress is None:
                        self.setRewardAddress(address=self.details.get('rewardaddress'))
                    else:
                        self.setRewardAddress(globally=True)

                self.key = self.details.key
                self.poolIsAccepting = bool(
                    self.details.wallet.get("accepting", False))
                self.oracleKey = self.details.oracleKey
                self.idKey = self.details.idKey
                self.subscriptionKeys = self.details.subscriptionKeys
                self.publicationKeys = self.details.publicationKeys
                self.subscriptions = [
                    Stream.fromMap(x)
                    for x in json.loads(self.details.subscriptions)]
                if (
                    attempt < 5 and (
                        self.details is None or len(self.subscriptions) == 0)
                ):
                    time.sleep(30)
                    continue
                logging.debug("subscriptions:", len(
                    self.subscriptions), print=True)
                self.publications = [
                    Stream.fromMap(x)
                    for x in json.loads(self.details.publications)]
                logging.debug(
                    "publications:",
                    len(self.publications),
                    print=True)
                logging.info("checked in with Satori", color="green")
                break
            except Exception as e:
                logging.warning(f"connecting to central err: {e}")
            x = x * 1.5 if x < 60 * 60 * 6 else 60 * 60 * 6
            logging.warning(f"trying again in {x}")
            time.sleep(x)

    def getBalances(self):
        '''
        we get this from the server, not electrumx
        example:
        {
            'currency': 100,
            'chain_balance': 0,
            'liquidity_balance': None,
        }
        '''
        success, self.balances = self.server.getBalances()
        if not success:
            logging.warning("Failed to get balances from server")
        return self.getBalance()

    def getBalance(self, currency: str = 'currency') -> float:
        return self.balances.get(currency, 0)

    def setRewardAddress(
        self,
        address: Union[str, None] = None,
        globally: bool = False
    ) -> bool:
        if EvrmoreWallet.addressIsValid(address):
            self.configRewardAddress = address
            config.add(data={'reward address': address})
            if isinstance(self.details, CheckinDetails):
                self.details.setRewardAddress(address)
            if not globally:
                return True
        else:
            self.configRewardAddress: str = str(config.get().get('reward address', ''))
        if (
            globally and
            self.env in ['prod', 'local', 'testprod'] and
            EvrmoreWallet.addressIsValid(self.configRewardAddress)
        ):
            self.server.setRewardAddress(address=self.configRewardAddress)
            return True
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

    def spawnEngine(self):
        """Spawn the AI Engine with stream assignments from Neuron"""
        if not self.subscriptions or not self.publications:
            logging.warning("No stream assignments available, skipping Engine spawn")
            return

        logging.info("Spawning AI Engine...", color="blue")
        try:
            self.aiengine = Engine.createFromNeuron(
                subscriptions=self.subscriptions,
                publications=self.publications,
                server=self.server,
                wallet=self.wallet)

            def runEngine():
                try:
                    self.aiengine.initializeFromNeuron()
                    while True:
                        time.sleep(60)
                except Exception as e:
                    logging.error(f"Engine error: {e}")

            engineThread = threading.Thread(target=runEngine, daemon=True)
            engineThread.start()
            logging.info("AI Engine spawned successfully", color="green")
        except Exception as e:
            logging.error(f"Failed to spawn AI Engine: {e}")

    # def findMatchingPubSubStream(self, uuid: str, sub: bool = True) -> Stream:
    #         if sub:
    #             for sub in self.subscriptions:
    #                 if sub.streamId.uuid == uuid:
    #                     return sub
    #         else:
    #             for pub in self.publications:
    #                 if pub.streamId.uuid == uuid:
    #                     return pub

    def pause(self, timeout: int = 60):
        """pause the engine."""
        self.paused = True
        self.pauseTimer = threading.Timer(timeout, self.unpause)
        self.pauseTimer.daemon = True
        self.pauseTimer.start()
        logging.info("AI engine paused", color="green")

    def unpause(self):
        """unpause the engine."""
        self.paused = False
        if hasattr(self, 'pauseTimer') and self.pauseTimer is not None:
            self.pauseTimer.cancel()
        self.pauseTimer = None
        logging.info("AI engine unpaused", color="green")

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

    def setEngineVersion(self, version: Union[str, None] = None) -> str:
        default = 'v2'
        version = (
            version
            if version in ['v1', 'v2']
            else config.get().get('engine version', default))
        self.engineVersion = version if version in ['v1', 'v2'] else default
        config.add(data={'engine version': self.engineVersion})
        return self.engineVersion

    def setInvitedBy(self, address: Union[str, None] = None) -> str:
        address = address or config.get().get('invited by', address)
        if address:
            self.invitedBy = address
            config.add(data={'invited by': self.invitedBy})
        return self.invitedBy

    def poolAccepting(self, status: bool):
        success, result = self.server.poolAccepting(status)
        if success:
            self.poolIsAccepting = status
        return success, result

    @property
    def stakeRequired(self) -> float:
        return self.details.stakeRequired or constants.stakeRequired


if __name__ == "__main__":
    logging.info("Starting Satori Neuron", color="green")
    startup = StartupDag.create(env='prod', runMode='worker')
    threading.Event().wait()
