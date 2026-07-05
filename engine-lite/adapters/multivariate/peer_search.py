"""Random-swap peer-search primitives for the Jordan-1 multivariate adapter.

Pure logic, stdlib only (``math`` + ``random``): no engine singletons, no
sqlite, no pandas, no torch. The adapter (Jordan-1 roadmap step 5) loads the
candidate histories and gains itself and calls these helpers with plain data,
so the search policy stays independently unit-testable and deepcopy-safe.

The design is ``docs/engine/Jordan-1_MULTIVARIATE.md`` sections 3
("Random-swap peer search") and 4 ("Persisted state"). One swap is attempted
per retrain:

    1. Train a baseline head on the current K peers -> ``mae_base``.
    2. Rank peers by summed XGBoost gain of their ``p{k}_delta_*`` columns;
       the lowest is the weakest (:func:`weakestPeer`).
    3. Draw one random candidate from the eligible pool
       (:func:`eligiblePool` + :func:`pickCandidate`).
    4. Swap weakest -> candidate, retrain (same seed) -> ``mae_new``.
    5. Keep iff :func:`acceptSwap` (beat ``mae_base`` by ``keep_margin``).
       On reject the candidate cools down (:func:`pruneCooldown`).

Every attempt is recorded in the ledger (:func:`appendLedger`).

Column-naming contract (must match ``features.py``): the k-th peer in the
ordered peer set owns columns ``p{k}_delta_0`` and ``p{k}_delta_1``. Peer
index -- not uuid -- keys the gain lookup, so ``weakestPeer`` takes the
ordered ``peer_uuids`` and maps position -> columns.

Ledger ``reason`` semantics (Task 5 must use these consistently):

    'initial'         first fit; no swap attempted (swapped_in/out both None).
    'margin'          a swap was accepted -- it beat ``keep_margin``.
    'no_improvement'  a swap was attempted and rejected.

State fields Task 5 persists (Jordan-1 section 4):

    'cooldown'      dict[uuid, int] -- uuid -> target row count at which the
                    candidate was rejected. A uuid is cooling down while
                    ``current_rows - cooldown[uuid] < cooldown_rows``; once
                    that gap reaches ``cooldown_rows`` it is eligible again.
                    Prune expired entries with :func:`pruneCooldown` before
                    persisting so the dict does not grow without bound.
    'retired_peers' dict[uuid, int] -- uuid -> target row count when it was
                    last swapped OUT of the working set. Informational /
                    audit trail; ``eligiblePool`` does not read it (a retired
                    peer re-enters the pool once its cooldown expires).
    'swap_ledger'   list[dict] -- LRU-capped audit log built with
                    :func:`appendLedger` (newest kept, cap ``max_entries``).
"""

from __future__ import annotations

import math
import random

# Zero-variance guard: a candidate whose observed values never move carries no
# signal and would only add a constant (useless) column, so it is dropped when
# the caller has actually measured its variance. Absent-from-``variances`` means
# "not yet computed" -> kept (the adapter may defer variance to alignment time).
_VARIANCE_EPS = 1e-12

# Default cooldown window (target rows) a rejected candidate must wait out.
_DEFAULT_COOLDOWN_ROWS = 100

# Default LRU cap on the persisted swap ledger (Jordan-1 section 4).
_DEFAULT_MAX_LEDGER_ENTRIES = 200

# Allowed ledger ``reason`` values (see module docstring).
_LEDGER_REASONS = frozenset({'initial', 'margin', 'no_improvement'})

# Required keys on every ledger entry (Jordan-1 section 4 schema).
_LEDGER_KEYS = frozenset({
    'at_rows', 'swapped_out', 'swapped_in',
    'prev_test_mae', 'new_test_mae', 'kept', 'reason',
})


def eligiblePool(
    candidates,
    target_uuid: str,
    row_counts: dict,
    cooldown: dict,
    current_rows: int,
    peer_min_rows: int = 30,
    exclude=(),
    cooldown_rows: int = _DEFAULT_COOLDOWN_ROWS,
    variances: dict | None = None,
) -> list:
    """Candidates eligible to enter the peer set, in caller order (deduped).

    A uuid survives every filter (Jordan-1 section 3, "Initial peer set" /
    "Retrain step"):

    * it is not the target itself;
    * it is not a ``_pred`` (prediction) stream;
    * it has ``>= peer_min_rows`` rows (missing from ``row_counts`` -> 0 ->
      dropped);
    * it is not already in the working set (``exclude``);
    * it is not in an *active* cooldown -- cooling down while
      ``current_rows - cooldown[uuid] < cooldown_rows``; an expired entry
      (gap ``>= cooldown_rows``) is eligible again;
    * if ``variances`` is supplied AND the uuid is present in it, its variance
      exceeds ``_VARIANCE_EPS`` (a flat stream is dropped). A uuid absent from
      ``variances`` is kept -- variance is simply unknown here.

    Args:
        candidates: iterable of candidate uuids (StreamStore uuids).
        target_uuid: the stream being predicted; always excluded.
        row_counts: dict[uuid, int] row count per candidate.
        cooldown: dict[uuid, int] uuid -> rejection row count (see module doc).
        current_rows: the target's current row count (the clock).
        peer_min_rows: minimum rows a candidate needs.
        exclude: iterable of uuids already in the working set.
        cooldown_rows: cooldown window length in target rows.
        variances: optional dict[uuid, float] observed variance per candidate.

    Returns:
        list[str] eligible uuids, first occurrence order, duplicates removed.
    """
    excludeSet = set(exclude)
    out: list = []
    seen: set = set()
    for uuid in candidates:
        if uuid in seen:
            continue
        if uuid == target_uuid:
            continue
        if isinstance(uuid, str) and uuid.endswith('_pred'):
            continue
        if row_counts.get(uuid, 0) < peer_min_rows:
            continue
        if uuid in excludeSet:
            continue
        if uuid in cooldown and (current_rows - cooldown[uuid]) < cooldown_rows:
            continue
        if variances is not None and uuid in variances:
            if abs(variances[uuid]) <= _VARIANCE_EPS:
                continue
        seen.add(uuid)
        out.append(uuid)
    return out


def initialPeers(pool, k: int, rng: random.Random) -> list:
    """``k`` uniform-random uuids from ``pool`` without replacement.

    Fewer than ``k`` when the pool is smaller. ``rng`` is a caller-supplied
    :class:`random.Random` so the initial draw is reproducible in tests and,
    with a persisted seed, across a node's retrains.
    """
    pool = list(pool)
    if k <= 0 or not pool:
        return []
    return rng.sample(pool, min(k, len(pool)))


def weakestPeer(peer_uuids, gains: dict, peer_added_at: dict) -> str:
    """The weakest peer in the ordered working set (to be swapped out).

    Each peer's strength is the summed XGBoost gain of its two delta columns:
    ``gains['p{k}_delta_0'] + gains['p{k}_delta_1']`` where ``k`` is the peer's
    index in ``peer_uuids``. ``gains`` comes from ``Head.featureGains()``,
    which guarantees every training column is present (0.0 if never split), but
    missing keys are still treated as 0.0 defensively. ``tfm_delta`` and target
    ``lag_*`` columns are never consulted -- only peer columns (section 3
    step 2).

    Lowest score is weakest. Ties break by oldest peer first
    (``peer_added_at[uuid]`` = target row count when it joined; lower = older),
    then by uuid lexicographic order.

    Args:
        peer_uuids: ordered current peer set (index -> ``p{k}`` columns).
        gains: per-column gain dict from ``Head.featureGains()``.
        peer_added_at: dict[uuid, int] join row count per peer.

    Returns:
        the uuid of the weakest peer.

    Raises:
        ValueError: if ``peer_uuids`` is empty.
    """
    peer_uuids = list(peer_uuids)
    if not peer_uuids:
        raise ValueError('weakestPeer: peer_uuids is empty')
    scored = []
    for k, uuid in enumerate(peer_uuids):
        score = gains.get(f'p{k}_delta_0', 0.0) + gains.get(f'p{k}_delta_1', 0.0)
        scored.append((score, peer_added_at.get(uuid, 0), uuid))
    scored.sort(key=lambda t: (t[0], t[1], t[2]))
    return scored[0][2]


def pickCandidate(pool, rng: random.Random):
    """One uniform-random candidate from ``pool``; ``None`` if the pool is empty."""
    pool = list(pool)
    if not pool:
        return None
    return rng.choice(pool)


def pruneCooldown(cooldown: dict, current_rows: int,
                  cooldown_rows: int = _DEFAULT_COOLDOWN_ROWS) -> dict:
    """Drop expired cooldown entries, keeping only still-active ones.

    A uuid is still cooling down while ``current_rows - rejected_at <
    cooldown_rows``; at exactly ``cooldown_rows`` the window has elapsed and the
    entry is dropped (boundary is exclusive of the cooldown, so the uuid is
    eligible again). Returns a new dict; the input is not mutated.
    """
    return {
        uuid: rejected_at
        for uuid, rejected_at in cooldown.items()
        if (current_rows - rejected_at) < cooldown_rows
    }


def appendLedger(ledger, entry: dict,
                 max_entries: int = _DEFAULT_MAX_LEDGER_ENTRIES) -> list:
    """Append a validated ledger entry and LRU-cap the result (newest kept).

    Validates the entry against the Jordan-1 section 4 schema before appending:
    it must carry exactly the required keys and a ``reason`` in
    ``{'initial', 'margin', 'no_improvement'}``. Returns a new list capped at
    ``max_entries`` most-recent entries; the input list is not mutated.

    Raises:
        ValueError: on a missing/unknown key or an invalid ``reason``.
    """
    keys = set(entry.keys())
    if keys != set(_LEDGER_KEYS):
        missing = set(_LEDGER_KEYS) - keys
        extra = keys - set(_LEDGER_KEYS)
        raise ValueError(
            f'appendLedger: bad entry keys (missing={sorted(missing)}, '
            f'extra={sorted(extra)})')
    if entry['reason'] not in _LEDGER_REASONS:
        raise ValueError(
            f'appendLedger: invalid reason {entry["reason"]!r}, '
            f'expected one of {sorted(_LEDGER_REASONS)}')
    out = list(ledger)
    out.append(entry)
    if max_entries is not None and max_entries >= 0 and len(out) > max_entries:
        out = out[-max_entries:]
    return out


def acceptSwap(mae_base: float, mae_new: float, keep_margin: float = 0.01) -> bool:
    """Whether a swap's ``mae_new`` beats the baseline by ``keep_margin``.

    Accept iff ``mae_new < mae_base * (1 - keep_margin)`` (Jordan-1 section 3
    step 5). Non-finite handling:

    * ``mae_new`` non-finite (NaN / inf) -> reject (a broken retrain never
      wins);
    * ``mae_base`` non-finite while ``mae_new`` is finite -> accept (any real
      score beats an undefined/``inf`` baseline, e.g. a first real fit).
    """
    if not math.isfinite(mae_new):
        return False
    if not math.isfinite(mae_base):
        return True
    return mae_new < mae_base * (1.0 - keep_margin)
