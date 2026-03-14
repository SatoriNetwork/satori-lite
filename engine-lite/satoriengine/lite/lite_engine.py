"""Lightweight stateless prediction engine for Nostr streams.

Uses simple statistical methods (mean, linear regression) to predict next
values from recent observations.
"""

import json
import numpy as np
from sklearn.linear_model import LinearRegression


class LiteEngine:
    """Stateless prediction engine for numeric observation streams."""

    def predict(self, observations: list[dict]) -> str | None:
        """Predict next value from recent observations.

        Args:
            observations: list of observation dicts (newest-first),
                          each with a 'value' key.

        Returns:
            Predicted value as a string, or None if prediction isn't possible.
        """
        if not observations:
            return None

        # Extract numeric values (reverse to chronological order)
        values = []
        for obs in reversed(observations):
            v = self._extract_numeric(obs.get('value', ''))
            if v is not None:
                values.append(v)

        if not values:
            return None

        n = len(values)
        if n == 1:
            result = values[0]
        elif n < 5:
            # Mean of last 2 values
            result = (values[-1] + values[-2]) / 2
        else:
            # Linear regression: predict at next time step
            result = self._linear_regression_predict(values)

        return str(result)

    def _extract_numeric(self, value_str: str) -> float | None:
        """Parse a string value to float, handling JSON-encoded numbers."""
        if not isinstance(value_str, str):
            try:
                return float(value_str)
            except (TypeError, ValueError):
                return None

        # Try direct float parse
        try:
            return float(value_str)
        except ValueError:
            pass

        # Try JSON decode (handles quoted numbers like '"42.5"'
        # and observation objects like '{"value": "0.091", ...}')
        try:
            decoded = json.loads(value_str)
            if isinstance(decoded, dict) and 'value' in decoded:
                return float(decoded['value'])
            return float(decoded)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def _linear_regression_predict(self, values: list[float]) -> float:
        """Predict next value using sklearn LinearRegression.

        Uses observation indices as X (0..n-1), values as Y.
        Predicts at X = n (next time step).
        Matches StarterAdapter's approach from engine-lite.
        """
        n = len(values)
        x = np.arange(n).reshape(-1, 1)
        y = np.array(values)
        model = LinearRegression()
        model.fit(x, y)
        return model.predict([[n]])[0]
