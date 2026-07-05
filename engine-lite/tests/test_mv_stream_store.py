"""Unit tests for ``StreamStore.count_streams_with_min_rows`` (Jordan-1
multivariate adapter §5 gate: "fewer than 2 streams with >= 30 rows -> 0.0,
single SQL count").

Uses a temp-dir sqlite db per test so runs never touch a real StreamStore
file. Tries the normal package import first (``satoriengine.stream_store``
has no heavy deps — just sqlite3/threading/pandas) and falls back to
importlib-by-path if that ever changes.

Runs under pytest (``python -m pytest``) or standalone
(``python test_mv_stream_store.py``) since the image ships no pytest.
"""

import importlib.util
import os
import tempfile

import pandas as pd

try:
    from satoriengine.stream_store import StreamStore
except Exception:
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _STORE_PATH = os.path.join(_HERE, '..', 'satoriengine', 'stream_store.py')
    _spec = importlib.util.spec_from_file_location('mv_stream_store', _STORE_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    StreamStore = _mod.StreamStore


def _new_store(tmpdir, name='test.db'):
    return StreamStore(os.path.join(tmpdir, name))


def _rows(n, start_epoch=1_700_000_000.0, step=3600.0):
    """A storage-shaped frame (columns: epoch, value, id) with n rows."""
    return pd.DataFrame({
        'epoch': [start_epoch + step * i for i in range(n)],
        'value': [float(i) for i in range(n)],
        'id': [f'h{i}' for i in range(n)],
    })


def test_empty_store_returns_zero():
    with tempfile.TemporaryDirectory() as tmp:
        store = _new_store(tmp)
        assert store.count_streams_with_min_rows(30) == 0
        store.close()


def test_mixed_row_counts_only_counts_streams_meeting_threshold():
    with tempfile.TemporaryDirectory() as tmp:
        store = _new_store(tmp)
        store.append('few', _rows(5))
        store.append('exact', _rows(30))
        store.append('many', _rows(31))
        assert store.count_streams_with_min_rows(30) == 2
        store.close()


def test_boundary_exactly_min_rows_counts():
    with tempfile.TemporaryDirectory() as tmp:
        store = _new_store(tmp)
        store.append('boundary', _rows(30))
        assert store.count_streams_with_min_rows(30) == 1
        assert store.count_streams_with_min_rows(31) == 0
        store.close()


def test_min_rows_one_counts_every_stream_with_data():
    with tempfile.TemporaryDirectory() as tmp:
        store = _new_store(tmp)
        store.append('a', _rows(1))
        store.append('b', _rows(5))
        store.append('c', _rows(30))
        assert store.count_streams_with_min_rows(1) == 3
        store.close()


def test_interleaved_with_other_store_methods():
    with tempfile.TemporaryDirectory() as tmp:
        store = _new_store(tmp)

        store.append('x', _rows(10))
        assert store.count_streams_with_min_rows(30) == 0
        assert store.row_count('x') == 10

        store.append('x', _rows(35))
        assert store.row_count('x') == 35
        assert store.count_streams_with_min_rows(30) == 1

        store.append('y', _rows(40))
        assert sorted(store.stream_uuids()) == ['x', 'y']
        assert store.count_streams_with_min_rows(30) == 2

        history = store.history('x')
        assert len(history) == 35
        assert list(history.columns) == ['date_time', 'value', 'id']

        store.close()


if __name__ == '__main__':
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith('test_') and callable(v)]
    passed = failed = 0
    for fn in tests:
        try:
            fn()
            passed += 1
            print(f'PASS {fn.__name__}')
        except Exception:
            failed += 1
            print(f'FAIL {fn.__name__}')
            traceback.print_exc()
    print(f'\n{passed} passed, {failed} failed, {len(tests)} total')
    raise SystemExit(1 if failed else 0)
