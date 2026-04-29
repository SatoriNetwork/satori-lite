"""MAE (Mean Absolute Error) scoring module.

Interface contract (all scoring modules must follow this):

    def score(payload: dict) -> dict[str, int]

payload keys:
    observed_value    float  — the actual observation value
    observed_at       int    — unix timestamp of the current observation
    prev_observed_at  int    — unix timestamp of the previous observation (0 if none)
    predictions       list   — each entry is a dict:
        predictor_pubkey   str   — who made the prediction
        predicted_value    float — the predicted value
        received_at        int   — unix timestamp when the host received it
    pay_per_obs_sats  int    — total sats budget for this observation
    paid_predictors   int    — how many top predictors to pay
    scoring_params    dict   — module-specific parameters (may be empty)

returns:
    dict mapping predictor_pubkey -> sats to pay (zero-value keys omitted)
    sum of values must not exceed pay_per_obs_sats

Timing fields allow modules to enforce submission windows — e.g. disqualify
predictions submitted too close to the observation they're predicting.
"""


def score(payload: dict) -> dict:
    observed = float(payload['observed_value'])
    predictions = payload['predictions']
    budget = int(payload['pay_per_obs_sats'])
    n_paid = int(payload['paid_predictors'])
    observed_at = payload.get('observed_at', 0)
    prev_observed_at = payload.get('prev_observed_at', 0)
    params = payload.get('scoring_params') or {}
    cutoff_pct = float(params.get('late_cutoff_pct', 0.9))

    if not predictions:
        return {}

    # Disqualify predictions submitted too late in the observation window.
    # If a prediction's received_at is past 90% of the interval between
    # the previous observation and the current one, the predictor was
    # gaming by waiting until the answer was nearly obvious.
    if prev_observed_at and observed_at and observed_at > prev_observed_at:
        window = observed_at - prev_observed_at
        deadline = prev_observed_at + window * cutoff_pct
        predictions = [p for p in predictions if p.get('received_at', 0) <= deadline]

    if not predictions:
        return {}

    # Rank by ascending absolute error (lower is better)
    ranked = sorted(
        predictions,
        key=lambda p: abs(float(p['predicted_value']) - observed),
    )
    # Cap paid slots to budget so we never pay more people than we have
    # sats for — avoids integer truncation zeroing out top predictors
    # and handing the remainder to the worst-ranked recipient.
    n_paid = min(n_paid, budget)
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
        if i == 0:
            # Best predictor gets remainder to avoid rounding loss
            continue
        sats = int(budget * w / total_weight)
        if sats > 0:
            payouts[p['predictor_pubkey']] = sats
        allocated += sats
    # Assign remainder to the best predictor (index 0)
    remainder = budget - allocated
    if remainder > 0:
        payouts[top[0]['predictor_pubkey']] = remainder

    return payouts
