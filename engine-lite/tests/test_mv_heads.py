"""Unit tests for the Jordan-1 multivariate head (``adapters/multivariate/heads.py``).

Covers: fit/predict shape and finiteness, determinism (fixed seed -- load
bearing for the random-swap peer search's mae_base/mae_new comparison),
state() round-trip (predictions identical after fromState), joblib
serializability of state(), deepcopy safety after fit, featureGains()
normalization (every training column present, zero-gain included), the
HEAD_REGISTRY lookup, and native NaN handling in lag columns.

``heads.py`` is loaded directly from its file path (same pattern as
``test_mv_features.py``) so these tests never touch ``adapters/__init__.py``
or ``adapters/multivariate/__init__.py`` (the latter still carries the
StarterAdapter copy-paste bug owned by a later task).

Runs under pytest (``python -m pytest``) or standalone
(``python test_mv_heads.py``) since the image ships no pytest.
"""

import copy
import importlib.util
import os
import tempfile

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_HEADS_PATH = os.path.join(
    _HERE, '..', 'adapters', 'multivariate', 'heads.py')
_spec = importlib.util.spec_from_file_location('mv_heads', _HEADS_PATH)
heads = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(heads)

Head = heads.Head
XgbHead = heads.XgbHead
HEAD_REGISTRY = heads.HEAD_REGISTRY
XGB_HEAD_PARAMS = heads.XGB_HEAD_PARAMS

import joblib


# --------------------------------------------------------------------------- #
# synthetic data helpers
# --------------------------------------------------------------------------- #

def _synthetic_xy(n=200, num_features=7, seed=0, with_nan=False):
    rng = np.random.default_rng(seed)
    cols = [f'lag_{i}' for i in range(num_features)]
    X = pd.DataFrame(rng.normal(size=(n, num_features)), columns=cols)
    # y depends on a couple of columns plus noise, so gain is non-trivially
    # distributed across features (some near-zero, some real).
    y = 3.0 * X['lag_0'] - 2.0 * X['lag_1'] + 0.1 * rng.normal(size=n)
    if with_nan:
        X = X.copy()
        # Scatter NaNs into a lag column, mirroring buildFrame's leading-NaN
        # target lag columns (XGBoost's native missing-value handling).
        X.loc[X.index[:5], 'lag_0'] = np.nan
        X.loc[X.index[50:55], 'lag_2'] = np.nan
    return X, y


# --------------------------------------------------------------------------- #
# fit / predict
# --------------------------------------------------------------------------- #

def test_fit_predict_returns_finite_predictions_of_right_shape():
    X, y = _synthetic_xy()
    head = XgbHead().fit(X, y)
    preds = head.predict(X)
    assert len(preds) == len(X)
    assert np.isfinite(np.asarray(preds, dtype=float)).all()


def test_fit_returns_self():
    X, y = _synthetic_xy()
    head = XgbHead()
    result = head.fit(X, y)
    assert result is head


# --------------------------------------------------------------------------- #
# determinism
# --------------------------------------------------------------------------- #

def test_determinism_same_data_same_seed_identical_predictions():
    X, y = _synthetic_xy()
    head1 = XgbHead().fit(X, y)
    head2 = XgbHead().fit(X, y)
    p1 = np.asarray(head1.predict(X), dtype=float)
    p2 = np.asarray(head2.predict(X), dtype=float)
    assert np.array_equal(p1, p2), 'two fits on identical data must be bit-identical'


def test_fixed_params_match_design_doc():
    # Load-bearing constants: swap search relies on these being fixed, not
    # searched. Guard against accidental drift.
    assert XGB_HEAD_PARAMS['max_depth'] == 3
    assert XGB_HEAD_PARAMS['n_estimators'] == 200
    assert XGB_HEAD_PARAMS['learning_rate'] == 0.05
    assert XGB_HEAD_PARAMS['min_child_weight'] == 5
    assert XGB_HEAD_PARAMS['subsample'] == 0.8
    assert XGB_HEAD_PARAMS['eval_metric'] == 'mae'
    assert 'random_state' in XGB_HEAD_PARAMS


# --------------------------------------------------------------------------- #
# state round-trip
# --------------------------------------------------------------------------- #

def test_state_round_trip_predicts_identically():
    X, y = _synthetic_xy()
    head = XgbHead().fit(X, y)
    restored = XgbHead.fromState(head.state())
    p1 = np.asarray(head.predict(X), dtype=float)
    p2 = np.asarray(restored.predict(X), dtype=float)
    assert np.array_equal(p1, p2)
    assert restored.featureGains() == head.featureGains()


def test_state_is_plain_dict_with_expected_keys():
    X, y = _synthetic_xy()
    head = XgbHead().fit(X, y)
    state = head.state()
    assert isinstance(state, dict)
    assert set(['model', 'feature_columns', 'params']).issubset(state.keys())
    assert state['feature_columns'] == list(X.columns)


# --------------------------------------------------------------------------- #
# serialization / deepcopy safety
# --------------------------------------------------------------------------- #

def test_state_is_joblib_serializable_round_trip():
    X, y = _synthetic_xy()
    head = XgbHead().fit(X, y)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'head_state.joblib')
        joblib.dump(head.state(), path)
        loaded_state = joblib.load(path)
    restored = XgbHead.fromState(loaded_state)
    p1 = np.asarray(head.predict(X), dtype=float)
    p2 = np.asarray(restored.predict(X), dtype=float)
    assert np.array_equal(p1, p2)


def test_head_deepcopies_after_fit():
    X, y = _synthetic_xy()
    head = XgbHead().fit(X, y)
    cloned = copy.deepcopy(head)
    p1 = np.asarray(head.predict(X), dtype=float)
    p2 = np.asarray(cloned.predict(X), dtype=float)
    assert np.array_equal(p1, p2)
    # Mutating the clone's params must not affect the original (real copy).
    cloned._params['max_depth'] = 99
    assert head._params['max_depth'] == XGB_HEAD_PARAMS['max_depth']


# --------------------------------------------------------------------------- #
# featureGains
# --------------------------------------------------------------------------- #

def test_feature_gains_covers_every_training_column_including_zero_gain():
    n = 200
    rng = np.random.default_rng(1)
    # lag_0/lag_1 drive y; lag_2 is pure noise unrelated to y and should
    # plausibly get zero (or at least very low) gain, but MUST still appear.
    X = pd.DataFrame({
        'lag_0': rng.normal(size=n),
        'lag_1': rng.normal(size=n),
        'lag_2': rng.normal(size=n),
        'p0_delta_0': np.zeros(n),  # constant -> can never be split on -> 0 gain
        'p0_delta_1': np.zeros(n),
    })
    y = 3.0 * X['lag_0'] - 2.0 * X['lag_1'] + 0.05 * rng.normal(size=n)
    head = XgbHead().fit(X, y)
    gains = head.featureGains()
    assert set(gains.keys()) == set(X.columns)
    for col in X.columns:
        assert gains[col] >= 0.0
    # The constant columns never split -> must be present and exactly 0.0,
    # not silently dropped (Booster.get_score omits zero-gain features).
    assert gains['p0_delta_0'] == 0.0
    assert gains['p0_delta_1'] == 0.0
    # The two real signal columns should have picked up positive gain.
    assert gains['lag_0'] > 0.0
    assert gains['lag_1'] > 0.0


def test_feature_gains_before_fit_returns_zeros_for_no_columns():
    head = XgbHead()
    assert head.featureGains() == {}


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

def test_head_registry_lookup_works():
    assert HEAD_REGISTRY['xgboost'] is XgbHead
    X, y = _synthetic_xy()
    HeadCls = HEAD_REGISTRY['xgboost']
    head = HeadCls().fit(X, y)
    preds = head.predict(X)
    assert np.isfinite(np.asarray(preds, dtype=float)).all()


# --------------------------------------------------------------------------- #
# NaN handling (target lag columns can contain NaN per features.py)
# --------------------------------------------------------------------------- #

def test_fit_and_predict_with_nan_in_lag_columns():
    X, y = _synthetic_xy(with_nan=True)
    head = XgbHead().fit(X, y)
    preds = head.predict(X)
    assert np.isfinite(np.asarray(preds, dtype=float)).all()
    gains = head.featureGains()
    assert set(gains.keys()) == set(X.columns)


def test_base_head_interface_raises_not_implemented():
    head = Head()
    try:
        head.fit(None, None)
        assert False, 'expected NotImplementedError'
    except NotImplementedError:
        pass
    try:
        head.predict(None)
        assert False, 'expected NotImplementedError'
    except NotImplementedError:
        pass
    try:
        head.state()
        assert False, 'expected NotImplementedError'
    except NotImplementedError:
        pass
    try:
        head.featureGains()
        assert False, 'expected NotImplementedError'
    except NotImplementedError:
        pass
    try:
        Head.fromState({})
        assert False, 'expected NotImplementedError'
    except NotImplementedError:
        pass


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
