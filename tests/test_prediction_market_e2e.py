"""End-to-end wiring test for the prediction market.

Exercises the full predictor→host→payment path without mocking the pieces
under test:

  1. A predictor builds a PredictionSubmission (with wallet pubkey) and
     delivers it to the host via the same save path the real listener uses.
  2. The host's scoring pipeline runs on a new observation using
     compute_payouts + the MAE scorer.
  3. The wallet pubkey is read from the competition_predictions table
     (not from subscriptions) — this is the fix for the wallet-lookup bug.
  4. A simulated channel payment is recorded in competition_payments.
  5. The leaderboard and host stats reflect the real numbers.

The payment primitives (openChannel, sendChannelPayment) are replaced with
deterministic stubs because they otherwise require a live wallet + relay.
Everything else — DB writes, scoring, wallet-pubkey propagation — is real.
"""

import importlib.util
import os
import sys
import tempfile
import time
import pytest

_NEURON_LITE = os.path.join(os.path.dirname(__file__), '..', 'neuron-lite')

# Load network_db
_db_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(_NEURON_LITE, 'satorineuron', 'network_db.py'))
_db_mod = importlib.util.module_from_spec(_db_spec)
_db_spec.loader.exec_module(_db_mod)
NetworkDB = _db_mod.NetworkDB

# Load competition_scoring
_cs_spec = importlib.util.spec_from_file_location(
    'competition_scoring',
    os.path.join(_NEURON_LITE, 'satorineuron', 'competition_scoring.py'))
_cs_mod = importlib.util.module_from_spec(_cs_spec)
_cs_spec.loader.exec_module(_cs_mod)
compute_payouts = _cs_mod.compute_payouts


# ── Fake payment adapter ─────────────────────────────────────────────────────

class FakePaymentAdapter:
    """Stand-in for the host's channel layer.

    Mirrors what _competitionPayPredictor does: opens a channel if needed,
    then sends a payment. Here we just record the calls so the test can
    assert the correct wallet pubkey was resolved and routed to.
    """
    def __init__(self):
        self.channels: dict[str, dict] = {}  # receiver_wallet -> channel dict
        self.payments: list[dict] = []  # list of {receiver_wallet, sats, stream}

    def open_channel(self, receiver_wallet_pubkey: str, amount_sats: int) -> str:
        p2sh = f'p2sh_for_{receiver_wallet_pubkey}'
        self.channels[receiver_wallet_pubkey] = {
            'p2sh_address': p2sh,
            'receiver_pubkey': receiver_wallet_pubkey,
            'remainder_sats': amount_sats,
        }
        return p2sh

    def send_payment(self, p2sh_address: str, sats: int, stream_name: str):
        # Deduct from channel balance
        for ch in self.channels.values():
            if ch['p2sh_address'] == p2sh_address:
                ch['remainder_sats'] -= sats
                break
        self.payments.append({
            'p2sh_address': p2sh_address,
            'sats': sats,
            'stream_name': stream_name,
        })

    def get_channel_for(self, receiver_wallet_pubkey: str, min_sats: int):
        ch = self.channels.get(receiver_wallet_pubkey)
        if ch and ch['remainder_sats'] >= min_sats:
            return ch
        return None


def score_and_pay(
    db: NetworkDB,
    payment_adapter: FakePaymentAdapter,
    stream_name: str,
    stream_provider_pubkey: str,
    host_pubkey: str,
    seq_num: int,
    observed_value,
):
    """Host-side pipeline that mirrors _competitionScoreAndPay +
    _competitionPayPredictor without async/network concerns.

    The purpose is to hit the exact same wallet-lookup-from-predictions
    path that the real code uses, so the test exercises the bug fix.
    """
    competition = db.get_competition(
        stream_name, stream_provider_pubkey, host_pubkey)
    if not competition or not competition.get('active'):
        return
    try:
        observed = float(observed_value)
    except (TypeError, ValueError):
        return

    predictions = db.get_competition_predictions(
        stream_name, stream_provider_pubkey, seq_num)
    if not predictions:
        return

    payouts = compute_payouts(competition, predictions, observed)
    preds_by_pubkey = {row['predictor_pubkey']: row for row in predictions}

    for predictor_pubkey, sats in payouts.items():
        row = preds_by_pubkey.get(predictor_pubkey) or {}
        wallet_pubkey = row.get('predictor_wallet_pubkey')
        if not wallet_pubkey:
            # This is the bug-fix gate: if wallet pubkey wasn't carried
            # through, we skip. In the broken code this was always the
            # outcome — the test asserts it is NOT the outcome here.
            continue

        channel = payment_adapter.get_channel_for(wallet_pubkey, sats)
        if not channel:
            p2sh = payment_adapter.open_channel(wallet_pubkey, 1_000_000)
            channel = payment_adapter.get_channel_for(wallet_pubkey, sats)
            assert channel is not None

        payment_adapter.send_payment(channel['p2sh_address'], sats, stream_name)
        db.record_competition_payment(
            stream_name=stream_name,
            stream_provider_pubkey=stream_provider_pubkey,
            predictor_pubkey=predictor_pubkey,
            seq_num=seq_num,
            sats_paid=sats,
            paid_at=int(time.time()),
        )


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield NetworkDB(os.path.join(tmpdir, 'e2e.db'))


@pytest.fixture
def adapter():
    return FakePaymentAdapter()


HOST_PK = 'host_nostr_pk'
PROVIDER_PK = 'provider_nostr_pk'
STREAM = 'btc-price-usd'


def _add_competition(db, pay=1000, paid_predictors=3):
    db.add_competition(
        stream_name=STREAM,
        stream_provider_pubkey=PROVIDER_PK,
        host_pubkey=HOST_PK,
        pay_per_obs_sats=pay,
        paid_predictors=paid_predictors,
        competing_predictors=10,
        scoring_metric='mae',
        scoring_params='{}',
        horizon=1,
        active=True,
        timestamp=int(time.time()),
    )


def _predictor_submits(db, predictor_pk, wallet_pk, predicted_value,
                       seq_num=1):
    """Simulate a prediction DM arriving at the host: save to DB with
    wallet pubkey, the same way _incomingPredictionsLoop does."""
    db.save_competition_prediction(
        stream_name=STREAM,
        stream_provider_pubkey=PROVIDER_PK,
        predictor_pubkey=predictor_pk,
        host_pubkey=HOST_PK,
        seq_num=seq_num,
        predicted_value=str(predicted_value),
        received_at=int(time.time()),
        predictor_wallet_pubkey=wallet_pk,
    )


# ── Tests ────────────────────────────────────────────────────────────────────

class TestWalletPubkeyPropagation:
    """BUG #1 — wallet pubkey must be read from the predictions table,
    not looked up via subscriptions/get_active."""

    def test_wallet_pubkey_stored_with_prediction(self, db):
        _add_competition(db)
        _predictor_submits(db, 'alice_nostr', 'alice_wallet', 100.0)

        rows = db.get_competition_predictions(STREAM, PROVIDER_PK, 1)
        assert len(rows) == 1
        assert rows[0]['predictor_wallet_pubkey'] == 'alice_wallet'

    def test_payment_flows_to_wallet_from_prediction_row(self, db, adapter):
        _add_competition(db)
        _predictor_submits(db, 'alice_nostr', 'alice_wallet', 100.0)

        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value=100.0)

        assert len(adapter.payments) == 1
        assert adapter.payments[0]['sats'] == 1000
        assert 'alice_wallet' in adapter.channels

    def test_predictor_with_no_wallet_pubkey_is_skipped(self, db, adapter):
        """Safety: if (somehow) the prediction row has no wallet, skip the
        payment rather than crashing or paying the wrong person."""
        _add_competition(db)
        db.save_competition_prediction(
            stream_name=STREAM,
            stream_provider_pubkey=PROVIDER_PK,
            predictor_pubkey='alice_nostr',
            host_pubkey=HOST_PK,
            seq_num=1,
            predicted_value='100.0',
            received_at=int(time.time()),
            predictor_wallet_pubkey=None,
        )

        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value=100.0)
        assert adapter.payments == []
        assert db.get_competition_payments(STREAM, PROVIDER_PK) == []


class TestEndToEndScoringRun:
    """Full predictor → host → payment round trip with multiple predictors."""

    def test_three_predictors_one_observation(self, db, adapter):
        _add_competition(db, pay=1000, paid_predictors=3)
        # Use non-zero errors so inverse-error weighting splits between all three
        _predictor_submits(db, 'alice', 'alice_wallet', 98.0)    # err=2
        _predictor_submits(db, 'bob',   'bob_wallet',   95.0)    # err=5
        _predictor_submits(db, 'carol', 'carol_wallet', 80.0)    # err=20

        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value=100.0)

        # All three paid (non-zero errors mean none dominates)
        assert len(adapter.payments) == 3
        # Total pot respected
        assert sum(p['sats'] for p in adapter.payments) <= 1000
        # All three wallets had channels opened
        assert 'alice_wallet' in adapter.channels
        assert 'bob_wallet' in adapter.channels
        assert 'carol_wallet' in adapter.channels

        # competition_payments table populated
        pays = db.get_competition_payments(STREAM, PROVIDER_PK)
        assert len(pays) == 3
        by_pk = {p['predictor_pubkey']: p for p in pays}
        # Alice (smallest error) gets the most under inverse-error weighting
        assert by_pk['alice']['sats_paid'] >= by_pk['bob']['sats_paid']
        assert by_pk['bob']['sats_paid'] >= by_pk['carol']['sats_paid']

    def test_paid_predictors_cap_honoured(self, db, adapter):
        """paid_predictors=2 means only the top 2 get paid even with 3 submissions."""
        _add_competition(db, pay=1000, paid_predictors=2)
        _predictor_submits(db, 'alice', 'alice_wallet', 100.0)
        _predictor_submits(db, 'bob',   'bob_wallet',    95.0)
        _predictor_submits(db, 'carol', 'carol_wallet',  50.0)

        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value=100.0)

        assert len(adapter.payments) == 2
        paid_wallets = set(adapter.channels.keys())
        assert paid_wallets == {'alice_wallet', 'bob_wallet'}

    def test_leaderboard_aggregates_multiple_observations(self, db, adapter):
        _add_competition(db, pay=1000, paid_predictors=2)
        # seq 1
        _predictor_submits(db, 'alice', 'alice_wallet', 100.0, seq_num=1)
        _predictor_submits(db, 'bob',   'bob_wallet',    50.0, seq_num=1)
        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value=100.0)
        # seq 2
        _predictor_submits(db, 'alice', 'alice_wallet', 200.0, seq_num=2)
        _predictor_submits(db, 'bob',   'bob_wallet',   150.0, seq_num=2)
        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=2, observed_value=200.0)

        board = db.get_competition_leaderboard(STREAM, PROVIDER_PK)
        by_pk = {r['predictor_pubkey']: r for r in board}
        # Alice predicted exactly both times and should outrank Bob
        assert by_pk['alice']['total_sats'] > by_pk['bob']['total_sats']
        assert by_pk['alice']['prediction_count'] == 2
        assert by_pk['bob']['prediction_count'] == 2

        stats = db.get_host_payment_stats(STREAM, PROVIDER_PK, HOST_PK)
        assert stats['scored_observations'] == 2
        assert stats['announced_per_obs'] == 1000
        # Pot must never exceed announcement
        assert stats['avg_paid_per_obs'] <= 1000


class TestNonNumericObservation:
    """BUG #5 — scoring pipeline must not crash on non-numeric observations."""

    def test_non_numeric_value_does_not_crash(self, db, adapter):
        _add_competition(db)
        _predictor_submits(db, 'alice', 'alice_wallet', 100.0)

        # Should silently skip rather than raise
        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value='not-a-number')
        assert adapter.payments == []
        assert db.get_competition_payments(STREAM, PROVIDER_PK) == []


class TestJoinedCompetitions:
    """BUG #3 — predictor side must persist which hosts it's joined for
    each stream, so the engine knows where to DM predictions."""

    def test_join_and_lookup_by_stream(self, db):
        db.join_competition(
            stream_name=STREAM,
            stream_provider_pubkey=PROVIDER_PK,
            host_pubkey=HOST_PK)

        joined = db.get_joined_competitions_for_stream(STREAM, PROVIDER_PK)
        assert len(joined) == 1
        assert joined[0]['host_pubkey'] == HOST_PK

    def test_join_is_idempotent(self, db):
        db.join_competition(STREAM, PROVIDER_PK, HOST_PK)
        db.join_competition(STREAM, PROVIDER_PK, HOST_PK)
        assert len(db.get_joined_competitions_for_stream(STREAM, PROVIDER_PK)) == 1

    def test_multiple_hosts_for_same_stream(self, db):
        db.join_competition(STREAM, PROVIDER_PK, 'host_a')
        db.join_competition(STREAM, PROVIDER_PK, 'host_b')
        joined = db.get_joined_competitions_for_stream(STREAM, PROVIDER_PK)
        host_pks = {r['host_pubkey'] for r in joined}
        assert host_pks == {'host_a', 'host_b'}

    def test_leave_competition(self, db):
        db.join_competition(STREAM, PROVIDER_PK, HOST_PK)
        db.leave_competition(STREAM, PROVIDER_PK, HOST_PK)
        assert db.get_joined_competitions_for_stream(STREAM, PROVIDER_PK) == []

    def test_stream_not_joined_returns_empty(self, db):
        assert db.get_joined_competitions_for_stream('other', PROVIDER_PK) == []


class TestLateArrival:
    """BUG #6 — if a prediction arrives after its observation, a second
    scoring pass should still pay the predictor. Here we verify the
    idempotent building blocks (observation by seq, re-scoring the same
    seq adds more predictions)."""

    def test_observation_by_seq_round_trip(self, db):
        db.save_observation(
            stream_name=STREAM,
            provider_pubkey=PROVIDER_PK,
            value='100.0',
            event_id='evt-1',
            seq_num=42,
            observed_at=int(time.time()))
        row = db.get_observation_by_seq(STREAM, PROVIDER_PK, 42)
        assert row is not None
        assert row['seq_num'] == 42
        assert row['value'] == '100.0'

    def test_observation_missing_returns_none(self, db):
        assert db.get_observation_by_seq(STREAM, PROVIDER_PK, 999) is None

    def test_late_prediction_gets_scored_on_second_pass(self, db, adapter):
        _add_competition(db, pay=1000, paid_predictors=2)
        # Alice predicts early, observation arrives, she gets paid
        _predictor_submits(db, 'alice', 'alice_wallet', 100.0)
        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value=100.0)
        assert len(db.get_competition_payments(STREAM, PROVIDER_PK)) == 1

        # Bob's prediction shows up late (after scoring already ran)
        _predictor_submits(db, 'bob', 'bob_wallet', 95.0)
        # Re-running the pipeline picks Bob up and pays him too
        score_and_pay(db, adapter, STREAM, PROVIDER_PK, HOST_PK,
                      seq_num=1, observed_value=100.0)
        pays = db.get_competition_payments(STREAM, PROVIDER_PK)
        payees = {p['predictor_pubkey'] for p in pays}
        assert 'bob' in payees
