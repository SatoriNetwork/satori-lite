from enum import Enum
from typing import Union, Callable
import threading
from queue import Queue
from satorilib.concepts.structs import StreamId, Stream
from satorilib.wallet import EvrmoreWallet
from satorilib.server import SatoriServerClient


class RunMode(Enum):
    normal = 1
    worker = 2
    wallet = 3

    @classmethod
    def choose(cls, runMode):
        # Convert runMode to lowercase if it's a string
        if isinstance(runMode, str):
            runMode = runMode.lower()
        # Define a mapping of possible inputs to Enum values
        mapping = {
            1: cls.normal,
            '1': cls.normal,
            '': cls.normal,
            None: cls.normal,
            'none': cls.normal,
            'normal': cls.normal,
            2: cls.worker,
            '2': cls.worker,
            'worker': cls.worker,
            3: cls.wallet,
            '3': cls.wallet,
            'wallet': cls.wallet,
        }
        # Return the corresponding Enum value
        return mapping.get(runMode, cls.normal)

class UiEndpoint(Enum):
    connectionStatus = 1
    modelUpdate = 2
    workingUpdate = 3

    def __str__(self):
        return self.name

    @property
    def name(self):
        if self == UiEndpoint.connectionStatus:
            return 'connection-status'
        if self == UiEndpoint.modelUpdate:
            return 'model-updates'
        if self == UiEndpoint.workingUpdate:
            return 'working-updates'
        return 'unknown'

class StartupDagStruct(object):
    ''' a DAG of startup tasks. '''

    def __init__(
        self,
        env: str = None,
        runMode: RunMode = False,
        sendToUI: Callable = None,
        urlServer: str = None,
        urlMundo: str = None,
        urlPubsubs: list[str] = None,
        *args
    ):
        self.env = env
        self.runMode = None
        sendToUI = sendToUI or (lambda x: None)
        self.chatUpdates: Queue = None
        self.latestConnectionStatus: dict = None
        self.env: str = None
        self.urlServer: str = None
        self.urlMundo: str = None
        self.urlPubsubs: [str] = None
        self.paused: bool = None
        self.pauseThread: Union[threading.Thread, None] = None
        self._evrmoreWallet: EvrmoreWallet = None
        self._evrmoreVault: Union[EvrmoreWallet, None] = None
        self.details: dict = None
        self.key: str = None
        self.oracleKey: str = None
        self.idKey: str = None
        self.subscriptionKeys: str = None
        self.publicationKeys: str = None
        self.signedStreamIds: list = None
        self.relayValidation: 'ValidateRelayStream' = None
        self.server: SatoriServerClient = None
        self.relay: 'RawStreamRelayEngine' = None
        self.engine: 'satoriengine.Engine' = None
        self.publications: list[Stream] = None
        self.subscriptions: list[Stream] = None
        self.udpQueue: Queue  # TODO: remove
        self.stakeStatus: bool = False

    def cacheOf(self, streamId: StreamId):
        ''' returns the reference to the cache of a stream '''

    @property
    def walletMode(self) -> bool:
        ''' get wallet '''

    @property
    def network(self) -> str:
        ''' get wallet '''

    @property
    def vault(self) -> EvrmoreWallet:
        ''' get wallet '''

    @property
    def wallet(self) -> EvrmoreWallet:
        ''' get wallet '''

    @property
    def evrmoreWallet(self) -> EvrmoreWallet:
        ''' get wallet '''

    def evrmoreVault(
        self,
        password: Union[str, None] = None,
        create: bool = False,
    ) -> Union[EvrmoreWallet, None]:
        ''' get the evrmore vault '''

    def start(self):
        ''' start the satori engine. '''

    def createRelayValidation(self):
        ''' creates relay validation engine '''

    def networkIsTest(self, network: str = None) -> bool:
        ''' get the ravencoin vault '''

    def getWallet(self, network: str = None) -> EvrmoreWallet:
        ''' get wallet '''

    def getVault(
        self,
        password: Union[str, None] = None,
        create: bool = False,
    ) -> EvrmoreWallet:
        ''' get the vault '''

    def checkin(self):
        ''' checks in with the Satori Server '''

    def buildEngine(self):
        ''' start the engine, it will run w/ what it has til ipfs is synced '''

    def subConnect(self):
        ''' establish a pubsub connection. '''

    def pubsConnect(self):
        ''' establish a pubsub connection. '''

    def startRelay(self):
        ''' starts the relay engine '''

    def pause(self, timeout: int = 60):
        ''' pause the engine. '''

    def unpause(self):
        ''' pause the engine. '''

    def performStakeCheck(self):
        ''' check the stake status '''

    def addWorkingUpdate(self, data: str):
        ''' tell ui we are working on something '''

    def addModelUpdate(self, data: dict):
        ''' tell ui about model changes '''
