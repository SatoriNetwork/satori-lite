"""Tests for the lite prediction engine."""

import sys
import os
import pytest

# Ensure engine-lite source is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'engine-lite'))

from satoriengine.lite.lite_engine import LiteEngine


@pytest.fixture
def engine():
    return LiteEngine()


def _obs(value):
    """Helper: create an observation dict."""
    return {'value': str(value)}


class TestPredictEmpty:
    def test_empty_list(self, engine):
        assert engine.predict([]) is None

    def test_none_input(self, engine):
        assert engine.predict([]) is None


class TestPredictSingle:
    def test_single_observation(self, engine):
        # newest-first order
        result = engine.predict([_obs(42.0)])
        assert result == '42.0'

    def test_single_integer(self, engine):
        result = engine.predict([_obs(7)])
        assert result == '7.0'


class TestPredictMean:
    def test_two_observations(self, engine):
        # newest-first: [20, 10] → chronological [10, 20] → mean(10,20) = 15
        result = engine.predict([_obs(20), _obs(10)])
        assert float(result) == pytest.approx(15.0)

    def test_three_observations(self, engine):
        # newest-first: [30, 20, 10] → chronological [10, 20, 30]
        # mean of last 2 chronological = (20+30)/2 = 25
        result = engine.predict([_obs(30), _obs(20), _obs(10)])
        assert float(result) == pytest.approx(25.0)

    def test_four_observations(self, engine):
        # newest-first: [40, 30, 20, 10] → chronological [10, 20, 30, 40]
        # mean of last 2 = (30+40)/2 = 35
        result = engine.predict([_obs(40), _obs(30), _obs(20), _obs(10)])
        assert float(result) == pytest.approx(35.0)


class TestPredictLinearRegression:
    def test_upward_trend(self, engine):
        # newest-first → chronological: [1, 2, 3, 4, 5] → predicts ~6
        obs = [_obs(5), _obs(4), _obs(3), _obs(2), _obs(1)]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(6.0)

    def test_downward_trend(self, engine):
        # newest-first → chronological: [10, 8, 6, 4, 2] → predicts ~0
        obs = [_obs(2), _obs(4), _obs(6), _obs(8), _obs(10)]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(0.0)

    def test_constant_values(self, engine):
        # Constant series → predicts same constant
        obs = [_obs(5.0)] * 10
        result = engine.predict(obs)
        assert float(result) == pytest.approx(5.0)

    def test_ten_observations(self, engine):
        # chronological: [1..10] → predicts 11
        obs = [_obs(i) for i in range(10, 0, -1)]  # newest-first
        result = engine.predict(obs)
        assert float(result) == pytest.approx(11.0)

    def test_noisy_data(self, engine):
        # Noisy upward trend — prediction should be reasonable
        obs = [_obs(v) for v in reversed([2, 4, 3, 5, 4, 6, 5, 7])]
        result = engine.predict(obs)
        val = float(result)
        assert 5.0 < val < 9.0


class TestNonNumeric:
    def test_non_numeric_string(self, engine):
        result = engine.predict([{'value': 'hello'}])
        assert result is None

    def test_mixed_non_numeric(self, engine):
        # All non-numeric → None
        result = engine.predict([{'value': 'foo'}, {'value': 'bar'}])
        assert result is None

    def test_missing_value_key(self, engine):
        result = engine.predict([{'other': '123'}])
        assert result is None


class TestJsonEncoded:
    def test_json_number(self, engine):
        result = engine.predict([{'value': '42.5'}])
        assert float(result) == pytest.approx(42.5)

    def test_json_quoted_number(self, engine):
        # JSON-encoded string containing a number: '"42.5"'
        import json
        result = engine.predict([{'value': json.dumps('42.5')}])
        assert float(result) == pytest.approx(42.5)

    def test_json_integer_string(self, engine):
        result = engine.predict([{'value': '100'}])
        assert float(result) == pytest.approx(100.0)


class TestEdgeCases:
    def test_mixed_valid_and_invalid_observations(self, engine):
        # Some non-numeric values should be skipped, valid ones used
        obs = [{'value': 'bad'}, _obs(10), {'value': 'nope'}, _obs(20), _obs(30)]
        result = engine.predict(obs)
        # 3 valid values (chronological: [30, 20, 10]) → mean of last 2 = 15
        assert result is not None
        assert float(result) == pytest.approx(15.0)

    def test_very_large_values(self, engine):
        # Should handle large floats without overflow
        obs = [_obs(v) for v in reversed([1e10, 2e10, 3e10, 4e10, 5e10])]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(6e10)

    def test_negative_values(self, engine):
        # Negative trend: [-5, -4, -3, -2, -1] → predicts 0
        obs = [_obs(v) for v in reversed([-5, -4, -3, -2, -1])]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(0.0)

    def test_all_zeros(self, engine):
        obs = [_obs(0)] * 7
        result = engine.predict(obs)
        assert float(result) == pytest.approx(0.0)

    def test_exactly_five_observations(self, engine):
        # Boundary: exactly 5 triggers linear regression, not mean
        # chronological: [10, 20, 30, 40, 50] → predicts 60
        obs = [_obs(50), _obs(40), _obs(30), _obs(20), _obs(10)]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(60.0)

    def test_single_valid_among_invalid(self, engine):
        # Only one parseable value among garbage
        obs = [{'value': 'x'}, _obs(99), {'value': 'y'}]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(99.0)

    def test_very_small_floats(self, engine):
        obs = [_obs(v) for v in reversed([1e-10, 2e-10, 3e-10, 4e-10, 5e-10])]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(6e-10)

    def test_crossing_zero(self, engine):
        # Trend crossing zero: [-2, -1, 0, 1, 2] → predicts 3
        obs = [_obs(v) for v in reversed([-2, -1, 0, 1, 2])]
        result = engine.predict(obs)
        assert float(result) == pytest.approx(3.0)

    def test_large_dataset(self, engine):
        # 100 observations, linear: [0..99] → predicts 100
        obs = [_obs(i) for i in range(99, -1, -1)]  # newest-first
        result = engine.predict(obs)
        assert float(result) == pytest.approx(100.0)

    def test_duplicate_values_at_mean_tier(self, engine):
        # 3 identical values → mean of last 2 = same value
        obs = [_obs(7.5)] * 3
        result = engine.predict(obs)
        assert float(result) == pytest.approx(7.5)


class TestExtractNumeric:
    def test_float_string(self, engine):
        assert engine._extract_numeric('3.14') == pytest.approx(3.14)

    def test_int_string(self, engine):
        assert engine._extract_numeric('42') == pytest.approx(42.0)

    def test_non_numeric(self, engine):
        assert engine._extract_numeric('hello') is None

    def test_empty_string(self, engine):
        assert engine._extract_numeric('') is None

    def test_numeric_passthrough(self, engine):
        # Non-string numeric input
        assert engine._extract_numeric(3.14) == pytest.approx(3.14)


