"""MAE (Mean Absolute Error) scoring module.

Interface contract (all scoring modules must follow this):

    def score(payload: dict) -> dict[str, int]

payload keys:
    observed_value   float  — the actual observation value
    predictions      list   — [{'predictor_pubkey': str, 'predicted_value': float}, ...]
    pay_per_obs_sats int    — total sats budget for this observation
    paid_predictors  int    — how many top predictors to pay
    scoring_params   dict   — module-specific parameters (may be empty)

returns:
    dict mapping predictor_pubkey -> sats to pay (zero-value keys omitted)
    sum of values must not exceed pay_per_obs_sats
"""


def score(payload: dict) -> dict:
    observed = float(payload['observed_value'])
    predictions = payload['predictions']
    budget = int(payload['pay_per_obs_sats'])
    n_paid = int(payload['paid_predictors'])

    if not predictions:
        return {}

    # Rank by ascending absolute error (lower is better)
    ranked = sorted(
        predictions,
        key=lambda p: abs(float(p['predicted_value']) - observed),
    )
    top = ranked[:n_paid]

    if not top:
        return {}

    # Inverse-error weighting: weight_i = 1 / (error_i + epsilon)
    # so predictors closer to truth receive proportionally more
    epsilon = 1e-9
    weights = [1.0 / (abs(float(p['predicted_value']) - observed) + epsilon)
               for p in top]
    total_weight = sum(weights)

    payouts = {}
    allocated = 0
    for i, (p, w) in enumerate(zip(top, weights)):
        if i == len(top) - 1:
            # Last recipient gets remainder to avoid rounding loss
            sats = budget - allocated
        else:
            sats = int(budget * w / total_weight)
        if sats > 0:
            payouts[p['predictor_pubkey']] = sats
        allocated += sats

    return payouts
