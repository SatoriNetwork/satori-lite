from typing import Union
import time
import copy
import warnings
import threading
import hashlib
import pandas as pd
from satorilib.concepts import Stream
from satorilib.logging import INFO, setup, debug, info, warning, error
from satorilib.utils.system import getProcessorCount
from satorilib.utils.time import datetimeToUnixTimestamp, now
from satorilib.server import SatoriServerClient
from satoriengine.veda.data import StreamForecast
from satoriengine.veda.adapters import ModelAdapter, StarterAdapter, XgbAdapter, XgbChronosAdapter, ETSAdapter, TimesFmAdapter
from satoriengine.veda.storage import EngineStorageManager


# ---------------------------------------------------------------------------
# Adapter selection
# ---------------------------------------------------------------------------
# Registry of all user-selectable adapters. Values may be None when the
# underlying dependency isn't installed; those entries are filtered from the
# user-visible choices below. xgb-chronos is intentionally not exposed yet —
# enable it by adding it back to the registry once torch is shipped.
ADAPTER_REGISTRY = {
    'ets':     ETSAdapter,
    'xgboost': XgbAdapter,
    'starter': StarterAdapter,
    'timesfm': TimesFmAdapter,
}

# Auto-selection order preserves the historical default (XGBoost → Starter)
# so existing neurons don't silently migrate to ETS on upgrade. Users must
# opt in to ETS via the settings UI.
AUTO_ADAPTERS = [XgbAdapter, StarterAdapter]

# Universal minimum-observations threshold. Any stream with fewer than this
# many rows is forced onto StarterAdapter regardless of user preference or
# any per-adapter condition() opinion. This is the single source of truth —
# new adapters should not duplicate this check.
MIN_OBSERVATIONS_FOR_TRAINED_MODEL = 10

# Only expose adapters whose dependency loaded successfully.
VALID_ADAPTER_CHOICES = ['auto'] + [
    k for k, v in ADAPTER_REGISTRY.items() if v is not None
]


def _getPreferredAdapterSetting() -> str:
    """Read engine.preferred_adapter from neuron config; 'auto' if absent."""
    try:
        from satorineuron import config as _neuronConfig
        cfg = _neuronConfig.get() or {}
        engineCfg = cfg.get('engine') or {}
        return engineCfg.get('preferred_adapter', 'auto') or 'auto'
    except Exception:
        return 'auto'


def buildPreferredAdapters() -> list:
    """
    Resolve the preferredAdapters list from neuron config.

    - 'auto'  → ETS → XGBoost → Starter (skipping any None entries)
    - explicit key → user's pick first, Starter appended as a safety net
    """
    choice = _getPreferredAdapterSetting()
    auto = [a for a in AUTO_ADAPTERS if a is not None]
    if choice == 'auto':
        return auto or [StarterAdapter]
    chosen = ADAPTER_REGISTRY.get(choice)
    if chosen is None:
        return auto or [StarterAdapter]
    # Chosen first, then the auto chain (XGBoost -> Starter) as a fallback so a
    # stream the chosen adapter declines (e.g. TimesFM below its 350-point /
    # 2 GB floor) lands on a real trained adapter instead of being forced back
    # onto the chosen one by chooseAdapter's condition-agnostic fallback loop.
    result = [chosen]
    for a in auto:
        if a is not None and a is not chosen and a not in result:
            result.append(a)
    if StarterAdapter is not None and StarterAdapter not in result:
        result.append(StarterAdapter)
    return result

warnings.filterwarnings('ignore')

# Default stream UUID to hide from logs (central-lite placeholder stream)
DEFAULT_STREAM_UUID = "a99072f1-421f-5ad8-87e0-8c3766b93810"
setup(level=INFO)



class Engine:

    @classmethod
    def createFromNeuron(
        cls,
        subscriptions: list,
        publications: list,
        server: SatoriServerClient,
    ) -> 'Engine':
        """
        Factory method for Neuron to spawn Engine with stream assignments.

        Args:
            subscriptions: List of Stream objects to subscribe to
            publications: List of Stream objects for predictions
            server: SatoriServerClient instance from Neuron
        """
        engine = cls()
        engine.server = server
        engine.subscriptionStreams = subscriptions
        engine.publicationStreams = publications
        return engine

    def __init__(self):
        self.streamModels: dict[str, StreamModel] = {}
        self.subscriptionStreams: list = []
        self.publicationStreams: list = []
        self.server: Union[SatoriServerClient, None] = None
        # SQLite storage manager for local data persistence
        self.storage: EngineStorageManager = EngineStorageManager.getInstance()
        # Prediction queue for batch submission
        self.predictionQueue: list[dict] = []
        self.predictionQueueLock: threading.Lock = threading.Lock()

    def initializeFromNeuron(self):
        """Initialize the models of the neuron's stream assignments"""
        info("Engine initializing from Neuron...", color='blue')
        self.initializeModelsFromNeuron()
        # info("Engine initialized successfully", color='green')

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
                    subscriptionStream=sub,
                    storage=self.storage)
                self.streamModels[subUuid].chooseAdapter(inplace=True)
                self.streamModels[subUuid].run_forever()
                if subUuid != DEFAULT_STREAM_UUID:
                    info(f"Model initialized for stream {subUuid}", color='green')
            except Exception as e:
                error(f"Failed to initialize model for stream {subUuid}: {e}")

    def queuePrediction(self, stream_uuid: str, stream_name: str, value: str, observed_at: str, hash_val: str, t1_value: str = None):
        """Add a prediction to the queue for batch submission."""
        with self.predictionQueueLock:
            self.predictionQueue.append({
                'stream_uuid': stream_uuid,
                'stream_name': stream_name,
                'value': value,
                't1_value': t1_value,
                'observed_at': observed_at,
                'hash': hash_val
            })
            debug(f"Queued prediction for {stream_name} (queue size: {len(self.predictionQueue)})")

    def flushPredictionQueue(self) -> Union[dict, None]:
        """Submit all queued predictions in a batch and clear the queue."""
        with self.predictionQueueLock:
            if not self.predictionQueue:
                return None

            predictions_to_submit = self.predictionQueue.copy()
            queue_size = len(predictions_to_submit)

        # Submit batch outside of lock
        if self.server is not None:
            info(f"Submitting batch of {queue_size} predictions to server...", color='cyan')
            result = self.server.publishPredictionsBatch(predictions_to_submit)

            if result and result.get('successful', 0) > 0:
                # Clear queue only if submission was successful
                with self.predictionQueueLock:
                    self.predictionQueue = []
                info(f"✓ Batch submitted: {result['successful']}/{result['total_submitted']} successful", color='green')
                return result
            else:
                warning(f"Batch prediction submission failed, keeping {queue_size} predictions in queue for retry")
                return None
        else:
            warning("Server not initialized, cannot submit batch predictions")
            return None


class StreamModel:

    @classmethod
    def createFromServer(
        cls,
        streamUuid: str,
        predictionStreamUuid: str,
        server: SatoriServerClient,
        subscriptionStream: Stream,
        storage: EngineStorageManager = None,
        sink: str = 'central',
    ):
        """Factory method for creating StreamModel that uses Central Server directly

        ``sink`` identifies where this model's predictions go: ``'central'`` (the
        default) means the neuron batch-submits them over HTTP; ``'relay'`` means
        the relay path reads the value back via ``predictForRelay`` and publishes
        it to Nostr itself (it never enters the central batch).
        """
        streamModel = cls()
        # Serializes onDataReceived / predictForRelay against the shared training
        # worker (which swaps self.stable/self.pilot) and against concurrent
        # same-stream observations. Reentrant because producePrediction may recurse
        # via fallback_prediction while the lock is held.
        streamModel._modelLock = threading.RLock()
        streamModel.sink = sink
        streamModel.cpu = getProcessorCount()
        # Config-driven: reads engine.preferred_adapter from neuron config
        # ('auto' = [ETS, XGB, Starter]; explicit key forces that adapter).
        streamModel.preferredAdapters = buildPreferredAdapters()
        streamModel.defaultAdapters = buildPreferredAdapters()
        streamModel.failedAdapters = []
        streamModel.streamUuid = streamUuid
        streamModel.predictionStreamUuid = predictionStreamUuid
        streamModel.subscriptionStream = subscriptionStream
        streamModel.server = server
        # SQLite storage manager for local persistence
        streamModel.storage = storage or EngineStorageManager.getInstance()
        streamModel.trainingDelay = streamModel._loadTrainingDelay()  # Load from config
        streamModel.initializeFromServer()
        return streamModel

    def initializeFromServer(self):
        """Initialize model when using Central Server directly"""
        self.data: pd.DataFrame = self.loadDataFromServer()
        self.adapter: ModelAdapter = self.chooseAdapter()
        self.pilot: ModelAdapter = self.adapter(uid=self.streamUuid)
        self.pilot.load(self.modelPath())
        self.stable: ModelAdapter = copy.deepcopy(self.pilot)
        if self.streamUuid != DEFAULT_STREAM_UUID:
            debug(f'AI Engine: stream id {self.streamUuid} using {self.adapter.__name__} (Central Server mode)', color='teal')

    def _loadTrainingDelay(self) -> int:
        """Load training delay from config file.

        Returns:
            int: Delay in seconds (default 600 = 10 minutes)
        """
        try:
            from satorineuron import config
            delay = config.get().get('training_delay', 600)
            return int(delay)
        except Exception as e:
            warning(f"Failed to load training delay from config: {e}")
            return 600  # Default to 10 minutes

    def loadDataFromServer(self) -> pd.DataFrame:
        """Load historical data - currently loads from local SQLite only."""
        # TODO: Implement fetching from Central Server when API is ready
        # For now, load from local SQLite if available
        localData = self.storage.getStreamDataForEngine(self.streamUuid)
        if not localData.empty:
            if self.streamUuid != DEFAULT_STREAM_UUID:
                info(f"Loaded {len(localData)} rows from local SQLite for stream {self.streamUuid}", color='green')
            return localData
        if self.streamUuid != DEFAULT_STREAM_UUID:
            info(f"No local data for stream {self.streamUuid}, starting fresh", color='yellow')
        return pd.DataFrame(columns=["date_time", "value", "id"])

    def _ingestData(self, data: pd.DataFrame) -> int:
        """Store new observations, update in-memory history, (re)choose adapter.

        Shared by the central poll path (onDataReceived) and the relay path
        (predictForRelay). Returns the number of newly stored rows. Callers hold
        ``self._modelLock``.

        Args:
            data: DataFrame with columns [date_time (datetime64 UTC), value, id]
        """
        if data.empty:
            return 0

        # Build storage frame [epoch, value, id] from the datetime64 column.
        # This is the only epoch extraction point — no string round-trips.
        storageDf = pd.DataFrame({
            'epoch': data['date_time'].astype('int64') // 10 ** 9,
            'value': data['value'],
            'id': data['id'],
        })
        insertedRows = self.storage.storeStreamData(self.streamUuid, storageDf)
        if insertedRows > 0:
            info(f"Stored {insertedRows} new rows for stream {self.streamUuid}", color='green')

        # Update in-memory history — append only rows not already present.
        new_rows = data[~data['date_time'].isin(self.data['date_time'])]
        if not new_rows.empty:
            self.data = pd.concat([self.data, new_rows], ignore_index=True)

        # Check if adapter should be upgraded (e.g., StarterAdapter -> XgbAdapter)
        previousAdapter = self.adapter.__name__
        self.chooseAdapter(inplace=True)

        # If upgraded from StarterAdapter to a real adapter, join the training queue
        if previousAdapter == 'StarterAdapter' and self.adapter.__name__ != 'StarterAdapter':
            if self.streamUuid != DEFAULT_STREAM_UUID:
                info(f"Stream {self.streamUuid[:8]} upgraded from StarterAdapter to {self.adapter.__name__}, joining training queue", color='green')
            self.run_forever()

        return insertedRows

    def onDataReceived(self, data: pd.DataFrame):
        """
        Called when new data is received from Central Server.

        Args:
            data: DataFrame with columns [date_time (datetime64 UTC), value, id]
        """
        try:
            with self._modelLock:
                insertedRows = self._ingestData(data)

                # Trigger prediction when new observation arrives
                if insertedRows > 0:
                    info(f"New observation received, triggering prediction for stream {self.streamUuid}", color='blue')
                    self.producePrediction()

        except Exception as e:
            error(f"Error storing received data: {e}")

    def predictForRelay(self, data: pd.DataFrame) -> Union[str, None]:
        """Relay entry point: ingest a frame and RETURN the forecast value string.

        Unlike onDataReceived, this does NOT route the prediction into the central
        batch-submission channel (``_pending_prediction``) — the relay caller
        publishes the returned value to Nostr itself. It predicts on every call
        (not gated on newly-stored rows) so the relay always emits one prediction
        per observation, even when an observation collides with an existing
        integer-second epoch in storage.

        Args:
            data: 1-row DataFrame with columns [date_time (datetime64 UTC), value, id]

        Returns:
            The forecast value as a string, or None if no forecast could be made.
        """
        try:
            with self._modelLock:
                self._ingestData(data)
                forecast = self._runForecast(self.stable)
                if isinstance(forecast, pd.DataFrame):
                    return str(StreamForecast.firstPredictionOf(forecast))
                return None
        except Exception as e:
            error(f"Relay prediction failed for stream {self.streamUuid}: {e}")
            return None

    def passPredictionData(self, forecast: pd.DataFrame):
        """Store the prediction locally and leave it in _pending_prediction,
        which the neuron's collectAndSubmitPredictions batches to Central."""
        if self.server is None:
            error('Failed to send Prediction to server : no server')
            return
        try:
            predictionValue = str(forecast['value'].iloc[0])
            observationTime = str(forecast.index[0])
            observationHash = hashlib.sha256(
                f"{predictionValue}{observationTime}".encode()
            ).hexdigest()[:16]

            # Store prediction locally in SQLite (table name = predictionStreamUuid)
            if getattr(self, 'storage', None) is not None:
                stored = self.storage.storePrediction(
                    predictionStreamUuid=self.predictionStreamUuid,
                    timestamp=observationTime,
                    value=float(predictionValue),
                    hash_val=observationHash,
                    provider='engine'
                )
                if stored:
                    debug(f"Prediction stored locally for stream {self.predictionStreamUuid}")

            stream_name = getattr(self.subscriptionStream.streamId, 'stream', 'unknown')
            debug(f"Prediction ready for batching: {stream_name} = {predictionValue}")
            self._pending_prediction = {
                'stream_uuid': self.streamUuid,
                'stream_name': stream_name,
                'value': predictionValue,
                't1_value': getattr(self, '_t1_value', None),
                'observed_at': observationTime,
                'hash': observationHash
            }
        except Exception as e:
            error(f"Error publishing prediction to Central Server: {e}")

    def _createAugmentedData(self, firstForecast: pd.DataFrame) -> pd.DataFrame:
        try:
            firstValue = StreamForecast.firstPredictionOf(firstForecast)
            if 'date_time' in firstForecast.columns:
                timestamp = firstForecast['date_time'].iloc[0]
            elif 'ds' in firstForecast.columns:
                timestamp = firstForecast['ds'].iloc[0]
            else:
                timestamp = now()

            tempHash = hashlib.sha256(
                f"{firstValue}{timestamp}".encode()
            ).hexdigest()[:16]

            tempRow = pd.DataFrame({
                'date_time': [timestamp],
                'value': [firstValue],
                'id': [tempHash]
            })
            return pd.concat([self.data, tempRow], ignore_index=True)

        except Exception as e:
            error(f"Error creating augmented data for autoregression: {e}")
            return self.data

    def _runForecast(self, model) -> Union[pd.DataFrame, None]:
        """Predict + 2-step autoregression; return the final forecast DataFrame.

        Returns None when the model is missing or its first prediction is not a
        DataFrame; returns the first forecast when only the second (autoregressed)
        prediction fails. Sets self._t1_value as a side effect, matching the prior
        inline behavior. Shared by producePrediction (central sink) and
        predictForRelay (returns the value). Callers hold self._modelLock.
        """
        if model is None:
            return None

        firstForecast = model.predict(data=self.data)

        # Only do autoregression if the first prediction is valid
        if not isinstance(firstForecast, pd.DataFrame):
            warning(f"First prediction returned {type(firstForecast).__name__} instead of DataFrame (value: {str(firstForecast)[:100]}), skipping autoregression")
            return None

        firstValue = StreamForecast.firstPredictionOf(firstForecast)
        debug(f"[AUTOREGRESSION] First prediction: {firstValue}", color='cyan')
        self._t1_value = str(firstValue)

        augmentedData = self._createAugmentedData(firstForecast)
        debug(f"[AUTOREGRESSION] Augmented data size: {len(augmentedData)} rows (original: {len(self.data)})", color='cyan')

        secondForecast = model.predict(data=augmentedData)
        if isinstance(secondForecast, pd.DataFrame):
            secondValue = StreamForecast.firstPredictionOf(secondForecast)
            debug(f"[AUTOREGRESSION] Second prediction: {secondValue}", color='cyan')
            return secondForecast

        warning(f"Second prediction returned {type(secondForecast).__name__} instead of DataFrame, using first prediction only")
        return firstForecast

    def producePrediction(self, updatedModel=None):
        """
        triggered by
            - model replaced with a better one
            - new observation on the stream
        """
        try:
            model = updatedModel or self.stable
            if model is None:
                return
            forecast = self._runForecast(model)
            if isinstance(forecast, pd.DataFrame):
                # Use Unix timestamp for consistency with observation storage
                predictionDf = pd.DataFrame({ 'value': [StreamForecast.firstPredictionOf(forecast)]
                                }, index=[datetimeToUnixTimestamp(now())])
                debug(predictionDf, print=True)
                self.passPredictionData(predictionDf)
            else:
                raise Exception(f'Forecast not in DataFrame format - got {type(forecast).__name__}')
        except Exception as e:
            error(f"Prediction failed for stream {self.streamUuid}: {type(e).__name__}: {e}")
            self.fallback_prediction()

    def fallback_prediction(self):
        """Fallback when prediction fails - use backup model without deleting existing model.

        The model file might be fine; the issue could be with prediction logic or data format.
        We'll let the normal training cycle replace the model if needed.
        """
        warning(f"Prediction failed for stream {self.streamUuid}, using fallback model (keeping existing model file)")
        backupModel = self.defaultAdapters[-1]()
        try:
            trainingResult = backupModel.fit(data=self.data)
            if abs(trainingResult.status) == 1:
                self.producePrediction(backupModel)
            else:
                warning(f"Fallback model training failed with status {trainingResult.status}")
        except Exception as e:
            error(f"Error training fallback model for stream {self.streamUuid}: {str(e)}")

    def modelPath(self) -> str:
        return (
            '/Satori/Neuron/models/veda/'
            f'{self.predictionStreamUuid}/'
            f'{self.adapter.__name__}.joblib')

    def chooseAdapter(self, inplace: bool = False) -> ModelAdapter:
        # TODO: this needs to be aultered. I think the logic is not right. we
        #       should gather a list of adapters that can be used in the
        #       current condition we're in. if we're already using one in that
        #       list, we should continue using it until it starts to make bad
        #       predictions. if not, we should then choose the best one from the
        #       list - we should optimize after we gather acceptable options.

        # Universal threshold: too little data for any trained model.
        # See MIN_OBSERVATIONS_FOR_TRAINED_MODEL at module top.
        if len(self.data) < MIN_OBSERVATIONS_FOR_TRAINED_MODEL:
            adapter = StarterAdapter
        elif False: # for testing specific adapters
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

    def _single_training_iteration(self):
        """Execute one training iteration (called by queue worker).

        Held under self._modelLock for its full duration so the worker's mutation
        of self.pilot / self.stable / self.data cannot race a concurrent
        producePrediction or predictForRelay on the same model. The lock is
        per-model, so this only serializes a prediction against training of the
        SAME stream (and the single training worker trains one stream at a time
        regardless); other streams are unaffected.
        """
        with self._modelLock:
            if len(self.data) == 0:
                return

            self.chooseAdapter(inplace=True)

            # Skip training for StarterAdapter (it doesn't actually train)
            if self.adapter.__name__ == 'StarterAdapter':
                return

            try:
                trainingResult = self.pilot.fit(data=self.data, stable=self.stable)
                if trainingResult.status == 1:
                    if self.pilot.compare(self.stable):
                        # Model improved - save with retry logic
                        saveSuccess = False
                        for attempt in range(3):
                            if self.pilot.save(self.modelPath()):
                                saveSuccess = True
                                break
                            if attempt < 2:
                                warning(f"Model save attempt {attempt + 1}/3 failed, retrying...")
                                time.sleep(5)

                        if saveSuccess:
                            self.stable = copy.deepcopy(self.pilot)
                            if self.streamUuid != DEFAULT_STREAM_UUID:
                                info(f"Model improved and saved: {self.streamUuid[:8]}", color='green')
                        else:
                            if self.streamUuid != DEFAULT_STREAM_UUID:
                                error(f"Failed to save improved model after 3 attempts for {self.streamUuid[:8]}")
                else:
                    if self.streamUuid != DEFAULT_STREAM_UUID:
                        warning(f'Training failed for {self.streamUuid[:8]} (status={trainingResult.status})')
                    self.failedAdapters.append(self.pilot)
            except Exception as e:
                # Discard the broken pilot so we don't keep retrying with bad
                # state (e.g. a loaded model whose schema doesn't match current
                # data). The next training tick will construct a fresh adapter
                # and rebuild from current observations.
                error(
                    f"Training error for {self.streamUuid[:8]}: {e} "
                    f"— resetting pilot ({self.adapter.__name__})")
                try:
                    self.pilot = self.adapter(uid=self.streamUuid)
                except Exception as reset_err:
                    error(
                        f"Failed to reset pilot for {self.streamUuid[:8]}: "
                        f"{reset_err}")

    def run_forever(self):
        '''Register stream with training queue for efficient resource management'''
        # Skip queuing for adapters that don't actually train: StarterAdapter
        # (naive predictions) and TimesFmAdapter (zero-shot foundation model).
        if self.adapter.__name__ in ('StarterAdapter', 'TimesFmAdapter'):
            return

        # Import here to avoid circular dependency
        from satoriengine.veda.training.queue_manager import get_training_manager

        # Get global training queue manager
        manager = get_training_manager()

        # Queue this stream for training
        manager.queue_training(self)

        # Log registration (only for non-default streams)
        if self.streamUuid != DEFAULT_STREAM_UUID:
            debug(f"Registered stream {self.streamUuid[:8]} with training queue", color='cyan')
