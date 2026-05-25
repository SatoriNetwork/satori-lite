"""Training Queue Manager for efficient multi-stream training."""

import threading
import time
from queue import Queue, Empty
import psutil
from satorilib.logging import error, warning, info, debug


# Minimum sleep between consecutive training iterations of the same stream.
# Prevents the worker from hot-spinning when trainingDelay is 0 or unset and
# nothing has changed (e.g. ETS fast-path returning instantly).
MIN_REQUEUE_DELAY_SECONDS = 5

# Wall-clock cap on a single training iteration. Generous enough that any
# legitimate fit (XGB seconds, ETS milliseconds) never trips it; catches
# true hangs (pathological data, stuck I/O, infinite loops in adapter
# code). On timeout the worker abandons the daemon thread and moves on;
# the zombie thread cleans up at process exit.
TRAINING_ITERATION_TIMEOUT_SECONDS = 3600


class TrainingQueueManager:
    """Manages training queue with single worker thread."""

    def __init__(self):
        self.queue = Queue()
        self.current_stream = None
        self.lock = threading.Lock()
        self.worker_thread = None
        self.running = False

    def start_worker(self):
        """Start single worker thread that processes training queue."""
        if self.worker_thread is not None and self.worker_thread.is_alive():
            warning("Training worker already running")
            return

        self.running = True
        self.worker_thread = threading.Thread(
            target=self._training_worker,
            daemon=True,
            name="TrainingWorker"
        )
        self.worker_thread.start()
        info("Training queue worker started (sequential training)", color='green')

    def stop_worker(self):
        """Stop the training worker thread."""
        self.running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5)

    def _training_worker(self):
        """Worker thread that processes training tasks sequentially."""
        while self.running:
            try:
                # Get next training task (blocks if queue empty, timeout after 10s)
                stream_model = self.queue.get(timeout=10)

                # Check resources before training
                if not self._has_sufficient_resources():
                    # Re-queue and wait
                    self.queue.put(stream_model)
                    time.sleep(5)
                    continue

                # Perform training
                self._train_stream(stream_model)

            except Empty:
                # Queue is empty, continue waiting
                continue
            except Exception as e:
                error(f"Training worker error: {e}")
                time.sleep(1)

    def _has_sufficient_resources(self):
        """Check if system has resources for training."""
        try:
            mem = psutil.virtual_memory()
            # With single worker, just check memory (CPU won't be overloaded)
            # Require at least 500MB free memory
            return mem.available / 1e9 > 0.5
        except Exception as e:
            error(f"Error checking resources: {e}")
            return True  # If we can't check, proceed anyway

    def _train_stream(self, stream_model):
        """Execute one training iteration for a stream."""
        with self.lock:
            self.current_stream = stream_model.streamUuid

        uuid8 = stream_model.streamUuid[:8]
        try:
            # Run the iteration in a daemon thread so a hang can't pin
            # the whole queue. On timeout the thread is abandoned
            # (continues running in background) and we move on.
            captured_exc: list = [None]
            def _runner():
                try:
                    stream_model._single_training_iteration()
                except Exception as e:
                    captured_exc[0] = e
            t = threading.Thread(
                target=_runner,
                daemon=True,
                name=f"fit-{uuid8}")
            t.start()
            t.join(timeout=TRAINING_ITERATION_TIMEOUT_SECONDS)
            if t.is_alive():
                error(f"Training timed out for {uuid8} after "
                      f"{TRAINING_ITERATION_TIMEOUT_SECONDS}s "
                      f"(adapter={stream_model.adapter.__name__}); "
                      f"abandoning thread, continuing queue")
            elif captured_exc[0] is not None:
                error(f"Training failed for {uuid8}: {captured_exc[0]}")
        finally:
            with self.lock:
                self.current_stream = None
            delay = max(stream_model.trainingDelay, MIN_REQUEUE_DELAY_SECONDS)
            time.sleep(delay)
            self.queue_training(stream_model)

    def queue_training(self, stream_model):
        """Add stream to training queue."""
        self.queue.put(stream_model)

    def get_queue_status(self):
        """Return queue statistics."""
        with self.lock:
            current = self.current_stream

        return {
            'queued': self.queue.qsize(),
            'current': current[:8] if current else None,
            'worker_alive': self.worker_thread.is_alive() if self.worker_thread else False
        }


# Global training manager (singleton)
_training_manager = None
_manager_lock = threading.Lock()


def get_training_manager():
    """Get or create global training queue manager (thread-safe singleton)."""
    global _training_manager

    if _training_manager is None:
        with _manager_lock:
            # Double-check pattern
            if _training_manager is None:
                _training_manager = TrainingQueueManager()
                _training_manager.start_worker()

    return _training_manager
