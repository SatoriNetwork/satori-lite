"""Integration tests: lite engine through the start.py data path.

Tests the full flow: observation dicts (as returned by networkDB.get_observations)
→ LiteEngine.predict() → prediction string → save/publish.

The DB returns rows as dicts with columns:
    id, stream_name, provider_pubkey, seq_num, observed_at, received_at, value, event_id

LiteEngine reads obs.get('value', '') from each dict.
Results are ordered received_at DESC (newest first).
"""

import sys
import os
import json
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'engine-lite'))

from satoriengine.lite.lite_engine import LiteEngine


# ---------------------------------------------------------------------------
# Helpers — build observation dicts exactly as networkDB.get_observations does
# ---------------------------------------------------------------------------

def _db_row(value, seq_num=1, stream_name='test/stream', pubkey='npub1abc',
            event_id=None, observed_at=None, received_at=None, row_id=1):
    """Build a dict matching the observations table schema."""
    return {
        'id': row_id,
        'stream_name': stream_name,
        'provider_pubkey': pubkey,
        'seq_num': seq_num,
        'observed_at': observed_at or int(time.time()),
        'received_at': received_at or int(time.time()),
        'value': value,
        'event_id': event_id or f'event-{row_id}',
    }


def _db_rows(values, stream_name='test/stream', pubkey='npub1abc'):
    """Build newest-first observation rows (matching ORDER BY received_at DESC)."""
    now = int(time.time())
    rows = []
    for i, v in enumerate(values):
        rows.append(_db_row(
            value=v,
            seq_num=len(values) - i,
            row_id=len(values) - i,
            received_at=now - i,  # newest first
            observed_at=now - i,
            stream_name=stream_name,
            pubkey=pubkey,
        ))
    return rows


@pytest.fixture
def engine():
    return LiteEngine()


# ===================================================================
# Test the exact data format from networkDB
# ===================================================================

class TestDBRowFormat:
    """Ensure LiteEngine handles the full dict with extra columns."""

    def test_single_row_with_all_columns(self, engine):
        rows = [_db_row(value='42.5')]
        result = engine.predict(rows)
        assert result == '42.5'

    def test_ignores_extra_columns(self, engine):
        rows = [_db_row(value='10.0')]
        # Add unexpected columns that might appear in future schema changes
        rows[0]['extra_col'] = 'should be ignored'
        rows[0]['metadata'] = {'foo': 'bar'}
        result = engine.predict(rows)
        assert result == '10.0'

    def test_multiple_rows_newest_first(self, engine):
        # DB returns newest first: values [30, 20, 10]
        # LiteEngine reverses to chronological [10, 20, 30], mean of last 2 = 25
        rows = _db_rows(['30.0', '20.0', '10.0'])
        result = engine.predict(rows)
        assert float(result) == pytest.approx(25.0)


# ===================================================================
# Test value formats as they come from Nostr events
# ===================================================================

class TestNostrValueFormats:
    """Values arrive as strings from observation.to_json() through Nostr."""

    def test_plain_float_string(self, engine):
        rows = _db_rows(['42.5'])
        assert engine.predict(rows) == '42.5'

    def test_plain_integer_string(self, engine):
        rows = _db_rows(['100'])
        assert float(engine.predict(rows)) == pytest.approx(100.0)

    def test_json_encoded_number(self, engine):
        # observation.to_json() might produce a JSON number as string
        rows = _db_rows([json.dumps(42.5)])
        assert float(engine.predict(rows)) == pytest.approx(42.5)

    def test_json_quoted_string_number(self, engine):
        # Double-encoded: '"42.5"' (JSON string containing a number)
        rows = _db_rows([json.dumps('42.5')])
        assert float(engine.predict(rows)) == pytest.approx(42.5)

    def test_scientific_notation(self, engine):
        rows = _db_rows(['1.5e3'])
        assert float(engine.predict(rows)) == pytest.approx(1500.0)

    def test_negative_number_string(self, engine):
        rows = _db_rows(['-7.5'])
        assert float(engine.predict(rows)) == pytest.approx(-7.5)

    def test_zero_string(self, engine):
        rows = _db_rows(['0', '0', '0', '0', '0'])
        assert float(engine.predict(rows)) == pytest.approx(0.0)

    def test_leading_whitespace(self, engine):
        # Nostr relay might not strip whitespace
        rows = _db_rows([' 42.5 '])
        result = engine.predict(rows)
        # Should handle or gracefully fail
        if result is not None:
            assert float(result) == pytest.approx(42.5)


class TestJsonObservationObjects:
    """Values stored as full JSON observation objects (real production format)."""

    def test_json_observation_object(self, engine):
        # Actual format from the DB: full JSON object with 'value' field
        obs_json = json.dumps({
            'stream_name': 'mana-usdt-binance',
            'timestamp': 1773496828,
            'value': '0.09070000',
            'seq_num': 928,
        })
        rows = _db_rows([obs_json])
        result = engine.predict(rows)
        assert result is not None
        assert float(result) == pytest.approx(0.0907)

    def test_multiple_json_observation_objects(self, engine):
        values = []
        for i, price in enumerate(['0.0900', '0.0910', '0.0920', '0.0930', '0.0940']):
            values.append(json.dumps({
                'stream_name': 'mana-usdt-binance',
                'timestamp': 1773496000 + i * 600,
                'value': price,
                'seq_num': i + 1,
            }))
        # newest first: 0.0940 is newest
        rows = _db_rows(values[::-1])
        result = engine.predict(rows)
        assert result is not None
        # Linear trend: 0.09, 0.091, ..., 0.094 → predicts ~0.095
        assert float(result) == pytest.approx(0.095, abs=0.001)

    def test_mixed_json_objects_and_plain_values(self, engine):
        obs_json = json.dumps({
            'stream_name': 'test',
            'timestamp': 1773496828,
            'value': '50.0',
            'seq_num': 1,
        })
        rows = _db_rows([obs_json, '60.0'])
        result = engine.predict(rows)
        assert result is not None

    def test_json_object_without_value_key(self, engine):
        obs_json = json.dumps({'stream_name': 'test', 'timestamp': 123})
        rows = _db_rows([obs_json])
        result = engine.predict(rows)
        # No 'value' key and dict can't be float → None
        assert result is None


# ===================================================================
# Test non-numeric / malformed data (real-world Nostr junk)
# ===================================================================

class TestMalformedData:
    """Nostr streams can contain arbitrary data — engine must not crash."""

    def test_empty_string_value(self, engine):
        rows = _db_rows([''])
        result = engine.predict(rows)
        assert result is None

    def test_none_value(self, engine):
        rows = [_db_row(value=None)]
        result = engine.predict(rows)
        assert result is None

    def test_text_value(self, engine):
        rows = _db_rows(['hello world'])
        result = engine.predict(rows)
        assert result is None

    def test_json_object_value(self, engine):
        rows = _db_rows([json.dumps({'temp': 42.5, 'unit': 'C'})])
        result = engine.predict(rows)
        # Can't extract a single number from a JSON object
        assert result is None

    def test_json_array_value(self, engine):
        rows = _db_rows([json.dumps([1, 2, 3])])
        result = engine.predict(rows)
        assert result is None

    def test_mixed_valid_and_garbage(self, engine):
        # Some observations are numeric, some are garbage
        rows = _db_rows(['10.0', 'bad', '20.0', 'corrupt', '30.0'])
        result = engine.predict(rows)
        # 3 valid values: chronological [30, 20, 10] → mean of last 2 = 15
        assert result is not None
        assert float(result) == pytest.approx(15.0)

    def test_all_garbage(self, engine):
        rows = _db_rows(['bad', 'data', 'here'])
        result = engine.predict(rows)
        assert result is None

    def test_unicode_value(self, engine):
        rows = _db_rows(['🚀42.5'])
        result = engine.predict(rows)
        # Should fail gracefully — emoji prefix makes it non-numeric
        assert result is None

    def test_extremely_long_string(self, engine):
        rows = _db_rows(['x' * 10000])
        result = engine.predict(rows)
        assert result is None

    def test_boolean_string(self, engine):
        # json.loads('true') → True → float(True) = 1.0, so booleans are numeric
        rows = _db_rows(['true', 'false'])
        result = engine.predict(rows)
        assert result is not None
        # chronological: [false=0.0, true=1.0] → mean of last 2 = 0.5
        assert float(result) == pytest.approx(0.5)


# ===================================================================
# Test the _networkRunEngine flow end-to-end (without DB/network)
# ===================================================================

class TestNetworkRunEngineFlow:
    """Simulate the exact flow in start.py _networkRunEngine."""

    def _simulate_engine_call(self, db_observations, observation_value):
        """
        Replicates start.py lines 354-371:
            prediction = LiteEngine().predict(observations)
            if prediction is not None:
                value_str = prediction
                method = 'lite'
            else:
                value = observation.value
                value_str = json.dumps(value) if not isinstance(value, str) else value
                method = 'echo'
        """
        prediction = LiteEngine().predict(db_observations)
        if prediction is not None:
            return prediction, 'lite'
        else:
            value = observation_value
            value_str = json.dumps(value) if not isinstance(value, str) else value
            return value_str, 'echo'

    def test_numeric_stream_uses_lite(self):
        # _db_rows: index 0 = newest. So ['10.0','20.0','30.0'] →
        # newest=10, oldest=30. Chronological: [30, 20, 10] → mean of last 2 = 15
        rows = _db_rows(['10.0', '20.0', '30.0'])
        value_str, method = self._simulate_engine_call(rows, '10.0')
        assert method == 'lite'
        assert float(value_str) == pytest.approx(15.0)

    def test_non_numeric_falls_back_to_echo(self):
        rows = _db_rows(['hello', 'world'])
        value_str, method = self._simulate_engine_call(rows, 'world')
        assert method == 'echo'
        assert value_str == 'world'

    def test_single_numeric_uses_lite(self):
        rows = _db_rows(['42.5'])
        value_str, method = self._simulate_engine_call(rows, '42.5')
        assert method == 'lite'
        assert value_str == '42.5'

    def test_empty_observations_falls_back_to_echo(self):
        value_str, method = self._simulate_engine_call([], 'latest_value')
        assert method == 'echo'
        assert value_str == 'latest_value'

    def test_mixed_data_some_valid(self):
        rows = _db_rows(['garbage', '50.0'])
        value_str, method = self._simulate_engine_call(rows, '50.0')
        assert method == 'lite'
        assert float(value_str) == pytest.approx(50.0)

    def test_prediction_is_always_string(self):
        rows = _db_rows(['10.0', '20.0', '30.0', '40.0', '50.0'])
        value_str, method = self._simulate_engine_call(rows, '50.0')
        assert method == 'lite'
        assert isinstance(value_str, str)

    def test_echo_with_non_string_value(self):
        """If observation.value is not a string, _networkRunEngine json.dumps it."""
        rows = _db_rows(['not_a_number'])
        value_str, method = self._simulate_engine_call(rows, 42.5)
        assert method == 'echo'
        assert value_str == '42.5'  # json.dumps(42.5)

    def test_echo_with_dict_value(self):
        rows = _db_rows(['not_a_number'])
        value_str, method = self._simulate_engine_call(rows, {'temp': 20})
        assert method == 'echo'
        assert json.loads(value_str) == {'temp': 20}


# ===================================================================
# Test with realistic Nostr stream scenarios
# ===================================================================

class TestRealisticStreams:
    """Simulate real-world Nostr data stream patterns."""

    def test_temperature_stream(self, engine):
        # Weather station publishing temperature readings
        temps = ['22.5', '22.7', '22.3', '22.8', '23.1',
                 '23.0', '22.9', '23.2', '23.5', '23.3']
        rows = _db_rows(temps)
        result = engine.predict(rows)
        assert result is not None
        val = float(result)
        # Should be in a reasonable temperature range
        assert 20.0 < val < 26.0

    def test_price_stream(self, engine):
        # Crypto price feed
        prices = ['45230.50', '45280.75', '45190.00', '45350.25', '45400.00',
                  '45380.50', '45420.75', '45500.00', '45480.25', '45550.00']
        rows = _db_rows(prices)
        result = engine.predict(rows)
        assert result is not None
        val = float(result)
        assert 44000.0 < val < 47000.0

    def test_counter_stream(self, engine):
        # Monotonically increasing counter — newest first for _db_rows
        # newest=10, oldest=1 → chronological [1..10] → predicts ~11
        counters = [str(i) for i in range(10, 0, -1)]
        rows = _db_rows(counters)
        result = engine.predict(rows)
        assert result is not None
        assert float(result) == pytest.approx(11.0, abs=1.0)

    def test_sparse_observations(self, engine):
        # Only 2 observations received so far (new stream)
        rows = _db_rows(['100.0', '110.0'])
        result = engine.predict(rows)
        assert result is not None
        # Mean of last 2 chronological = (100+110)/2 = 105
        assert float(result) == pytest.approx(105.0)

    def test_first_observation_ever(self, engine):
        rows = _db_rows(['42.0'])
        result = engine.predict(rows)
        assert result == '42.0'

    def test_high_frequency_30_observations(self, engine):
        # start.py fetches limit=30 observations (newest first)
        # newest=14.5, oldest=0.0 → chronological [0, 0.5, ..., 14.5] → predicts ~15.0
        values = [str(float(i) * 0.5) for i in range(29, -1, -1)]
        rows = _db_rows(values)
        result = engine.predict(rows)
        assert result is not None
        assert float(result) == pytest.approx(15.0, abs=1.0)

    def test_stream_with_occasional_nulls(self, engine):
        # Some observations might have None values (failed parse upstream)
        rows = _db_rows(['10.0', None, '20.0', None, '30.0', '40.0', '50.0'])
        result = engine.predict(rows)
        # 5 valid values, uses linear regression
        assert result is not None

    def test_stream_value_as_integer_strings(self, engine):
        # Some streams publish integers without decimals (newest first)
        rows = _db_rows(['5', '4', '3', '2', '1'])
        result = engine.predict(rows)
        assert float(result) == pytest.approx(6.0, abs=0.5)
