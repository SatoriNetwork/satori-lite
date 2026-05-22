"""Tests for bounties table in NetworkDB (Phase 1)."""

import importlib.util
import os
import tempfile
import time
import pytest

_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetworkDB = _mod.NetworkDB


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield NetworkDB(os.path.join(tmpdir, 'test.db'))


@pytest.fixture
def sample():
    return dict(
        stream_name='btc-price-usd',
        stream_provider_pubkey='aabbcc',
        host_pubkey='ddeeff',
        pay_per_obs_sats=300,
        paid_predictors=3,
        competing_predictors=5,
        scoring_metric='mae',
        scoring_params='{}',
        horizon=1,
        active=1,
        timestamp=1711234567,
    )


class TestSchema:

    def test_bounties_table_exists(self, db):
        conn = db._get_conn()
        tables = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'bounties' in tables


class TestAddBounty:

    def test_add_and_get(self, db, sample):
        db.add_bounty(**sample)
        row = db.get_bounty(
            sample['stream_name'],
            sample['stream_provider_pubkey'],
            sample['host_pubkey'])
        assert row is not None
        assert row['pay_per_obs_sats'] == 300
        assert row['scoring_metric'] == 'mae'

    def test_add_twice_upserts(self, db, sample):
        db.add_bounty(**sample)
        updated = {**sample, 'pay_per_obs_sats': 500}
        db.add_bounty(**updated)
        row = db.get_bounty(
            sample['stream_name'],
            sample['stream_provider_pubkey'],
            sample['host_pubkey'])
        assert row['pay_per_obs_sats'] == 500

    def test_get_nonexistent_returns_none(self, db):
        assert db.get_bounty('x', 'y', 'z') is None


class TestGetAllBounties:

    def test_empty(self, db):
        assert db.get_all_bounties() == []

    def test_returns_all(self, db, sample):
        db.add_bounty(**sample)
        second = {**sample, 'stream_name': 'eth-price', 'host_pubkey': 'ff0011'}
        db.add_bounty(**second)
        rows = db.get_all_bounties()
        assert len(rows) == 2

    def test_active_only(self, db, sample):
        db.add_bounty(**sample)
        inactive = {**sample, 'stream_name': 'eth-price', 'host_pubkey': 'ff0011', 'active': 0}
        db.add_bounty(**inactive)
        rows = db.get_all_bounties(active_only=True)
        assert len(rows) == 1
        assert rows[0]['stream_name'] == 'btc-price-usd'


class TestCloseBounty:

    def test_close_sets_inactive(self, db, sample):
        db.add_bounty(**sample)
        db.close_bounty(
            sample['stream_name'],
            sample['stream_provider_pubkey'],
            sample['host_pubkey'])
        row = db.get_bounty(
            sample['stream_name'],
            sample['stream_provider_pubkey'],
            sample['host_pubkey'])
        assert row['active'] == 0

    def test_close_nonexistent_does_not_raise(self, db):
        db.close_bounty('x', 'y', 'z')  # should not raise


class TestMyBounties:

    def test_get_hosted_by(self, db, sample):
        db.add_bounty(**sample)
        other = {**sample, 'stream_name': 'eth-price', 'host_pubkey': 'other'}
        db.add_bounty(**other)
        rows = db.get_bounties_hosted_by('ddeeff')
        assert len(rows) == 1
        assert rows[0]['host_pubkey'] == 'ddeeff'
