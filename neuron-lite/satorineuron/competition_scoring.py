"""Scoring pipeline for prediction market competitions.

Separated from start.py so it can be imported and tested without the full
neuron dependency tree (pandas, evrmore, etc.).
"""

import importlib.util
import json
import logging
import os

SCORING_DIR = os.path.join(os.path.dirname(__file__), '..', 'scoring')


def load_scorer(metric: str):
    """Load a scoring module by metric name from the scoring/ directory.

    Args:
        metric: Scoring metric name, e.g. 'mae'. Loaded from scoring/{metric}.py.

    Returns:
        The loaded module (must expose a `score(payload) -> dict` function).

    Raises:
        FileNotFoundError: If no module exists for the given metric.
    """
    path = os.path.join(SCORING_DIR, f'{metric}.py')
    spec = importlib.util.spec_from_file_location(f'scoring_{metric}', path)
    if not spec:
        raise FileNotFoundError(f'Scoring module not found: {metric}')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_payload(competition: dict, predictions: list, observed_value: float) -> dict:
    """Build the payload dict passed to a scoring module's score() function.

    Args:
        competition: Row dict from the competitions table.
        predictions: List of row dicts from competition_predictions table.
        observed_value: The actual observed value for this seq_num.

    Returns:
        Payload dict matching the scoring module interface contract.
    """
    scoring_params = json.loads(competition.get('scoring_params') or '{}')
    return {
        'observed_value': observed_value,
        'predictions': [
            {
                'predictor_pubkey': p['predictor_pubkey'],
                'predicted_value': float(p['predicted_value']),
            }
            for p in predictions
        ],
        'pay_per_obs_sats': competition['pay_per_obs_sats'],
        'paid_predictors': competition['paid_predictors'],
        'scoring_params': scoring_params,
    }


def compute_payouts(competition: dict, predictions: list, observed_value: float) -> dict:
    """Run the scoring module for a competition and return payout amounts.

    Args:
        competition: Row dict from the competitions table (must have active=1).
        predictions: List of prediction row dicts for this seq_num.
        observed_value: The actual value to score against.

    Returns:
        Dict mapping predictor_pubkey -> sats (only non-zero entries included).
        Empty dict if competition is inactive, no predictions, or scorer fails.
    """
    if not competition.get('active'):
        return {}
    if not predictions:
        return {}

    metric = competition['scoring_metric']
    try:
        mod = load_scorer(metric)
    except (FileNotFoundError, Exception) as e:
        logging.warning(f'Competition: scoring module error ({metric}): {e}')
        return {}

    try:
        payload = build_payload(competition, predictions, observed_value)
        return {k: v for k, v in mod.score(payload).items() if v > 0}
    except Exception as e:
        logging.warning(f'Competition: scorer {metric} raised: {e}')
        return {}
