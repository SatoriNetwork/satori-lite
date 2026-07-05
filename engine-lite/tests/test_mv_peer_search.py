"""Unit tests for the Jordan-1 random-swap peer-search primitives
(``adapters/multivariate/peer_search.py``).

Covers the eligibility filters (target / ``_pred`` / thin / current-set /
cooldown / zero-variance), deterministic seeded draws, the weakest-peer ranking
and its tie-breaks, cooldown pruning boundary, ledger LRU cap + schema
validation, and the ``acceptSwap`` margin boundary + non-finite handling.

``peer_search.py`` is loaded directly from its file path (same pattern as
``test_mv_features.py`` / ``test_mv_heads.py``) so the tests never touch
``adapters/__init__.py`` or ``adapters/multivariate/__init__.py``.

Runs under pytest (``python -m pytest``) or standalone
(``python test_mv_peer_search.py``) since the image ships no pytest.
"""

import importlib.util
import math
import os
import random

_HERE = os.path.dirname(os.path.abspath(__file__))
_PS_PATH = os.path.join(
    _HERE, '..', 'adapters', 'multivariate', 'peer_search.py')
_spec = importlib.util.spec_from_file_location('mv_peer_search', _PS_PATH)
peer_search = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(peer_search)

eligiblePool = peer_search.eligiblePool
initialPeers = peer_search.initialPeers
weakestPeer = peer_search.weakestPeer
pickCandidate = peer_search.pickCandidate
pruneCooldown = peer_search.pruneCooldown
appendLedger = peer_search.appendLedger
acceptSwap = peer_search.acceptSwap


def _ledgerEntry(**overrides):
    """A schema-valid ledger entry, with optional field overrides."""
    entry = {
        'at_rows': 100,
        'swapped_out': None,
        'swapped_in': None,
        'prev_test_mae': 1.0,
        'new_test_mae': 0.9,
        'kept': True,
        'reason': 'initial',
    }
    entry.update(overrides)
    return entry


# --------------------------------------------------------------------------- #
# eligiblePool
# --------------------------------------------------------------------------- #

def test_eligible_pool_excludes_target_pred_thin_and_current_set():
    candidates = ['target', 'a', 'b_pred', 'thin', 'c', 'incurrent']
    row_counts = {'target': 500, 'a': 40, 'b_pred': 40, 'thin': 5,
                  'c': 40, 'incurrent': 40}
    out = eligiblePool(
        candidates,
        target_uuid='target',
        row_counts=row_counts,
        cooldown={},
        current_rows=500,
        peer_min_rows=30,
        exclude=('incurrent',))
    assert out == ['a', 'c'], out


def test_eligible_pool_missing_row_count_treated_as_thin():
    out = eligiblePool(
        ['a', 'unknown'],
        target_uuid='t',
        row_counts={'a': 40},   # 'unknown' absent -> 0 rows -> dropped
        cooldown={},
        current_rows=100)
    assert out == ['a']


def test_eligible_pool_active_cooldown_excluded_expired_included():
    # 'cool' rejected at row 450; window 100. At current 500, gap=50 < 100 -> out.
    # 'warm' rejected at row 300; gap=200 >= 100 -> back in.
    out = eligiblePool(
        ['cool', 'warm'],
        target_uuid='t',
        row_counts={'cool': 40, 'warm': 40},
        cooldown={'cool': 450, 'warm': 300},
        current_rows=500,
        cooldown_rows=100)
    assert out == ['warm'], out


def test_eligible_pool_cooldown_boundary_exactly_at_window_is_eligible():
    # gap == cooldown_rows -> window elapsed -> eligible.
    out = eligiblePool(
        ['x'],
        target_uuid='t',
        row_counts={'x': 40},
        cooldown={'x': 400},
        current_rows=500,
        cooldown_rows=100)
    assert out == ['x']


def test_eligible_pool_zero_variance_dropped_when_known_kept_when_absent():
    candidates = ['flat', 'moving', 'unmeasured']
    row_counts = {'flat': 40, 'moving': 40, 'unmeasured': 40}
    # 'flat' variance ~0 -> dropped; 'unmeasured' absent from variances -> kept.
    out = eligiblePool(
        candidates,
        target_uuid='t',
        row_counts=row_counts,
        cooldown={},
        current_rows=100,
        variances={'flat': 0.0, 'moving': 2.5})
    assert out == ['moving', 'unmeasured'], out

    # Without any variances dict, the flat stream is kept (variance unknown).
    out2 = eligiblePool(
        candidates, target_uuid='t', row_counts=row_counts,
        cooldown={}, current_rows=100)
    assert out2 == ['flat', 'moving', 'unmeasured'], out2


def test_eligible_pool_dedupes_preserving_first_order():
    out = eligiblePool(
        ['a', 'b', 'a'],
        target_uuid='t',
        row_counts={'a': 40, 'b': 40},
        cooldown={},
        current_rows=100)
    assert out == ['a', 'b']


# --------------------------------------------------------------------------- #
# initialPeers
# --------------------------------------------------------------------------- #

def test_initial_peers_deterministic_no_duplicates():
    pool = [f's{i}' for i in range(20)]
    a = initialPeers(pool, 5, random.Random(42))
    b = initialPeers(pool, 5, random.Random(42))
    assert a == b, 'same seed must give same draw'
    assert len(a) == 5
    assert len(set(a)) == 5, 'no duplicates'
    assert all(x in pool for x in a)


def test_initial_peers_pool_smaller_than_k():
    pool = ['a', 'b']
    out = initialPeers(pool, 5, random.Random(0))
    assert sorted(out) == ['a', 'b']
    assert len(set(out)) == 2


def test_initial_peers_empty_pool_and_nonpositive_k():
    assert initialPeers([], 5, random.Random(0)) == []
    assert initialPeers(['a', 'b'], 0, random.Random(0)) == []


# --------------------------------------------------------------------------- #
# weakestPeer
# --------------------------------------------------------------------------- #

def test_weakest_peer_lowest_summed_gain_wins():
    peers = ['u0', 'u1', 'u2']
    gains = {
        'p0_delta_0': 5.0, 'p0_delta_1': 5.0,   # 10
        'p1_delta_0': 0.5, 'p1_delta_1': 0.4,   # 0.9  <- weakest
        'p2_delta_0': 3.0, 'p2_delta_1': 1.0,   # 4
        'lag_1': 100.0, 'tfm_delta': 100.0,     # must be ignored
    }
    added = {'u0': 0, 'u1': 0, 'u2': 0}
    assert weakestPeer(peers, gains, added) == 'u1'


def test_weakest_peer_missing_gain_key_treated_as_zero():
    peers = ['u0', 'u1']
    gains = {'p0_delta_0': 1.0, 'p0_delta_1': 1.0}  # u1 columns absent -> 0
    added = {'u0': 0, 'u1': 0}
    assert weakestPeer(peers, gains, added) == 'u1'


def test_weakest_peer_tie_breaks_by_oldest_then_uuid():
    # All summed gains equal -> tie. Oldest (lowest added_at) wins.
    peers = ['zeta', 'alpha', 'mid']
    gains = {
        'p0_delta_0': 1.0, 'p0_delta_1': 1.0,
        'p1_delta_0': 1.0, 'p1_delta_1': 1.0,
        'p2_delta_0': 1.0, 'p2_delta_1': 1.0,
    }
    added = {'zeta': 50, 'alpha': 10, 'mid': 10}  # alpha & mid oldest (10)
    # alpha and mid tie on age -> uuid order -> 'alpha'.
    assert weakestPeer(peers, gains, added) == 'alpha'


def test_weakest_peer_tie_same_age_uses_uuid_order():
    peers = ['b', 'a']
    gains = {'p0_delta_0': 0.0, 'p0_delta_1': 0.0,
             'p1_delta_0': 0.0, 'p1_delta_1': 0.0}
    added = {'a': 5, 'b': 5}
    assert weakestPeer(peers, gains, added) == 'a'


def test_weakest_peer_empty_raises():
    try:
        weakestPeer([], {}, {})
    except ValueError:
        return
    raise AssertionError('expected ValueError on empty peer_uuids')


# --------------------------------------------------------------------------- #
# pickCandidate
# --------------------------------------------------------------------------- #

def test_pick_candidate_none_on_empty():
    assert pickCandidate([], random.Random(0)) is None


def test_pick_candidate_deterministic_with_seed():
    pool = [f'c{i}' for i in range(10)]
    a = pickCandidate(pool, random.Random(7))
    b = pickCandidate(pool, random.Random(7))
    assert a == b
    assert a in pool


# --------------------------------------------------------------------------- #
# pruneCooldown
# --------------------------------------------------------------------------- #

def test_prune_cooldown_keeps_active_drops_expired_boundary():
    cooldown = {
        'active': 450,    # gap 50 < 100 -> keep
        'expired': 300,   # gap 200 >= 100 -> drop
        'boundary': 400,  # gap exactly 100 -> drop (window elapsed)
    }
    out = pruneCooldown(cooldown, current_rows=500, cooldown_rows=100)
    assert out == {'active': 450}, out
    # Original not mutated.
    assert 'expired' in cooldown


# --------------------------------------------------------------------------- #
# appendLedger
# --------------------------------------------------------------------------- #

def test_append_ledger_caps_keeping_newest():
    ledger = []
    for i in range(10):
        ledger = appendLedger(ledger, _ledgerEntry(at_rows=i), max_entries=5)
    assert len(ledger) == 5
    assert [e['at_rows'] for e in ledger] == [5, 6, 7, 8, 9]


def test_append_ledger_does_not_mutate_input():
    original = [_ledgerEntry(at_rows=1)]
    appendLedger(original, _ledgerEntry(at_rows=2), max_entries=5)
    assert len(original) == 1


def test_append_ledger_rejects_missing_key():
    bad = _ledgerEntry()
    del bad['kept']
    try:
        appendLedger([], bad)
    except ValueError:
        return
    raise AssertionError('expected ValueError on missing key')


def test_append_ledger_rejects_extra_key():
    bad = _ledgerEntry()
    bad['bogus'] = 1
    try:
        appendLedger([], bad)
    except ValueError:
        return
    raise AssertionError('expected ValueError on extra key')


def test_append_ledger_rejects_bad_reason():
    bad = _ledgerEntry(reason='whatever')
    try:
        appendLedger([], bad)
    except ValueError:
        return
    raise AssertionError('expected ValueError on invalid reason')


def test_append_ledger_accepts_valid_reasons():
    for reason in ('initial', 'margin', 'no_improvement'):
        out = appendLedger([], _ledgerEntry(reason=reason))
        assert out[-1]['reason'] == reason


# --------------------------------------------------------------------------- #
# acceptSwap
# --------------------------------------------------------------------------- #

def test_accept_swap_beats_margin():
    # base 1.0, margin 0.01 -> threshold 0.99. 0.98 < 0.99 -> accept.
    assert acceptSwap(1.0, 0.98, keep_margin=0.01) is True


def test_accept_swap_margin_boundary_is_reject():
    # mae_new == mae_base*(1-margin) exactly -> strict < fails -> reject.
    base = 1.0
    margin = 0.01
    boundary = base * (1 - margin)
    assert acceptSwap(base, boundary, keep_margin=margin) is False


def test_accept_swap_no_improvement_rejected():
    assert acceptSwap(1.0, 1.5) is False


def test_accept_swap_non_finite_new_rejected():
    assert acceptSwap(1.0, float('nan')) is False
    assert acceptSwap(1.0, float('inf')) is False


def test_accept_swap_infinite_base_finite_new_accepted():
    assert acceptSwap(float('inf'), 5.0) is True
    assert acceptSwap(float('nan'), 5.0) is True


def test_accept_swap_infinite_base_and_non_finite_new_rejected():
    # non-finite new dominates: reject even against an inf baseline.
    assert acceptSwap(float('inf'), float('inf')) is False


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
