from typing import Union
import os
import time
import json
import copy
import asyncio
import warnings
import threading
import numpy as np
import pandas as pd
from satorilib.concepts import Observation, Stream
from satorilib.logging import INFO, setup, debug, info, warning, error
from satorilib.utils.system import getProcessorCount
from satorilib.utils.time import datetimeToTimestamp, now
from satorilib.datamanager import DataClient, DataServerApi, DataClientApi, PeerInfo, Message, Subscription
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.server import SatoriServerClient
# TODO: Commented out for engine-neuron integration
# from satorilib.centrifugo import (
#     create_centrifugo_client,
#     create_subscription_handler,
#     subscribe_to_stream
# )
from satoriengine.veda import config
from satoriengine.veda.data import StreamForecast, validate_single_entry
from satoriengine.veda.adapters import ModelAdapter, StarterAdapter, XgbAdapter, XgbChronosAdapter
from satoriengine.veda.storage import EngineStorageManager

warnings.filterwarnings('ignore')
setup(level=INFO)



class Engine:

    @classmethod
    async def create(cls) -> 'Engine':
        engine = cls()
        await engine.initialize()
        return engine

    @classmethod
    def createFromNeuron(
        cls,
        subscriptions: list,
        publications: list,
        server: SatoriServerClient,
        wallet: any,
    ) -> 'Engine':
        """
        Factory method for Neuron to spawn Engine with stream assignments.

        Args:
            subscriptions: List of Stream objects to subscribe to
            publications: List of Stream objects for predictions
            server: SatoriServerClient instance from Neuron
            wallet: Wallet instance from Neuron for signing
        """
        engine = cls()
        engine.server = server
        engine.wallet = wallet
        engine.subscriptionStreams = subscriptions
        engine.publicationStreams = publications
        # Build subscription/publication dicts from Stream objects
        for sub in subscriptions:
            engine.subscriptions[sub.streamId.uuid] = PeerInfo([], [])
        for pub in publications:
            engine.publications[pub.streamId.uuid] = PeerInfo([], [])
        return engine

    def __init__(self):
        self.streamModels: dict[str, StreamModel] = {}
        self.subscriptions: dict[str, PeerInfo] = {}
        self.publications: dict[str, PeerInfo] = {}
        self.subscriptionStreams: list = []
        self.publicationStreams: list = []
        self.server: Union[SatoriServerClient, None] = None
        self.wallet: any = None
        self.dataServerIp: str = ''
        self.dataServerPort: Union[int, None] = None
        self.dataClient: Union[DataClient, None] = None
        self.paused: bool = False
        self.threads: list[threading.Thread] = []
        self.identity: EvrmoreIdentity = EvrmoreIdentity('/Satori/Neuron/wallet/wallet.yaml')
        self.transferProtocol: Union[str, None] = None
        # SQLite storage manager for local data persistence
        self.storage: EngineStorageManager = EngineStorageManager.getInstance()
        # TODO: Commented out for engine-neuron integration
        # self.centrifugo = None
        # self.centrifugoSubscriptions: list = []



    # TODO: Commented out for engine-neuron integration
    # async def centrifugoConnect(self, centrifugoPayload: dict):
    #     """establish a centrifugo connection for subscribing"""
    #     if not centrifugoPayload:
    #         error("No centrifugo payload provided")
    #         return
    #
    #     token = centrifugoPayload.get('token')
    #     ws_url = centrifugoPayload.get('ws_url')
    #
    #     if not token or not ws_url:
    #         error("Missing token or ws_url in centrifugo payload")
    #         return
    #
    #     try:
    #         self.centrifugo = await create_centrifugo_client(
    #             ws_url=ws_url,
    #             token=token,
    #             on_connected_callback=lambda x: info("Centrifugo connected"),
    #             on_disconnected_callback=lambda x: info("Centrifugo disconnected"))
    #
    #         await self.centrifugo.connect()
    #     except Exception as e:
    #         error(f"Failed to connect to Centrifugo: {e}")
    #
    # async def handleCentrifugoMessage(self, ctx, streamUuid: str):
    #     """Handle messages from Centrifugo"""
    #     try:
    #         raw_data = ctx.pub.data
    #
    #         # Parse the JSON string to get the actual data
    #         if isinstance(raw_data, str):
    #             data = json.loads(raw_data)
    #         else:
    #             data = raw_data
    #
    #         # Format data for Observation parsing
    #         formatted_data = {
    #             "topic": json.dumps({"uuid": streamUuid}),
    #             "data": data.get('value'),
    #             "time": data.get('time'),
    #             "hash": data.get('hash')
    #         }
    #         obs = Observation.parse(json.dumps(formatted_data))
    #         streamModel = self.streamModels.get(streamUuid)
    #         if isinstance(streamModel, StreamModel) and getattr(streamModel, 'usePubSub', True):
    #             await streamModel.handleSubscriptionMessage(
    #                 "Subscription",
    #                 message=Message({
    #                     'data': obs,
    #                     'status': 'stream/observation'}),
    #                 pubSubFlag=True)
    #     except Exception as e:
    #         error(f"Error handling Centrifugo message: {e}")

    def initialize(self):
        self.connectToDataServer()
        threading.Thread(target=self.stayConnectedForever, daemon=True).start()
        self.startService()

    def initializeFromNeuron(self):
        """Initialize engine when spawned from Neuron (no DataServer needed)"""
        info("Engine initializing from Neuron...", color='blue')
        self.initializeModelsFromNeuron()
        # info("Engine initialized successfully", color='green')

    def startService(self):
        self.getPubSubInfo()
        self.initializeModels()

    def addStream(self, stream: Stream, pubStream: Stream):
        ''' add streams to a running engine '''
        # don't duplicate effort
        if stream.streamId.uuid in [s.streamId.uuid for s in self.streams]:
            return
        self.streams.append(stream)
        self.pubstreams.append(pubStream)
        self.streamModels[stream.streamId] = StreamModel(
            streamId=stream.streamId,
            predictionStreamId=pubStream.streamId,
            predictionProduced=self.predictionProduced)
        self.streamModels[stream.streamId].chooseAdapter(inplace=True)
        self.streamModels[stream.streamId].run_forever()

    def pause(self, force: bool = False):
        if force:
            self.paused = True
        for streamModel in self.streamModels.values():
            streamModel.pause()

    def resume(self, force: bool = False):
        if force:
            self.paused = False
        if not self.paused:
            for streamModel in self.streamModels.values():
                streamModel.resume()

    @property
    def isConnectedToServer(self):
        if hasattr(self, 'dataClient') and self.dataClient is not None:
            return self.dataClient.isConnected()
        return False

    def connectToDataServer(self):
        ''' connect to server, retry if failed '''

        def authenticate() -> bool:
            response = asyncio.run(self.dataClient.authenticate(islocal='engine'))
            if response.status == DataServerApi.statusSuccess.value:
                info("Local Engine successfully connected to Server Ip at :", self.dataServerIp, color="green")
                return True
            return False

        def initiateServerConnection() -> bool:
            ''' local engine client authorization '''
            self.dataClient = DataClient(self.dataServerIp, self.dataServerPort, identity=self.identity)
            return authenticate()

        waitingPeriod = 10
        while not self.isConnectedToServer:
            try:
                self.dataServerIp = config.get().get('server ip', '0.0.0.0')
                self.dataServerPort = int(config.get().get('server port', 24600))
                if initiateServerConnection():
                    return True
            except Exception as e:
                warning(f'Failed to find a valid Server Ip, retrying in {waitingPeriod}')
                time.sleep(waitingPeriod)

    def getPubSubInfo(self):
        ''' gets the relation info between pub-sub streams '''
        waitingPeriod = 10
        while not self.subscriptions and self.isConnectedToServer:
            try:
                pubSubResponse: Message = asyncio.run(self.dataClient.getPubsubMap())
                self.transferProtocol = pubSubResponse.streamInfo.get('transferProtocol')
                transferProtocolPayload = pubSubResponse.streamInfo.get('transferProtocolPayload')
                transferProtocolKey = pubSubResponse.streamInfo.get('transferProtocolKey')
                pubSubMapping = pubSubResponse.streamInfo.get('pubSubMapping')
                if pubSubResponse.status == DataServerApi.statusSuccess.value and pubSubMapping:
                    for sub_uuid, data in pubSubMapping.items():
                        self.subscriptions[sub_uuid] = PeerInfo(data['dataStreamSubscribers'], data['dataStreamPublishers'])
                        self.publications[data['publicationUuid']] = PeerInfo(data['predictiveStreamSubscribers'], data['predictiveStreamPublishers'])
                    if self.subscriptions:
                        info(pubSubResponse.senderMsg, color='green')
                else:
                    raise Exception
                return
            except Exception:
                warning(f"Failed to fetch pub-sub info, waiting for {waitingPeriod} seconds")
                time.sleep(waitingPeriod)

    def stayConnectedForever(self):
        ''' runs in thread to maintain connection '''
        while True:
            time.sleep(5)
            self.cleanupThreads()
            if not self.isConnectedToServer:
                import sys
                sys.exit(1)

    def initializeModels(self):
        info(f'Transfer protocol : {self.transferProtocol}', color='green')
        for subUuid, pubUuid in zip(self.subscriptions.keys(), self.publications.keys()):
            peers = self.subscriptions[subUuid]
            try:
                self.streamModels[subUuid] = StreamModel.create(
                    streamUuid=subUuid,
                    predictionStreamUuid=pubUuid,
                    peerInfo=peers,
                    dataClient=self.dataClient,
                    identity=self.identity,
                    pauseAll=self.pause,
                    resumeAll=self.resume,
                    transferProtocol=self.transferProtocol)
            except Exception as e:
                error(e)
            # TODO: Commented out for engine-neuron integration
            # if self.centrifugo is not None:
            #     self.streamModels[subUuid].usePubSub = True
            self.streamModels[subUuid].chooseAdapter(inplace=True)
            self.streamModels[subUuid].run_forever()

    def initializeModelsFromNeuron(self):
        """Initialize models when spawned from Neuron - uses server directly"""
        info("Initializing models from Neuron stream assignments...", color='green')

        # Match subscriptions to publications by predicting relationship
        for sub in self.subscriptionStreams:
            # Find matching publication (prediction stream)
            matchingPub = None
            for pub in self.publicationStreams:
                if pub.predicting == sub.streamId:
                    matchingPub = pub
                    break

            if matchingPub is None:
                warning(f"No matching publication for subscription {sub.streamId.uuid}")
                continue

            subUuid = sub.streamId.uuid
            pubUuid = matchingPub.streamId.uuid

            try:
                self.streamModels[subUuid] = StreamModel.createFromServer(
                    streamUuid=subUuid,
                    predictionStreamUuid=pubUuid,
                    server=self.server,
                    wallet=self.wallet,
                    subscriptionStream=sub,
                    publicationStream=matchingPub,
                    pauseAll=self.pause,
                    resumeAll=self.resume,
                    storage=self.storage)
                self.streamModels[subUuid].chooseAdapter(inplace=True)
                self.streamModels[subUuid].run_forever()
                info(f"Model initialized for stream {subUuid}", color='green')
            except Exception as e:
                error(f"Failed to initialize model for stream {subUuid}: {e}")

        # TODO: Commented out for engine-neuron integration
        # if self.centrifugo is not None:
        #     for subUuid in self.subscriptions.keys():
        #         streamModel = self.streamModels.get(subUuid)
        #         if streamModel:
        #             try:
        #                 def create_callback(stream_uuid):
        #                     async def callback(ctx):
        #                         await self.handleCentrifugoMessage(ctx, stream_uuid)
        #                     return callback
        #
        #                 sub = await subscribe_to_stream(
        #                     client=self.centrifugo,
        #                     stream_uuid=subUuid,
        #                     events=create_subscription_handler(
        #                         stream_uuid=subUuid,
        #                         on_publication_callback=create_callback(subUuid)))
        #                 self.centrifugoSubscriptions.append(sub)
        #                 info(f"Subscribed to Centrifugo stream {subUuid}")
        #             except Exception as e:
        #                 error(f"Failed to subscribe to Centrifugo stream {subUuid}: {e}")

    def cleanupThreads(self):
        for thread in self.threads:
            if not thread.is_alive():
                self.threads.remove(thread)
        debug(f'prediction thread count: {len(self.threads)}')

    # TODO: Commented out for engine-neuron integration
    # async def cleanupCentrifugo(self):
    #     """Clean up Centrifugo connections and subscriptions"""
    #     try:
    #         if hasattr(self, 'centrifugoSubscriptions') and self.centrifugoSubscriptions:
    #             for subscription in self.centrifugoSubscriptions:
    #                 try:
    #                     await subscription.unsubscribe()
    #                 except Exception as e:
    #                     error(f"Failed to unsubscribe from Centrifugo stream: {e}")
    #             self.centrifugoSubscriptions = []
    #
    #         if hasattr(self, 'centrifugo') and self.centrifugo:
    #             try:
    #                 await self.centrifugo.disconnect()
    #                 info("Centrifugo client disconnected")
    #             except Exception as e:
    #                 error(f"Failed to disconnect Centrifugo client: {e}")
    #             finally:
    #                 self.centrifugo = None
    #     except Exception as e:
    #         error(f"Error during Centrifugo cleanup: {e}")


class StreamModel:

    @classmethod
    def create(
        cls,
        streamUuid: str,
        predictionStreamUuid: str,
        peerInfo: PeerInfo,
        dataClient: DataClient,
        identity: EvrmoreIdentity,
        pauseAll:callable,
        resumeAll:callable,
        transferProtocol: str
    ):
        streamModel = cls(
            streamUuid,
            predictionStreamUuid,
            peerInfo,
            dataClient,
            identity,
            pauseAll,
            resumeAll,
            transferProtocol
        )
        streamModel.initialize()
        return streamModel

    @classmethod
    def createFromServer(
        cls,
        streamUuid: str,
        predictionStreamUuid: str,
        server: SatoriServerClient,
        wallet: any,
        subscriptionStream: Stream,
        publicationStream: Stream,
        pauseAll: callable,
        resumeAll: callable,
        storage: EngineStorageManager = None,
    ):
        """Factory method for creating StreamModel that uses Central Server directly"""
        streamModel = cls.__new__(cls)
        streamModel.cpu = getProcessorCount()
        streamModel.pauseAll = pauseAll
        streamModel.resumeAll = resumeAll
        streamModel.preferredAdapters = [XgbAdapter, StarterAdapter]
        streamModel.defaultAdapters = [XgbAdapter, XgbAdapter, StarterAdapter]
        streamModel.failedAdapters = []
        streamModel.thread = None
        streamModel.streamUuid = streamUuid
        streamModel.predictionStreamUuid = predictionStreamUuid
        streamModel.subscriptionStream = subscriptionStream
        streamModel.publicationStream = publicationStream
        streamModel.server = server
        streamModel.wallet = wallet
        streamModel.rng = np.random.default_rng(37)
        streamModel.publisherHost = None
        streamModel.transferProtocol = 'central'
        streamModel.usePubSub = False
        streamModel.internal = True
        streamModel.useServer = True  # Flag to use server instead of DataClient
        streamModel.peerInfo = PeerInfo([], [])
        streamModel.dataClientOfIntServer = None
        streamModel.dataClientOfExtServer = None
        streamModel.identity = None
        # SQLite storage manager for local persistence
        streamModel.storage = storage or EngineStorageManager.getInstance()
        streamModel.initializeFromServer()
        return streamModel

    def __init__(
        self,
        streamUuid: str,
        predictionStreamUuid: str,
        peerInfo: PeerInfo,
        dataClient: DataClient,
        identity: EvrmoreIdentity,
        pauseAll:callable,
        resumeAll:callable,
        transferProtocol: str
    ):
        self.cpu = getProcessorCount()
        self.pauseAll = pauseAll
        self.resumeAll = resumeAll
        # self.preferredAdapters: list[ModelAdapter] = [XgbChronosAdapter, XgbAdapter, StarterAdapter ]# SKAdapter #model[0] issue
        self.preferredAdapters: list[ModelAdapter] = [ XgbAdapter, StarterAdapter ]# SKAdapter #model[0] issue
        self.defaultAdapters: list[ModelAdapter] = [XgbAdapter, XgbAdapter, StarterAdapter]
        self.failedAdapters = []
        self.thread: threading.Thread = None
        self.streamUuid: str = streamUuid
        self.predictionStreamUuid: str = predictionStreamUuid
        self.peerInfo: PeerInfo = peerInfo
        self.dataClientOfIntServer: DataClient = dataClient
        self.identity: EvrmoreIdentity = identity
        self.rng = np.random.default_rng(37)
        self.publisherHost = None
        self.transferProtocol: str = transferProtocol
        self.usePubSub: bool = False
        self.internal: bool = False
        self.useServer: bool = False  # Default: use DataClient

    def initialize(self):
        self.data: pd.DataFrame = self.loadData()
        self.adapter: ModelAdapter = self.chooseAdapter()
        self.pilot: ModelAdapter = self.adapter(uid=self.streamUuid)
        self.pilot.load(self.modelPath())
        self.stable: ModelAdapter = copy.deepcopy(self.pilot)
        self.paused: bool = False
        self.dataClientOfExtServer: Union[DataClient, None] = DataClient(self.dataClientOfIntServer.serverHostPort[0], self.dataClientOfIntServer.serverPort, identity=self.identity)
        debug(f'AI Engine: stream id {self.streamUuid} using {self.adapter.__name__}', color='teal')

    def initializeFromServer(self):
        """Initialize model when using Central Server directly"""
        self.data: pd.DataFrame = self.loadDataFromServer()
        self.adapter: ModelAdapter = self.chooseAdapter()
        self.pilot: ModelAdapter = self.adapter(uid=self.streamUuid)
        self.pilot.load(self.modelPath())
        self.stable: ModelAdapter = copy.deepcopy(self.pilot)
        self.paused: bool = False
        debug(f'AI Engine: stream id {self.streamUuid} using {self.adapter.__name__} (Central Server mode)', color='teal')

    def loadDataFromServer(self) -> pd.DataFrame:
        """Load historical data - currently loads from local SQLite only."""
        # TODO: Implement fetching from Central Server when API is ready
        # For now, load from local SQLite if available
        localData = self.storage.getStreamDataForEngine(self.streamUuid)
        if not localData.empty:
            info(f"Loaded {len(localData)} rows from local SQLite for stream {self.streamUuid}", color='green')
            return localData
        info(f"No local data for stream {self.streamUuid}, starting fresh", color='yellow')
        return pd.DataFrame(columns=["date_time", "value", "id"])

    def onDataReceived(self, data: pd.DataFrame):
        """
        Called when new data is received from Central Server.
        Stores data in SQLite and passes to engine for predictions.

        Args:
            data: DataFrame with columns: ts (or date_time), value, hash (or id)
                  Index should be timestamp or 'ts' column should contain timestamps.

        TODO: Call this method when data arrives from Central Server.
        """
        try:
            if data.empty:
                return

            # Normalize column names for storage
            storageDf = data.copy()
            if 'date_time' in storageDf.columns:
                storageDf = storageDf.set_index('date_time')
            elif 'ts' in storageDf.columns:
                storageDf = storageDf.set_index('ts')

            # Store in SQLite (table name = streamUuid)
            insertedRows = self.storage.storeStreamData(
                self.streamUuid,
                storageDf,
                provider='central'
            )
            if insertedRows > 0:
                info(f"Stored {insertedRows} new rows in SQLite for stream {self.streamUuid}", color='green')

            # Also update in-memory data for predictions
            engineDf = data.copy()
            if 'ts' in engineDf.columns:
                engineDf = engineDf.rename(columns={'ts': 'date_time', 'hash': 'id'})
            if 'provider' in engineDf.columns:
                del engineDf['provider']

            self.data = pd.concat([self.data, engineDf], ignore_index=True)
            self.data = self.data.drop_duplicates(subset=['date_time'], keep='last')

            # Trigger prediction when new observation arrives
            if insertedRows > 0:
                info(f"New observation received, triggering prediction for stream {self.streamUuid}", color='blue')
                self.producePrediction()

        except Exception as e:
            error(f"Error storing received data: {e}")  

    # TODO: Commented out for engine-neuron integration - P2P removed
    # async def p2pInit(self):
    #     await self.connectToPeer()
    #     asyncio.create_task(self.monitorPublisherConnection())
    #     await self.startStreamService()
    #
    # async def startStreamService(self):
    #     await self.syncData()
    #     await self.makeSubscription()

    def updateDataClient(self, dataClient):
        ''' Update the internal server data client reference '''
        self.dataClientOfIntServer = dataClient

    # TODO: Commented out for engine-neuron integration - P2P removed
    # def returnPeerIp(self, peer: Union[str, None] = None) -> str:
    #     if peer is not None:
    #         return peer.split(':')[0]
    #     return self.publisherHost.split(':')[0]
    #
    # def returnPeerPort(self, peer: Union[str, None] = None) -> int:
    #     if peer is not None:
    #         return int(peer.split(':')[1])
    #     return int(self.publisherHost.split(':')[1])
    #
    # @property
    # def isConnectedToPublisher(self):
    #     if self.internal:
    #         if self.publisherHost is None:
    #             return False
    #         return True
    #     if hasattr(self, 'dataClientOfExtServer') and self.dataClientOfExtServer is not None and self.publisherHost is not None:
    #         return self.dataClientOfExtServer.isConnected(self.returnPeerIp(), self.returnPeerPort())
    #     return False
    #
    # async def monitorPublisherConnection(self):
    #     """Combined method that monitors connection status and stream activity"""
    #     while True:
    #         if self.internal:
    #             await asyncio.sleep(30)
    #             continue
    #         for _ in range(30):
    #             if not self.isConnectedToPublisher:
    #                 self.publisherHost = None
    #                 await self.dataClientOfIntServer.streamInactive(self.streamUuid)
    #                 await self.connectToPeer()
    #                 await self.startStreamService()
    #                 break
    #             await asyncio.sleep(9)
    #
    #         if self.publisherHost is not None and self.isConnectedToPublisher:
    #             if not await self._isPublisherActive():
    #                 await self.dataClientOfIntServer.streamInactive(self.streamUuid)
    #                 await self.connectToPeer()
    #                 await self.startStreamService()
    #
    # async def _isPublisherActive(self, publisher: str = None) -> bool:
    #     ''' confirms if the publisher has the subscription stream in its available stream '''
    #     try:
    #         response = await self.dataClientOfExtServer.isStreamActive(
    #                     peerHost=self.returnPeerIp(publisher) if publisher is not None else self.returnPeerIp(),
    #                     peerPort=self.returnPeerPort(publisher) if publisher is not None else self.returnPeerPort(),
    #                     uuid=self.streamUuid)
    #         if response.status == DataServerApi.statusSuccess.value:
    #             return True
    #         else:
    #             raise Exception
    #     except Exception as e:
    #         return False
    #
    # async def connectToPeer(self) -> bool:
    #     ''' Connects to a peer to receive subscription if it has an active subscription to the stream '''
    #
    #     async def check_peer_active(ip):
    #         """Only check if peer is active without establishing connection"""
    #         try:
    #             response = await self.dataClientOfExtServer.isStreamActive(
    #                 peerHost=self.returnPeerIp(ip),
    #                 peerPort=self.returnPeerPort(ip),
    #                 uuid=self.streamUuid
    #             )
    #             return response.status == DataServerApi.statusSuccess.value
    #         except Exception:
    #             return False
    #
    #     async def establish_connection(ip):
    #         """Actually establish the connection"""
    #         try:
    #             if await self._isPublisherActive(ip):
    #                 await self.dataClientOfIntServer.addActiveStream(uuid=self.streamUuid)
    #                 return True
    #         except Exception:
    #             pass
    #         return False
    #
    #     while not self.isConnectedToPublisher:
    #         if self.peerInfo.publishersIp is not None and len(self.peerInfo.publishersIp) > 0:
    #             candidate_publisher = self.peerInfo.publishersIp[0]
    #             if await establish_connection(candidate_publisher):
    #                 self.publisherHost = candidate_publisher
    #                 self.usePubSub = False
    #                 return True
    #
    #         response = await self.dataClientOfIntServer.isStreamActive(uuid=self.streamUuid)
    #         if response.status == DataServerApi.statusSuccess.value:
    #             info("Connected to Local Data Server", self.streamUuid)
    #             self.publisherHost = f"{self.dataClientOfIntServer.serverHostPort[0]}:{self.dataClientOfIntServer.serverPort}"
    #             self.internal = True
    #             self.usePubSub = False
    #             return True
    #
    #         subscriber_ips = [ip for ip in self.peerInfo.subscribersIp]
    #         self.rng.shuffle(subscriber_ips)
    #
    #         check_tasks = [(ip, asyncio.create_task(check_peer_active(ip))) for ip in subscriber_ips]
    #         active_peers = []
    #         for ip, task in check_tasks:
    #             try:
    #                 if await task:
    #                     active_peers.append(ip)
    #             except Exception as e:
    #                 error(f"Error checking peer {ip}: {str(e)}")
    #         if active_peers:
    #             selected_peer = active_peers[0]
    #             if await establish_connection(selected_peer):
    #                 self.publisherHost = selected_peer
    #                 self.usePubSub = False
    #                 return True
    #
    #         self.publisherHost = None
    #         warning('Failed to connect to Peers, switching to PubSub', self.streamUuid, print=True)
    #         self.usePubSub = True
    #         await asyncio.sleep(60*60)

    # TODO: Commented out for engine-neuron integration - P2P removed
    # async def syncData(self):
    #     '''
    #     - this can be highly optimized. but for now we do the simple version
    #     - just ask for their entire dataset every time
    #         - if it's different than the df we got from our own dataserver,
    #           then tell dataserver to save this instead
    #         - replace what we have
    #     '''
    #     try:
    #         if self.internal:
    #             return
    #         else:
    #             DataResponse = await self.dataClientOfExtServer.getRemoteStreamData(
    #                 peerHost=self.returnPeerIp(),
    #                 peerPort=self.returnPeerPort(),
    #                 uuid=self.streamUuid)
    #         if DataResponse.status == DataServerApi.statusSuccess.value:
    #             externalDf = DataResponse.data
    #             if not externalDf.equals(self.data) and len(externalDf) > 0:
    #                 response = await self.dataClientOfIntServer.insertStreamData(
    #                                 uuid=self.streamUuid,
    #                                 data=externalDf,
    #                                 replace=True
    #                             )
    #                 if response.status == DataServerApi.statusSuccess.value:
    #                     info("Data updated in server", color='green')
    #                     externalDf = externalDf.reset_index().rename(columns={
    #                                 'ts': 'date_time',
    #                                 'hash': 'id'
    #                             }).drop(columns=['provider'])
    #                     self.data = externalDf
    #                 else:
    #                     raise Exception(DataResponse.senderMsg)
    #             else:
    #                 raise Exception(DataResponse.senderMsg)
    #     except Exception as e:
    #         error("Failed to sync data, ", e)
    #         self.publisherHost = None
    #
    # async def makeSubscription(self):
    #     '''
    #     - and subscribe to the stream so we get the information
    #         - whenever we get an observation on this stream, pass to the DataServer
    #     - continually generate predictions for prediction publication streams and pass that to
    #     '''
    #     if self.internal:
    #         await self.dataClientOfIntServer.subscribe(
    #             uuid=self.streamUuid,
    #             publicationUuid=self.predictionStreamUuid,
    #             callback=self.handleSubscriptionMessage,
    #             engineSubscribed=True)
    #     else:
    #         await self.dataClientOfExtServer.subscribe(
    #             uuid=self.streamUuid,
    #             peerHost=self.returnPeerIp(),
    #             peerPort=self.returnPeerPort(),
    #             publicationUuid=self.predictionStreamUuid,
    #             callback=self.handleSubscriptionMessage,
    #             engineSubscribed=True)
    #
    # async def handleSubscriptionMessage(self, subscription: any,  message: Message, pubSubFlag: bool = False):
    #     debug(f"Stream {self.streamUuid} received subscription message (pubsub: {pubSubFlag})")
    #     if message.status == DataClientApi.streamInactive.value:
    #         warning("Stream Inactive")
    #         await self.closePeerConnection()
    #         self.publisherHost = None
    #     else:
    #         await self.appendNewData(message.data, pubSubFlag)
    #         self.pauseAll(force=True)
    #         await self.producePrediction()
    #         self.resumeAll(force=True)
    #
    # async def closePeerConnection(self):
    #     """Close the connection to the current publisher peer"""
    #     if self.internal:
    #         self.publisherHost = None
    #         return
    #     if self.publisherHost is not None and hasattr(self, 'dataClientOfExtServer') and self.dataClientOfExtServer is not None:
    #         try:
    #             peer = self.dataClientOfExtServer.peers.get((self.returnPeerIp(), self.returnPeerPort()))
    #             if peer is not None:
    #                 await self.dataClientOfExtServer.disconnect(peer)
    #                 info(f"Closed connection to peer {(self.returnPeerIp(), self.returnPeerPort())}")
    #             self.publisherHost = None
    #         except Exception as e:
    #             error(f"Error closing peer connection: {e}")
    #             self.publisherHost = None

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    async def appendNewData(self, observation: Union[pd.DataFrame, dict], pubSubFlag: bool):
        """extract the data and save it to self.data"""
        try:
            if pubSubFlag:
                parsedData = json.loads(observation.raw)
                debug(f"Stream {self.streamUuid} appending new data: {parsedData['data']} at {parsedData['time']}")
                if validate_single_entry(parsedData["time"], parsedData["data"]):
                        await self.dataClientOfIntServer.insertStreamData(
                                uuid=self.streamUuid,
                                data=pd.DataFrame({ 'value': [float(parsedData["data"])]
                                            }, index=[str(parsedData["time"])]),
                                isSub=True
                            )
                        self.data = pd.concat(
                            [
                                self.data,
                                pd.DataFrame({
                                    "date_time": [str(parsedData["time"])],
                                    "value": [float(parsedData["data"])],
                                    "id": [str(parsedData["hash"])]})
                            ],
                            ignore_index=True)
                else:
                    error("Row not added due to corrupt observation")
            else:
                observation_id = observation['hash'].values[0]
                # Check if self.data is not empty and if the ID already exists
                if not self.data.empty and observation_id in self.data['id'].values:
                    error("Row not added because observation with same ID already exists")
                elif validate_single_entry(observation.index[0], observation["value"].values[0]):
                    if not self.internal:
                        response = await self.dataClientOfIntServer.insertStreamData(
                                uuid=self.streamUuid,
                                data=observation,
                                isSub=True
                            )
                        if response.status == DataServerApi.statusSuccess.value:
                            info(response.senderMsg, color='green')
                        else:
                            raise Exception("Raw ", response.senderMsg)
                    observationDf = observation.reset_index().rename(columns={
                                'index': 'date_time',
                                'hash': 'id'
                            }).drop(columns=['provider'])
                    self.data = pd.concat([self.data, observationDf], ignore_index=True)
                else:
                    error("Row not added due to corrupt observation")
        except Exception as e:
            error("Subscription data not added", e)

    def passPredictionData(self, forecast: pd.DataFrame, passToCentralServer: bool = False):
        try:
            # If using Central Server mode, publish directly to Central
            if getattr(self, 'useServer', False) and self.server is not None:
                self.publishPredictionToServer(forecast)
                return

            # Original DataClient mode
            if not hasattr(self, 'activatePredictionStream') and passToCentralServer:
                asyncio.run(self.dataClientOfIntServer.addActiveStream(uuid=self.predictionStreamUuid))
                self.activatePredictionStream = True
            response = asyncio.run(self.dataClientOfIntServer.insertStreamData(
                            uuid=self.predictionStreamUuid,
                            data=forecast,
                            isSub=True,
                            replace=False if passToCentralServer else True,
                        ))
            if response.status == DataServerApi.statusSuccess.value:
                info("Prediction", response.senderMsg, color='green')
            else:
                raise Exception(response.senderMsg)
        except Exception as e:
            error('Failed to send Prediction to server : ', e)

    def publishPredictionToServer(self, forecast: pd.DataFrame):
        """Publish prediction directly to Central Server and store locally."""
        try:
            predictionValue = str(forecast['value'].iloc[0])
            observationTime = str(forecast.index[0])
            # Generate hash for the prediction
            import hashlib
            observationHash = hashlib.sha256(
                f"{predictionValue}{observationTime}".encode()
            ).hexdigest()[:16]

            # Store prediction locally in SQLite (table name = predictionStreamUuid)
            if hasattr(self, 'storage') and self.storage is not None:
                stored = self.storage.storePrediction(
                    predictionStreamUuid=self.predictionStreamUuid,
                    timestamp=observationTime,
                    value=float(predictionValue),
                    hash_val=observationHash,
                    provider='engine'
                )
                if stored:
                    debug(f"Prediction stored locally for stream {self.predictionStreamUuid}")

            # Use publication stream's topic
            topic = self.publicationStream.streamId.jsonId

            isSuccess = self.server.publish(
                topic=topic,
                data=predictionValue,
                observationTime=observationTime,
                observationHash=observationHash,
                isPrediction=True,
                useAuthorizedCall=True)

            if isSuccess:
                info(f"Prediction published to Central Server: {predictionValue} at {observationTime}", color='green')
            else:
                warning(f"Failed to publish prediction to Central Server")
        except Exception as e:
            error(f"Error publishing prediction to Central Server: {e}")

    def producePrediction(self, updatedModel=None):
        """
        triggered by
            - model replaced with a better one
            - new observation on the stream
        """
        try:
            model = updatedModel or self.stable
            if model is not None:
                forecast = model.predict(data=self.data)
                if isinstance(forecast, pd.DataFrame):
                    predictionDf = pd.DataFrame({ 'value': [StreamForecast.firstPredictionOf(forecast)]
                                    }, index=[datetimeToTimestamp(now())])
                    debug(predictionDf, print=True)
                    if updatedModel is not None:
                        self.passPredictionData(predictionDf)
                    else:
                        self.passPredictionData(predictionDf, True)
                else:
                    raise Exception('Forecast not in dataframe format')
        except Exception as e:
            error(e)
            self.fallback_prediction()

    def fallback_prediction(self):
        if os.path.isfile(self.modelPath()):
            try:
                os.remove(self.modelPath())
                debug("Deleted failed model file:", self.modelPath(), color="teal")
            except Exception as e:
                error(f'Failed to delete model file: {str(e)}')
        backupModel = self.defaultAdapters[-1]()
        try:
            trainingResult = backupModel.fit(data=self.data)
            if abs(trainingResult.status) == 1:
                self.producePrediction(backupModel)
        except Exception as e:
            error(f"Error training new model: {str(e)}")

    def loadData(self) -> pd.DataFrame:
        try:
            response = asyncio.run(self.dataClientOfIntServer.getLocalStreamData(uuid=self.streamUuid))
            if response.status == DataServerApi.statusSuccess.value:
                conformedData = response.data.reset_index().rename(columns={
                    'ts': 'date_time',
                    'hash': 'id'
                })
                del conformedData['provider']
                return conformedData
            else:
                raise Exception(response.senderMsg)
        except Exception as e:
            debug(e)
            return pd.DataFrame(columns=["date_time", "value", "id"])

    def modelPath(self) -> str:
        return (
            '/Satori/Neuron/models/veda/'
            f'{self.predictionStreamUuid}/'
            f'{self.adapter.__name__}.joblib')

    def chooseAdapter(self, inplace: bool = False) -> ModelAdapter:
        """
        everything can try to handle some cases
        Engine
            - low resources available - SKAdapter
            - few observations - SKAdapter
            - (mapping of cases to suitable adapters)
        examples: StartPipeline, SKAdapter, XGBoostPipeline, ChronosPipeline, DNNPipeline
        """
        # TODO: this needs to be aultered. I think the logic is not right. we
        #       should gather a list of adapters that can be used in the
        #       current condition we're in. if we're already using one in that
        #       list, we should continue using it until it starts to make bad
        #       predictions. if not, we should then choose the best one from the
        #       list - we should optimize after we gather acceptable options.

        if False: # for testing specific adapters
            adapter = XgbChronosAdapter
        else:
            import psutil
            availableRamGigs = psutil.virtual_memory().available / 1e9
            availableSwapGigs = psutil.swap_memory().free / 1e9
            totalAvailableRamGigs = availableRamGigs + availableSwapGigs
            adapter = None
            for p in self.preferredAdapters:
                if p in self.failedAdapters:
                    continue
                if p.condition(data=self.data, cpu=self.cpu, availableRamGigs=totalAvailableRamGigs) == 1:
                    adapter = p
                    break
            if adapter is None:
                for adapter in self.defaultAdapters:
                    if adapter not in self.failedAdapters:
                        break
                if adapter is None:
                    adapter = self.defaultAdapters[-1]
        if (
            inplace and (
                not hasattr(self, 'pilot') or
                not isinstance(self.pilot, adapter))
        ):
            info(
                f'AI Engine: stream id {self.streamUuid} '
                f'switching from {self.adapter.__name__} '
                f'to {adapter.__name__} on {self.streamUuid}',
                color='teal')
            self.adapter = adapter
            self.pilot = adapter(uid=self.streamUuid)
            self.pilot.load(self.modelPath())
        return adapter

    def run(self):
        """
        Main loop for generating models and comparing them to the best known
        model so far in order to replace it if the new model is better, always
        using the best known model to make predictions on demand.
        """
        while len(self.data) > 0:
            if self.paused:
                time.sleep(10)
                continue
            self.chooseAdapter(inplace=True)
            try:
                trainingResult = self.pilot.fit(data=self.data, stable=self.stable)
                if trainingResult.status == 1:
                    if self.pilot.compare(self.stable):
                        if self.pilot.save(self.modelPath()):
                            self.stable = copy.deepcopy(self.pilot)
                            info("stable model updated for stream:", self.streamUuid)
                else:
                    debug(f'model training failed on {self.streamUuid} waiting 10 minutes to retry')
                    self.failedAdapters.append(self.pilot)
                    time.sleep(600)
            except Exception as e:
                import traceback
                traceback.print_exc()
                error(e)
                try:
                    debug(self.pilot.dataset)
                except Exception as e:
                    pass


    def run_forever(self):
        '''Creates separate threads for running the model training loop'''

        if hasattr(self, 'thread') and self.thread and self.thread.is_alive():
            warning(f"Thread for model {self.streamUuid} already running. Not creating another.")
            return

        def training_loop_thread():
            try:
                self.run()
            except Exception as e:
                error(f"Error in training loop thread: {e}")
                import traceback
                traceback.print_exc()

        self.thread = threading.Thread(target=training_loop_thread, daemon=True)
        self.thread.start()


def main():
    engine = Engine()
    engine.initialize()
    # Keep running
    while True:
        time.sleep(60)

if __name__ == "__main__":
    main()