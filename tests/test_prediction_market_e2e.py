"""End-to-end wiring test for the prediction market, on the host's real code.

  1. A predictor's submission (with its alias pubkey) reaches the host
     through the real listener, _incomingPredictionsLoop, which saves it.
  2. An observation reaches the host through _networkProcessObservation,
     which saves it and runs the real pay loop: _bountyScoreAndPay scores
     with the bounty's metric, _bountyPayPredictor records each payment and
     pays it over a channel to the alias read from the prediction's row.
  3. The leaderboard and host stats reflect the real numbers.

Only openChannel and sendChannelPayment are stand-ins, because the real ones
need a funded alias and a relay. They keep the host's channel rows as the
real ones do and list each transfer.

The stream has a clock of its own, so the scorer's submission window never
depends on how fast the test runs: observation n is at T0 + n * CADENCE, and
a prediction of it is sent a minute after observation n - 1 unless the test
says when.
"""

import asyncio
import unittest.mock as mock

import pytest

from satorilib.satori_nostr.models import (
    DatastreamObservation, InboundObservation, InboundPrediction,
    PredictionSubmission)
from satorineuron.network_db import NetworkDB

from tests.neuron_start import start

HOST_PK = 'host_nostr_pk'
PROVIDER_PK = 'provider_nostr_pk'
STREAM = 'btc-price-usd'
T0 = 1_700_000_000
CADENCE = 600


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path):
    return NetworkDB(str(tmp_path / 'e2e.db'))


@pytest.fixture
def host(db, monkeypatch):
    """A StartupDag hosting bounties on a real NetworkDB (object.__new__
    skips __init__, which needs a wallet). host.transfers lists each channel
    transfer as (receiver's alias pubkey, sats)."""
    # An empty config, whatever neuron-lite/config holds on this box.
    monkeypatch.setattr(
        start, 'config', mock.MagicMock(**{'get.return_value': {}}))
    h = object.__new__(start.StartupDag)
    h.networkDB = db
    h.nostrPubkey = HOST_PK
    h.transfers = []

    async def openChannel(receiver_pubkey, amount_sats, minutes=None,
                          blocks=None, receiver_nostr_pubkey=None):
        p2sh = f'p2sh_for_{receiver_pubkey}'
        db.save_channel(
            p2sh, 'host_alias', receiver_pubkey, 'redeem', 'funding', 0,
            amount_sats, amount_sats, True, blocks, minutes,
            receiver_nostr_pubkey=receiver_nostr_pubkey)
        return p2sh

    async def sendChannelPayment(p2sh_address, pay_amount_sats, stream_name=''):
        channel = db.get_channel(p2sh_address)
        db.update_channel_remainder(
            p2sh_address, channel['remainder_sats'] - pay_amount_sats)
        h.transfers.append((channel['receiver_pubkey'], pay_amount_sats))

    h.openChannel = openChannel
    h.sendChannelPayment = sendChannelPayment
    return h


def _add_bounty(db, pay=1000, paid_predictors=3):
    db.add_bounty(
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
        timestamp=T0,
    )


def _predictor_submits(host, predictor_pk, alias_pk, predicted_value,
                       seq_num=1, sent_at=None):
    """A prediction DM arrives at the host: the real listener saves it, and
    scores it at once when its observation is already in."""
    if sent_at is None:
        sent_at = T0 + (seq_num - 1) * CADENCE + 60

    class Relay:
        async def incoming_predictions(self):
            yield InboundPrediction(
                prediction=PredictionSubmission(
                    stream_name=STREAM,
                    stream_provider_pubkey=PROVIDER_PK,
                    predictor_pubkey=predictor_pk,
                    predictor_alias_pubkey=alias_pk,
                    seq_num=seq_num,
                    predicted_value=predicted_value,
                    timestamp=sent_at),
                event_id=f'pred-{predictor_pk}-{seq_num}')

    asyncio.run(host._incomingPredictionsLoop(Relay(), 'relay'))


def _observation_arrives(host, value, seq_num=1):
    """An observation of the stream arrives at the host: saved, then scored
    and paid."""
    asyncio.run(host._networkProcessObservation(InboundObservation(
        stream_name=STREAM,
        nostr_pubkey=PROVIDER_PK,
        observation=DatastreamObservation(
            stream_name=STREAM,
            timestamp=T0 + seq_num * CADENCE,
            value=value,
            seq_num=seq_num),
        event_id=f'obs-{seq_num}')))


def _paid_channels(db):
    return {c['receiver_pubkey'] for c in db.get_channels_as_sender()}


# ── Tests ────────────────────────────────────────────────────────────────────

class TestAliasPubkeyPropagation:
    """BUG #1 — alias pubkey must be read from the predictions table,
    not looked up via subscriptions/get_active."""

    def test_alias_pubkey_stored_with_prediction(self, db, host):
        _add_bounty(db)
        _predictor_submits(host, 'alice_nostr', 'alice_alias', 100.0)

        rows = db.get_bounty_predictions(STREAM, PROVIDER_PK, 1)
        assert len(rows) == 1
        assert rows[0]['predictor_alias_pubkey'] == 'alice_alias'

    def test_payment_flows_to_alias_from_prediction_row(self, db, host):
        _add_bounty(db)
        _predictor_submits(host, 'alice_nostr', 'alice_alias', 100.0)

        _observation_arrives(host, 100.0)

        assert host.transfers == [('alice_alias', 1000)]
        assert _paid_channels(db) == {'alice_alias'}

    def test_predictor_with_no_alias_pubkey_gets_no_transfer(self, db, host):
        """Safety: if (somehow) the prediction row has no alias, the score
        is recorded (the leaderboard's and the dedup's) and the transfer is
        skipped rather than crashing or paying the wrong person."""
        _add_bounty(db)
        _predictor_submits(host, 'alice_nostr', None, 100.0)

        _observation_arrives(host, 100.0)

        assert host.transfers == []
        assert _paid_channels(db) == set()
        pays = db.get_bounty_payments(STREAM, PROVIDER_PK)
        assert [(p['predictor_pubkey'], p['sats_paid']) for p in pays] == [
            ('alice_nostr', 1000)]


class TestEndToEndScoringRun:
    """Full predictor → host → payment round trip with multiple predictors.

    Each error is non-zero: under inverse-error weighting an exact
    prediction takes the whole pot and the others get nothing."""

    def test_three_predictors_one_observation(self, db, host):
        _add_bounty(db, pay=1000, paid_predictors=3)
        _predictor_submits(host, 'alice', 'alice_alias', 98.0)    # err=2
        _predictor_submits(host, 'bob',   'bob_alias',   95.0)    # err=5
        _predictor_submits(host, 'carol', 'carol_alias', 80.0)    # err=20

        _observation_arrives(host, 100.0)

        # All three paid, each over a channel to its own alias
        assert len(host.transfers) == 3
        assert _paid_channels(db) == {'alice_alias', 'bob_alias', 'carol_alias'}
        # Total pot respected
        assert sum(sats for _, sats in host.transfers) <= 1000

        # bounty_payments table populated
        pays = db.get_bounty_payments(STREAM, PROVIDER_PK)
        assert len(pays) == 3
        by_pk = {p['predictor_pubkey']: p for p in pays}
        # Alice (smallest error) gets the most under inverse-error weighting
        assert by_pk['alice']['sats_paid'] >= by_pk['bob']['sats_paid']
        assert by_pk['bob']['sats_paid'] >= by_pk['carol']['sats_paid']

    def test_paid_predictors_cap_honoured(self, db, host):
        """paid_predictors=2 means only the top 2 get paid even with 3 submissions."""
        _add_bounty(db, pay=1000, paid_predictors=2)
        _predictor_submits(host, 'alice', 'alice_alias', 98.0)
        _predictor_submits(host, 'bob',   'bob_alias',   95.0)
        _predictor_submits(host, 'carol', 'carol_alias', 50.0)

        _observation_arrives(host, 100.0)

        assert len(host.transfers) == 2
        assert _paid_channels(db) == {'alice_alias', 'bob_alias'}

    def test_leaderboard_aggregates_multiple_observations(self, db, host):
        _add_bounty(db, pay=1000, paid_predictors=2)
        # seq 1
        _predictor_submits(host, 'alice', 'alice_alias', 99.0, seq_num=1)
        _predictor_submits(host, 'bob',   'bob_alias',   50.0, seq_num=1)
        _observation_arrives(host, 100.0, seq_num=1)
        # seq 2
        _predictor_submits(host, 'alice', 'alice_alias', 199.0, seq_num=2)
        _predictor_submits(host, 'bob',   'bob_alias',   150.0, seq_num=2)
        _observation_arrives(host, 200.0, seq_num=2)

        board = db.get_bounty_leaderboard(STREAM, PROVIDER_PK)
        by_pk = {r['predictor_pubkey']: r for r in board}
        # Alice was the closer both times and should outrank Bob
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

    def test_non_numeric_value_does_not_crash(self, db, host):
        _add_bounty(db)
        _predictor_submits(host, 'alice', 'alice_alias', 100.0)

        # Should silently skip rather than raise
        _observation_arrives(host, 'not-a-number')
        assert host.transfers == []
        assert db.get_bounty_payments(STREAM, PROVIDER_PK) == []


class TestJoinedBounties:
    """BUG #3 — predictor side must persist which hosts it's joined for
    each stream, so the engine knows where to DM predictions."""

    def test_join_and_lookup_by_stream(self, db):
        db.join_bounty(
            stream_name=STREAM,
            stream_provider_pubkey=PROVIDER_PK,
            host_pubkey=HOST_PK)

        joined = db.get_joined_bounties_for_stream(STREAM, PROVIDER_PK)
        assert len(joined) == 1
        assert joined[0]['host_pubkey'] == HOST_PK

    def test_join_is_idempotent(self, db):
        db.join_bounty(STREAM, PROVIDER_PK, HOST_PK)
        db.join_bounty(STREAM, PROVIDER_PK, HOST_PK)
        assert len(db.get_joined_bounties_for_stream(STREAM, PROVIDER_PK)) == 1

    def test_multiple_hosts_for_same_stream(self, db):
        db.join_bounty(STREAM, PROVIDER_PK, 'host_a')
        db.join_bounty(STREAM, PROVIDER_PK, 'host_b')
        joined = db.get_joined_bounties_for_stream(STREAM, PROVIDER_PK)
        host_pks = {r['host_pubkey'] for r in joined}
        assert host_pks == {'host_a', 'host_b'}

    def test_leave_bounty(self, db):
        db.join_bounty(STREAM, PROVIDER_PK, HOST_PK)
        db.leave_bounty(STREAM, PROVIDER_PK, HOST_PK)
        assert db.get_joined_bounties_for_stream(STREAM, PROVIDER_PK) == []

    def test_stream_not_joined_returns_empty(self, db):
        assert db.get_joined_bounties_for_stream('other', PROVIDER_PK) == []


class TestLateArrival:
    """BUG #6 — a prediction sent in time whose DM arrives after its
    observation is scored when it arrives, while the observation's pot is
    unpaid. BUG #9 — an observation is scored once: its pot is not paid
    again to whoever arrives after."""

    def test_observation_by_seq_round_trip(self, db):
        db.save_observation(
            stream_name=STREAM,
            provider_pubkey=PROVIDER_PK,
            value='100.0',
            event_id='evt-1',
            seq_num=42,
            observed_at=T0)
        row = db.get_observation_by_seq(STREAM, PROVIDER_PK, 42)
        assert row is not None
        assert row['seq_num'] == 42
        assert row['value'] == '100.0'

    def test_observation_missing_returns_none(self, db):
        assert db.get_observation_by_seq(STREAM, PROVIDER_PK, 999) is None

    def test_late_prediction_gets_scored_when_it_arrives(self, db, host):
        _add_bounty(db, pay=1000, paid_predictors=2)
        # Both observations arrive before any prediction: nobody to pay
        _observation_arrives(host, 100.0, seq_num=1)
        _observation_arrives(host, 200.0, seq_num=2)
        assert db.get_bounty_payments(STREAM, PROVIDER_PK) == []

        # Bob's prediction of seq 2, sent a minute into its window, shows up
        _predictor_submits(host, 'bob', 'bob_alias', 195.0, seq_num=2)

        pays = db.get_bounty_payments(STREAM, PROVIDER_PK)
        assert [(p['predictor_pubkey'], p['seq_num']) for p in pays] == [
            ('bob', 2)]
        assert host.transfers == [('bob_alias', 1000)]

    def test_prediction_sent_after_the_cutoff_is_not_paid(self, db, host):
        """The scorer's window, from the trigger observation to the one
        predicted, reaches it from the host's saved observations."""
        _add_bounty(db, pay=1000, paid_predictors=2)
        _observation_arrives(host, 100.0, seq_num=1)
        _observation_arrives(host, 200.0, seq_num=2)

        # Sent in the window's last tenth, when the answer is nearly known
        _predictor_submits(host, 'bob', 'bob_alias', 200.0, seq_num=2,
                           sent_at=T0 + 2 * CADENCE - 30)

        assert host.transfers == []
        assert db.get_bounty_payments(STREAM, PROVIDER_PK) == []

    def test_scored_observation_is_not_scored_again(self, db, host):
        _add_bounty(db, pay=1000, paid_predictors=2)
        # Alice predicts early, observation arrives, she gets the pot
        _predictor_submits(host, 'alice', 'alice_alias', 98.0)
        _observation_arrives(host, 100.0)
        assert host.transfers == [('alice_alias', 1000)]

        # Bob's prediction shows up after the pot is paid, and Alice's
        # again by a second relay: neither is paid from it
        _predictor_submits(host, 'bob', 'bob_alias', 99.0)
        _predictor_submits(host, 'alice', 'alice_alias', 98.0)

        assert host.transfers == [('alice_alias', 1000)]
        pays = db.get_bounty_payments(STREAM, PROVIDER_PK)
        assert {p['predictor_pubkey'] for p in pays} == {'alice'}
