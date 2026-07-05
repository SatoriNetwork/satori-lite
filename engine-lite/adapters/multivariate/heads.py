"""Pluggable head models for the Jordan-1 multivariate adapter.

A "head" is the only trained component in the multivariate stack (see
``docs/engine/Jordan-1_MULTIVARIATE.md`` section 3 and
``docs/engine/MULTIVARIATE.md`` section 3.4): it consumes the feature matrix
produced by ``features.py`` (target lags + peer deltas) and predicts the
target's next-step level diff.

This module is intentionally free of engine singletons, sqlite, and torch --
same "pure function / pure class" discipline as ``features.py`` -- so it stays
independently unit-testable and safe to ``copy.deepcopy`` inside the adapter's
pilot/stable swap (``engine.py``'s ``self.stable = copy.deepcopy(self.pilot)``).

Interface (duck-typed, not an ABC-enforced plugin system):

    fit(X, y) -> Head          train in place, returns self
    predict(X) -> array-like   same feature columns/order as fit
    state() -> dict            joblib-serializable; safe to embed in the
                                adapter's persisted state under 'head_state'
    fromState(state) -> Head   classmethod, inverse of state()
    featureGains() -> dict     per-feature XGBoost gain, EVERY training
                                column present (0.0 if it never split)

Registry: ``HEAD_REGISTRY = {'xgboost': XgbHead}``. The adapter persists
``head_name`` alongside ``head_state`` (Jordan-1 section 4) and looks the
class up here on load.
"""

from __future__ import annotations

import pandas as pd
from xgboost import XGBRegressor

# Fixed conservative params (Jordan-1 section 3 / MULTIVARIATE.md 3.4):
# training rows are thin (tens to low hundreds), so there is no hyperparameter
# search -- just one deliberately shallow, regularized configuration. The
# fixed seed is load-bearing: the random-swap peer search (Jordan-1 section 3)
# retrains the SAME peer set with the SAME seed to get `mae_base`, then swaps
# one peer and retrains again with the SAME seed for `mae_new`; if the seed
# floated, the swap's MAE delta would be contaminated by training noise, not
# attributable to the peer change.
XGB_HEAD_PARAMS: dict = {
    'max_depth': 3,
    'n_estimators': 200,
    'learning_rate': 0.05,
    'min_child_weight': 5,
    'subsample': 0.8,
    'eval_metric': 'mae',
    'random_state': 0,
}


class Head:
    """Minimal duck-typed head interface. Not meant to be instantiated."""

    def fit(self, X, y) -> 'Head':
        raise NotImplementedError

    def predict(self, X):
        raise NotImplementedError

    def state(self) -> dict:
        raise NotImplementedError

    @classmethod
    def fromState(cls, state: dict) -> 'Head':
        raise NotImplementedError

    def featureGains(self) -> dict:
        raise NotImplementedError


class XgbHead(Head):
    """XGBoost regression head, fixed conservative hyperparameters.

    Feature columns are captured at ``fit`` time (from the DataFrame's column
    order) and re-applied on ``predict``/``featureGains`` so a plain array
    input still lines up with the columns the booster was trained on, and so
    ``featureGains`` can report a zero for any column the booster never split
    on (``Booster.get_score`` silently omits those).
    """

    def __init__(self, params: dict | None = None):
        # Copy so callers mutating their own dict afterward can't reach in.
        self._params: dict = dict(params) if params is not None else dict(XGB_HEAD_PARAMS)
        self.model: XGBRegressor | None = None
        self._featureColumns: list[str] = []

    def fit(self, X, y) -> 'XgbHead':
        X = X if isinstance(X, pd.DataFrame) else pd.DataFrame(X)
        self._featureColumns = list(X.columns)
        model = XGBRegressor(**self._params)
        model.fit(X, y)
        self.model = model
        return self

    def predict(self, X):
        if self.model is None:
            raise RuntimeError('XgbHead.predict called before fit/fromState')
        if not isinstance(X, pd.DataFrame):
            X = pd.DataFrame(X, columns=self._featureColumns)
        return self.model.predict(X)

    def featureGains(self) -> dict:
        """Per-feature gain, normalized to include every training column.

        ``Booster.get_score(importance_type='gain')`` keys its result by
        feature name (since the model was fit on a named DataFrame) but
        OMITS any feature with zero total gain (never used in a split). Peer
        search (Jordan-1 section 3 step 2) needs every ``p{k}_delta_*``
        column present -- a missing key would silently drop a weak/unused
        peer out of the "weakest peer" ranking instead of correctly scoring
        it 0.0.
        """
        if self.model is None:
            return {col: 0.0 for col in self._featureColumns}
        raw = self.model.get_booster().get_score(importance_type='gain')
        return {col: float(raw.get(col, 0.0)) for col in self._featureColumns}

    def state(self) -> dict:
        """Plain dict, joblib-serializable. Includes the fitted estimator.

        XGBRegressor is picklable/deepcopy-safe (verified: round-trips through
        joblib.dump/load and copy.deepcopy with identical predictions), so
        embedding it directly is simpler and just as safe as extracting the
        booster's raw bytes, and it keeps ``fromState`` trivial.
        """
        return {
            'model': self.model,
            'feature_columns': list(self._featureColumns),
            'params': dict(self._params),
        }

    @classmethod
    def fromState(cls, state: dict) -> 'XgbHead':
        head = cls(params=state.get('params'))
        head.model = state.get('model')
        head._featureColumns = list(state.get('feature_columns', []))
        return head


# head_name -> Head subclass. The adapter persists 'head_name' (Jordan-1
# section 4) and looks the class up here on load; new heads (linear, LightGBM,
# MLP) are a registry entry, no adapter changes.
HEAD_REGISTRY: dict = {'xgboost': XgbHead}
