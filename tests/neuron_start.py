"""neuron-lite/start.py as `start`, for tests of StartupDag's real methods:
`from tests.neuron_start import start`.

start.py pulls in the AI engine, which no test of the neuron's own code needs.
Stub only that, and only while importing: a stub left in sys.modules is what
every test file imported after this one would get (patch.dict would also drop
numpy & co., which can't be imported twice).
"""
import importlib.util
import sys
from pathlib import Path
from unittest import mock

_START = Path(__file__).parent.parent / 'neuron-lite' / 'start.py'

_engine = mock.MagicMock()
_stubs = {
    'satoriengine': _engine,
    'satoriengine.veda': _engine.veda,
    'satoriengine.veda.engine': _engine.veda.engine}
_stubbed = [name for name in _stubs if name not in sys.modules]
sys.modules.update({name: _stubs[name] for name in _stubbed})
try:
    _spec = importlib.util.spec_from_file_location('_neuron_start', _START)
    start = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(start)
finally:
    for name in _stubbed:
        sys.modules.pop(name, None)
