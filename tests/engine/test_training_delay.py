"""The training delay, the Training menu's frequency slider: a StreamModel reads
training_delay from the neuron's config (600 s when it is unset or not a
number), and the training queue waits that long, never under
MIN_REQUEUE_DELAY_SECONDS, before it queues the stream again. It reads the
model's delay at each wait, so the slider's POST, which sets it on the running
models, takes effect at the next one.
"""

import types

import pytest

from satorineuron import config
from satoriengine.veda.engine import StreamModel
from satoriengine.veda.training import queue_manager


@pytest.mark.parametrize('saved, delay', [
    ({'training_delay': 120}, 120),
    ({'training_delay': '45'}, 45),
    ({}, 600),
    ({'training_delay': 'invalid'}, 600),
])
def test_the_delay_comes_from_the_neuron_config(monkeypatch, saved, delay):
    monkeypatch.setattr(config, 'get', lambda *a, **k: saved)
    assert StreamModel._loadTrainingDelay(object.__new__(StreamModel)) == delay


class Model:
    streamUuid = 'stream-uuid-0001'

    def __init__(self, trainingDelay):
        self.trainingDelay = trainingDelay
        self.trained = 0

    def _single_training_iteration(self):
        self.trained += 1


@pytest.mark.parametrize('trainingDelay, waited', [
    (120, 120),
    (0, queue_manager.MIN_REQUEUE_DELAY_SECONDS),
])
def test_the_queue_waits_the_delay_then_queues_the_stream_again(monkeypatch, trainingDelay, waited):
    waits = []
    monkeypatch.setattr(queue_manager, 'time', types.SimpleNamespace(sleep=waits.append))
    manager = queue_manager.TrainingQueueManager()  # no worker thread: one iteration by hand
    model = Model(trainingDelay)
    manager._train_stream(model)
    assert model.trained == 1
    assert waits == [waited]
    assert manager.queue.get_nowait() is model
