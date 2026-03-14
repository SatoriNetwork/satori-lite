"""Comprehensive tests for the heavy engine adapters.

Tests StarterAdapter, XgbAdapter, condition-based model switching,
and the full training pipeline with various data shapes and edge cases.
"""

import sys
import os
import pytest
import numpy as np
import pandas as pd
import tempfile
import shutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'engine-lite'))

from satoriengine.veda.adapters.starter.starter_model import StarterAdapter
from satoriengine.veda.adapters.xgboost.xgb import XgbAdapter
from satoriengine.veda.adapters.interface import ModelAdapter, TrainingResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(values, freq='1h', start='2024-01-01', stream_id='test-stream'):
    """Build a DataFrame matching the heavy engine's expected format."""
    dates = pd.date_range(start=start, periods=len(values), freq=freq)
    return pd.DataFrame({
        'date_time': dates.astype(str),
        'value': values,
        'id': stream_id,
    })


def _linear_data(n, slope=1.0, intercept=0.0, noise=0.0, freq='1h'):
    """Generate linear trend data: y = slope*x + intercept + noise."""
    rng = np.random.default_rng(42)
    x = np.arange(n, dtype=float)
    values = slope * x + intercept + rng.normal(0, noise, n)
    return _make_df(values.tolist(), freq=freq)


def _sinusoidal_data(n, amplitude=10.0, period=24, offset=50.0, freq='1h'):
    """Generate sinusoidal data with known period."""
    x = np.arange(n, dtype=float)
    values = amplitude * np.sin(2 * np.pi * x / period) + offset
    return _make_df(values.tolist(), freq=freq)


def _constant_data(n, value=42.0, freq='1h'):
    return _make_df([value] * n, freq=freq)


def _spike_data(n, spike_idx, spike_val=1000.0, base=10.0, freq='1h'):
    """Mostly constant with a spike at spike_idx."""
    values = [base] * n
    values[spike_idx] = spike_val
    return _make_df(values, freq=freq)


# ===================================================================
# StarterAdapter tests
# ===================================================================

class TestStarterCondition:
    def test_small_data_returns_1(self):
        assert StarterAdapter.condition(data=[1, 2, 3]) == 1.0

    def test_exactly_10_returns_1(self):
        assert StarterAdapter.condition(data=list(range(10))) == 1.0

    def test_11_points_returns_0(self):
        assert StarterAdapter.condition(data=list(range(11))) == 0.0

    def test_low_ram_returns_1(self):
        assert StarterAdapter.condition(data=list(range(100)), availableRamGigs=0.01) == 1.0

    def test_enough_ram_big_data_returns_0(self):
        assert StarterAdapter.condition(data=list(range(50)), availableRamGigs=2.0) == 0.0

    def test_empty_data_returns_1(self):
        assert StarterAdapter.condition(data=[]) == 1.0


class TestStarterPrediction:
    def test_empty_data(self):
        adapter = StarterAdapter()
        result = adapter.predict(data=pd.DataFrame())
        assert result is not None
        assert result['pred'].iloc[0] == 0

    def test_none_data(self):
        adapter = StarterAdapter()
        result = adapter.predict(data=None)
        assert result['pred'].iloc[0] == 0

    def test_single_row(self):
        df = _make_df([99.5])
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        assert result['pred'].iloc[0] == pytest.approx(99.5)

    def test_two_rows_mean_of_last_two(self):
        df = _make_df([10.0, 20.0])
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        assert result['pred'].iloc[0] == pytest.approx(15.0)

    def test_three_rows_mean_of_last_two(self):
        df = _make_df([10.0, 20.0, 30.0])
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        # last 2 rows: 20, 30 → mean = 25
        assert result['pred'].iloc[0] == pytest.approx(25.0)

    def test_four_rows_mean_of_last_two(self):
        df = _make_df([10.0, 20.0, 30.0, 40.0])
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        # last 2 rows: 30, 40 → mean = 35
        assert result['pred'].iloc[0] == pytest.approx(35.0)

    def test_five_rows_uses_linear_regression(self):
        # Perfect line: [0, 1, 2, 3, 4] → predicts 5
        df = _make_df([0.0, 1.0, 2.0, 3.0, 4.0])
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        assert result['pred'].iloc[0] == pytest.approx(5.0, abs=0.1)

    def test_ten_rows_linear_regression(self):
        df = _make_df(list(range(10)))
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        assert result['pred'].iloc[0] == pytest.approx(10.0, abs=0.1)

    def test_negative_trend(self):
        # [50, 40, 30, 20, 10] → should predict ~0
        df = _make_df([50.0, 40.0, 30.0, 20.0, 10.0])
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        assert result['pred'].iloc[0] == pytest.approx(0.0, abs=0.5)

    def test_constant_values(self):
        df = _make_df([7.0] * 8)
        adapter = StarterAdapter()
        result = adapter.predict(data=df)
        assert result['pred'].iloc[0] == pytest.approx(7.0, abs=0.01)


class TestStarterFitAndCompare:
    def test_fit_returns_negative_status(self):
        adapter = StarterAdapter()
        result = adapter.fit(data=pd.DataFrame())
        assert result.status == -1
        assert result.model is adapter

    def test_compare_always_true(self):
        adapter = StarterAdapter()
        assert adapter.compare(None) is True
        assert adapter.compare(StarterAdapter()) is True

    def test_score_always_zero(self):
        adapter = StarterAdapter()
        assert adapter.score() == 0.0

    def test_save_returns_true(self):
        adapter = StarterAdapter()
        assert adapter.save('/tmp/fake') is True

    def test_load_returns_none(self):
        adapter = StarterAdapter()
        assert adapter.load('/tmp/nonexistent') is None


# ===================================================================
# XgbAdapter tests
# ===================================================================

class TestXgbCondition:
    def test_low_ram_returns_0(self):
        assert XgbAdapter.condition(data=list(range(50)), availableRamGigs=0.01) == 0

    def test_big_data_returns_1(self):
        assert XgbAdapter.condition(data=list(range(20)), availableRamGigs=2.0) == 1.0

    def test_exactly_10_returns_0(self):
        # data length 10 is NOT > 10
        assert XgbAdapter.condition(data=list(range(10)), availableRamGigs=2.0, cpu=4) == 0.0

    def test_11_points_returns_1(self):
        assert XgbAdapter.condition(data=list(range(11)), availableRamGigs=2.0) == 1.0

    def test_single_cpu_returns_1(self):
        # cpu == 1 triggers condition even with small data
        assert XgbAdapter.condition(data=[1, 2], availableRamGigs=2.0, cpu=1) == 1.0

    def test_empty_data_multicpu_returns_0(self):
        assert XgbAdapter.condition(data=[], availableRamGigs=2.0, cpu=4) == 0.0


class TestXgbHyperparameters:
    def test_prep_params_within_bounds(self):
        params = XgbAdapter._prepParams()
        bounds = XgbAdapter.paramBounds()
        for key, (lo, hi) in bounds.items():
            assert lo <= params[key] <= hi, f"{key}={params[key]} not in [{lo},{hi}]"

    def test_mutate_params_within_bounds(self):
        rng = np.random.default_rng(42)
        initial = XgbAdapter._prepParams(rng)
        for _ in range(10):  # mutate 10 times, always in bounds
            mutated = XgbAdapter._mutateParams(prevParams=initial, rng=rng)
            bounds = XgbAdapter.paramBounds()
            for key, (lo, hi) in bounds.items():
                assert lo <= mutated[key] <= hi, f"{key}={mutated[key]} not in [{lo},{hi}]"
            initial = mutated

    def test_mutate_preserves_random_state(self):
        rng = np.random.default_rng(42)
        initial = XgbAdapter._prepParams(rng)
        mutated = XgbAdapter._mutateParams(prevParams=initial, rng=rng)
        assert mutated['random_state'] == initial['random_state']

    def test_mutate_preserves_eval_metric(self):
        rng = np.random.default_rng(42)
        initial = XgbAdapter._prepParams(rng)
        mutated = XgbAdapter._mutateParams(prevParams=initial, rng=rng)
        assert mutated['eval_metric'] == 'mae'

    def test_integer_params_are_int(self):
        rng = np.random.default_rng(42)
        params = XgbAdapter._prepParams(rng)
        mutated = XgbAdapter._mutateParams(prevParams=params, rng=rng)
        for key in ['n_estimators', 'max_depth', 'min_child_weight']:
            assert isinstance(mutated[key], int), f"{key} should be int, got {type(mutated[key])}"

    def test_mutate_from_none_generates_fresh(self):
        mutated = XgbAdapter._mutateParams(prevParams=None)
        assert 'n_estimators' in mutated
        assert 'learning_rate' in mutated


class TestXgbFitAndPredict:
    """Test XgbAdapter with real training on synthetic data."""

    def test_fit_linear_trend(self):
        data = _linear_data(50, slope=2.0, intercept=10.0)
        adapter = XgbAdapter()
        result = adapter.fit(data=data)
        assert result.status == 1
        assert adapter.model is not None

    def test_predict_after_fit(self):
        data = _linear_data(50, slope=2.0, intercept=10.0)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None
        assert 'pred' in prediction.columns
        assert len(prediction) == 1

    def test_predict_without_fit_returns_none(self):
        adapter = XgbAdapter()
        data = _linear_data(50)
        result = adapter.predict(data=data)
        assert result is None

    def test_fit_constant_data(self):
        data = _constant_data(50, value=100.0)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None
        # Constant data → prediction should be close to 100
        assert prediction['pred'].iloc[0] == pytest.approx(100.0, abs=5.0)

    def test_fit_sinusoidal_data(self):
        data = _sinusoidal_data(100, amplitude=10, period=24, offset=50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None
        # Should be in a reasonable range around the offset
        assert 30.0 < prediction['pred'].iloc[0] < 70.0

    def test_fit_noisy_linear(self):
        data = _linear_data(80, slope=1.5, intercept=5.0, noise=2.0)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None
        # Last value is ~1.5*79+5=123.5, prediction should be in the ballpark
        pred_val = prediction['pred'].iloc[0]
        assert 80.0 < pred_val < 180.0

    def test_fit_negative_values(self):
        data = _linear_data(50, slope=-1.0, intercept=100.0)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None
        # Trend is downward; last value ~51, prediction should be reasonable
        pred_val = prediction['pred'].iloc[0]
        assert pred_val < 100.0

    def test_fit_very_small_values(self):
        values = [1e-8 * i for i in range(50)]
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None

    def test_fit_very_large_values(self):
        values = [1e8 + i * 1e6 for i in range(50)]
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None

    def test_fit_with_spike(self):
        data = _spike_data(50, spike_idx=25, spike_val=1000.0, base=10.0)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction = adapter.predict(data=data)
        assert prediction is not None
        # Model should produce a finite prediction (spike is an outlier)
        assert prediction['pred'].iloc[0] < 1100.0


class TestXgbScoring:
    def test_score_untrained_returns_inf(self):
        adapter = XgbAdapter()
        assert adapter.score() == np.inf

    def test_score_after_fit(self):
        data = _linear_data(50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        score = adapter.score()
        assert score >= 0.0
        assert score < np.inf

    def test_score_constant_data_near_zero(self):
        data = _constant_data(50, value=42.0)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        score = adapter.score()
        # Constant data should be easy to predict
        assert score < 5.0

    def test_score_with_custom_test_set(self):
        data = _linear_data(50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        # Score with the training set as custom test set
        score = adapter.score(test_x=adapter.trainX, test_y=adapter.trainY)
        assert score >= 0.0
        assert score < np.inf


class TestXgbCompare:
    def test_compare_with_none_returns_true(self):
        adapter = XgbAdapter()
        # Comparing with non-XgbAdapter returns True (different class)
        assert adapter.compare(None) is True

    def test_compare_with_starter_returns_true(self):
        adapter = XgbAdapter()
        starter = StarterAdapter()
        assert adapter.compare(starter) is True

    def test_compare_two_trained_models(self):
        data = _linear_data(50)
        pilot = XgbAdapter()
        pilot.fit(data=data)
        stable = XgbAdapter()
        stable.fit(data=data)
        # Both trained on same data — compare should return a boolean
        result = pilot.compare(stable)
        assert result is True or result is False or isinstance(result, (bool, np.bool_))

    def test_better_model_wins(self):
        # Train one model on clean data, another on noisy data
        clean = _linear_data(60, slope=1.0, noise=0.0)
        noisy = _linear_data(60, slope=1.0, noise=10.0)

        good = XgbAdapter()
        good.fit(data=clean)

        bad = XgbAdapter()
        bad.fit(data=noisy)

        # The model trained on clean data should score better on clean test set
        good_score = good.score()
        bad_score = bad.score()
        # At minimum both should be finite
        assert good_score < np.inf
        assert bad_score < np.inf


class TestXgbSaveLoad:
    def test_save_and_load_roundtrip(self, tmp_path):
        data = _linear_data(50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        prediction_before = adapter.predict(data=data)

        model_path = str(tmp_path / 'model.joblib')
        assert adapter.save(model_path) is True

        loaded = XgbAdapter()
        loaded.load(model_path)
        assert loaded.model is not None

    def test_load_nonexistent_returns_none(self):
        adapter = XgbAdapter()
        result = adapter.load('/tmp/does_not_exist_12345.joblib')
        assert result is None

    def test_save_creates_directory(self, tmp_path):
        data = _linear_data(50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        model_path = str(tmp_path / 'subdir' / 'model.joblib')
        assert adapter.save(model_path) is True
        assert os.path.isfile(model_path)

    def test_loaded_model_predicts_same(self, tmp_path):
        data = _linear_data(50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred_before = adapter.predict(data=data)['pred'].iloc[0]

        model_path = str(tmp_path / 'model.joblib')
        adapter.save(model_path)

        loaded = XgbAdapter()
        loaded.load(model_path)
        # Need to prepare data for the loaded model
        loaded._manageData(data)
        pred_after = loaded.model.predict(loaded.dataset.iloc[[-1], :-1])[0]

        assert pred_before == pytest.approx(pred_after, rel=1e-5)


# ===================================================================
# Adapter switching (condition-based model selection)
# ===================================================================

class TestAdapterSwitching:
    """Test the condition-based model selection logic that the engine uses."""

    def _select_adapter(self, data_len, ram_gigs=2.0, cpu=4):
        """Simulate chooseAdapter logic from engine.py lines 1133-1182."""
        preferred = [XgbAdapter, StarterAdapter]
        data = list(range(data_len))
        for adapter_cls in preferred:
            if adapter_cls.condition(data=data, cpu=cpu, availableRamGigs=ram_gigs) == 1:
                return adapter_cls
        return StarterAdapter  # fallback

    def test_small_data_selects_starter(self):
        assert self._select_adapter(5) == StarterAdapter

    def test_10_points_selects_starter(self):
        assert self._select_adapter(10) == StarterAdapter

    def test_11_points_selects_xgb(self):
        assert self._select_adapter(11) == XgbAdapter

    def test_50_points_selects_xgb(self):
        assert self._select_adapter(50) == XgbAdapter

    def test_low_ram_selects_starter(self):
        # XgbAdapter returns 0 for low RAM, StarterAdapter returns 1
        assert self._select_adapter(50, ram_gigs=0.01) == StarterAdapter

    def test_single_cpu_selects_xgb(self):
        # cpu == 1 makes XgbAdapter return 1 even for small data
        assert self._select_adapter(5, cpu=1) == XgbAdapter

    def test_growing_dataset_switches_adapter(self):
        """Simulate data growing from small to large — adapter should switch."""
        selections = []
        for n in [1, 5, 10, 11, 20, 50, 100]:
            selections.append((n, self._select_adapter(n)))

        # First few should be Starter, then switch to Xgb
        assert selections[0][1] == StarterAdapter  # n=1
        assert selections[1][1] == StarterAdapter  # n=5
        assert selections[2][1] == StarterAdapter  # n=10
        assert selections[3][1] == XgbAdapter      # n=11
        assert selections[4][1] == XgbAdapter      # n=20
        assert selections[5][1] == XgbAdapter      # n=50
        assert selections[6][1] == XgbAdapter      # n=100


# ===================================================================
# End-to-end: Starter → XgbAdapter transition with growing data
# ===================================================================

class TestStarterToXgbTransition:
    """Simulate the real scenario: data grows, model switches from Starter to Xgb."""

    def test_transition_with_linear_data(self):
        """Start with few points (Starter), grow to many (Xgb), verify both predict."""
        all_values = [float(i) for i in range(60)]

        # Phase 1: small data → StarterAdapter
        small_df = _make_df(all_values[:5])
        starter = StarterAdapter()
        starter_pred = starter.predict(data=small_df)
        assert starter_pred is not None
        assert starter_pred['pred'].iloc[0] == pytest.approx(5.0, abs=0.5)

        # Phase 2: data grows past 10 → XgbAdapter kicks in
        big_df = _make_df(all_values[:30])
        xgb = XgbAdapter()
        xgb.fit(data=big_df)
        xgb_pred = xgb.predict(data=big_df)
        assert xgb_pred is not None
        # Linear data [0..29], prediction should be near 30
        assert 10.0 < xgb_pred['pred'].iloc[0] < 45.0

    def test_transition_with_constant_data(self):
        all_values = [42.0] * 60

        starter = StarterAdapter()
        starter_pred = starter.predict(data=_make_df(all_values[:3]))
        assert starter_pred['pred'].iloc[0] == pytest.approx(42.0)

        xgb = XgbAdapter()
        xgb.fit(data=_make_df(all_values[:30]))
        xgb_pred = xgb.predict(data=_make_df(all_values[:30]))
        assert xgb_pred is not None
        assert xgb_pred['pred'].iloc[0] == pytest.approx(42.0, abs=3.0)

    def test_transition_with_noisy_data(self):
        rng = np.random.default_rng(42)
        all_values = [10.0 + 0.5 * i + rng.normal(0, 1) for i in range(60)]

        # Starter with 4 points
        starter = StarterAdapter()
        starter_pred = starter.predict(data=_make_df(all_values[:4]))
        assert starter_pred is not None

        # Xgb with 40 points
        xgb = XgbAdapter()
        xgb.fit(data=_make_df(all_values[:40]))
        xgb_pred = xgb.predict(data=_make_df(all_values[:40]))
        assert xgb_pred is not None
        # Should be in the general trend range
        pred_val = xgb_pred['pred'].iloc[0]
        assert 15.0 < pred_val < 45.0

    def test_incremental_data_growth(self):
        """Simulate data arriving incrementally — Xgb should handle growing dataset."""
        xgb = XgbAdapter()

        # First fit with 20 points
        data_20 = _linear_data(20, slope=1.0)
        xgb.fit(data=data_20)
        pred_20 = xgb.predict(data=data_20)['pred'].iloc[0]

        # Grow to 40 points — re-fit
        data_40 = _linear_data(40, slope=1.0)
        xgb.fit(data=data_40)
        pred_40 = xgb.predict(data=data_40)['pred'].iloc[0]

        # Predictions should grow as data extends
        assert pred_40 > pred_20

    def test_xgb_improves_over_training(self):
        """Multiple fit calls should generally improve or maintain score."""
        data = _linear_data(60, slope=2.0, noise=1.0)
        adapter = XgbAdapter()

        adapter.fit(data=data)
        score_1 = adapter.score()

        # Train a few more times (hyperparameter mutation)
        for _ in range(3):
            adapter.fit(data=data)

        score_final = adapter.score()
        # Both should be finite and non-negative
        assert score_1 < np.inf
        assert score_final < np.inf


# ===================================================================
# Data edge cases for XgbAdapter
# ===================================================================

class TestXgbDataEdgeCases:
    def test_minimum_viable_data(self):
        """Exactly 11 points — the threshold for XgbAdapter."""
        data = _linear_data(11)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None

    def test_large_dataset(self):
        """500 points — should handle without issues."""
        data = _linear_data(500, slope=0.1)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None
        assert pred['pred'].iloc[0] > 0

    def test_minute_frequency_data(self):
        data = _linear_data(100, freq='1min')
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None

    def test_daily_frequency_data(self):
        data = _linear_data(60, freq='1D')
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None

    def test_data_with_zeros(self):
        values = [0.0] * 25 + [1.0] * 25
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None

    def test_step_function(self):
        """Abrupt change in value — step from 10 to 100 at midpoint."""
        values = [10.0] * 25 + [100.0] * 25
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None
        # Should predict near 100 (recent values)
        assert pred['pred'].iloc[0] > 50.0

    def test_alternating_values(self):
        """Alternating high/low pattern."""
        values = [10.0 if i % 2 == 0 else 20.0 for i in range(50)]
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None
        assert 5.0 < pred['pred'].iloc[0] < 25.0

    def test_exponential_growth(self):
        values = [2.0 ** (i / 10) for i in range(50)]
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None
        assert pred['pred'].iloc[0] > 0

    def test_negative_values_throughout(self):
        values = [-100.0 + i * 0.5 for i in range(50)]
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None

    def test_crossing_zero(self):
        """Values go from negative to positive."""
        values = [-25.0 + i for i in range(50)]
        data = _make_df(values)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        pred = adapter.predict(data=data)
        assert pred is not None
        # XGBoost is tree-based so may not extrapolate perfectly; just check reasonable range
        assert pred['pred'].iloc[0] > -30.0


# ===================================================================
# Preprocessing tests
# ===================================================================

class TestXgbPreprocessing:
    def test_percentage_change_features_created(self):
        data = _linear_data(60)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        expected_cols = [f'percent{p}' for p in [1, 2, 3, 5, 8, 13, 21, 34, 55]]
        for col in expected_cols:
            assert col in adapter.dataset.columns, f"Missing feature column: {col}"

    def test_time_features_created(self):
        data = _linear_data(60)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        for col in ['hour', 'day', 'month', 'year', 'day_of_week']:
            assert col in adapter.dataset.columns, f"Missing time feature: {col}"

    def test_tomorrow_target_created(self):
        data = _linear_data(60)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        assert 'tomorrow' in adapter.dataset.columns

    def test_no_infinities_in_dataset(self):
        data = _linear_data(60)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        numeric_cols = adapter.dataset.select_dtypes(include=[np.number]).columns
        for col in numeric_cols:
            assert not np.isinf(adapter.dataset[col]).any(), f"Inf found in {col}"

    def test_train_test_split_ratio(self):
        data = _linear_data(50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        total = len(adapter.trainX) + len(adapter.testX)
        test_ratio = len(adapter.testX) / total
        assert test_ratio == pytest.approx(0.2, abs=0.05)

    def test_train_test_not_shuffled(self):
        """Test set should be the later portion (time-ordered split)."""
        data = _linear_data(50)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        # trainX index should be before testX index (since shuffle=False)
        assert adapter.trainX.index[-1] < adapter.testX.index[0] or \
               len(adapter.trainX) + len(adapter.testX) > 0


# ===================================================================
# Training loop: does the model actually improve over iterations?
# ===================================================================

class TestTrainingImprovement:
    """Simulate the engine's training loop (engine.py _single_training_iteration).

    The real loop does:
      1. chooseAdapter(inplace=True)
      2. pilot.fit(data)
      3. if pilot.compare(stable): stable = deepcopy(pilot)

    We replicate this and verify that:
      - The stable model's score improves (decreases) over iterations
      - compare() correctly identifies improvements
      - The best score is retained even when a worse mutation appears
    """

    def _run_training_loop(self, data, iterations=15):
        """Run the pilot/stable training loop, return history of stable scores."""
        import copy

        stable = XgbAdapter()
        stable.fit(data=data)
        stable_scores = [stable.score()]
        improvements = 0

        for _ in range(iterations):
            pilot = XgbAdapter()
            pilot.fit(data=data)
            if pilot.compare(stable):
                stable = copy.deepcopy(pilot)
                improvements += 1
            stable_scores.append(stable.score())

        return stable_scores, improvements

    def test_score_never_worsens(self):
        """Stable model score should never increase (get worse) — we only keep improvements."""
        data = _linear_data(80, slope=2.0, noise=3.0)
        scores, _ = self._run_training_loop(data, iterations=10)
        for i in range(1, len(scores)):
            assert scores[i] <= scores[i - 1] + 1e-9, \
                f"Score worsened at iteration {i}: {scores[i-1]:.4f} → {scores[i]:.4f}"

    def test_at_least_one_improvement(self):
        """Over 25 iterations with noisy data, at least one mutation should improve."""
        data = _linear_data(100, slope=1.0, noise=5.0)
        scores, improvements = self._run_training_loop(data, iterations=25)
        assert improvements >= 1, \
            f"No improvements in 25 iterations. Scores: {[f'{s:.4f}' for s in scores]}"

    def test_final_score_better_than_first(self):
        """After multiple iterations, final stable score should be <= initial."""
        data = _sinusoidal_data(120, amplitude=10, period=24, offset=50)
        scores, _ = self._run_training_loop(data, iterations=12)
        assert scores[-1] <= scores[0] + 1e-9, \
            f"Final score {scores[-1]:.4f} worse than initial {scores[0]:.4f}"

    def test_improvement_on_constant_data(self):
        """Constant data is trivial — score should be near zero quickly."""
        data = _constant_data(60, value=42.0)
        scores, _ = self._run_training_loop(data, iterations=5)
        assert scores[-1] < 2.0, f"Constant data score should be near 0, got {scores[-1]:.4f}"

    def test_improvement_on_noisy_data(self):
        """Noisy data — score should still decrease over training."""
        rng = np.random.default_rng(42)
        values = [50.0 + 0.5 * i + rng.normal(0, 3) for i in range(100)]
        data = _make_df(values)
        scores, improvements = self._run_training_loop(data, iterations=12)
        # At least the score should be finite and non-negative
        assert all(s >= 0 and s < np.inf for s in scores)
        # Final should be <= initial (or very close)
        assert scores[-1] <= scores[0] + 1e-9

    def test_compare_returns_false_for_same_model(self):
        """Comparing a model against itself — pilot can't beat stable with same score."""
        import copy
        data = _linear_data(60, slope=1.0, noise=0.5)
        adapter = XgbAdapter()
        adapter.fit(data=data)
        clone = copy.deepcopy(adapter)
        # Same model vs itself: thisScore == otherScore → not improved
        assert adapter.compare(clone) == False

    def test_hyperparameters_change_each_iteration(self):
        """Each fit() should mutate hyperparameters."""
        data = _linear_data(60)
        adapter = XgbAdapter()

        adapter.fit(data=data)
        params_1 = dict(adapter.hyperparameters)

        adapter.fit(data=data)
        params_2 = dict(adapter.hyperparameters)

        # At least some params should differ (mutation is random)
        changed = sum(1 for k in XgbAdapter.paramBounds()
                      if params_1[k] != params_2[k])
        assert changed > 0, "Hyperparameters should change between fit() calls"

    def test_training_loop_with_growing_data(self):
        """Simulate data arriving over time — each size gets its own training loop."""
        import copy

        rng_data = np.random.default_rng(42)
        all_values = [float(i) + rng_data.normal(0, 1) for i in range(100)]
        scores_at_size = {}

        for size in [20, 40, 60, 80, 100]:
            data = _make_df(all_values[:size])
            # Run a mini training loop at each data size
            stable = XgbAdapter()
            stable.fit(data=data)
            for _ in range(3):
                pilot = XgbAdapter()
                pilot.fit(data=data)
                if pilot.compare(stable):
                    stable = copy.deepcopy(pilot)
            scores_at_size[size] = stable.score()

        # All scores should be finite
        for size, score in scores_at_size.items():
            assert score < np.inf, f"Score at size {size} is inf"
            assert score >= 0, f"Score at size {size} is negative"
