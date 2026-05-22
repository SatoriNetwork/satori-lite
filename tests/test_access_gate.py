"""Tests for approval-gated private streams (Phase 5).

Tests cover:
- AccessRequest / InboundAccessRequest models
- approval_required flag on DatastreamMetadata
- KIND_ACCESS_REQUEST constant
- NetworkDB access_requests and approved_subscribers tables
- Full approve/reject/revoke lifecycle
"""

import importlib.util
import json
import os
import tempfile
import time
import pytest

from satorilib.satori_nostr.models import (
    AccessRequest,
    InboundAccessRequest,
    DatastreamMetadata,
    KIND_ACCESS_REQUEST,
)

_spec = importlib.util.spec_from_file_location(
    'network_db',
    os.path.join(os.path.dirname(__file__),
                 '..', 'neuron-lite', 'satorineuron', 'network_db.py'))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
NetworkDB = _mod.NetworkDB


# ── Fixtures ──────────────────────────────────────────────────

@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield NetworkDB(os.path.join(tmpdir, 'test.db'))


# ── Model tests ──────────────────────────────────────────────

class TestAccessRequestModel:

    def test_create_and_serialize(self):
        req = AccessRequest(
            stream_name='btc-price',
            requester_pubkey='aabb11',
            producer_pubkey='ccdd22',
            message='Please let me predict',
            timestamp=1711234567,
        )
        assert req.stream_name == 'btc-price'
        assert req.requester_pubkey == 'aabb11'
        assert req.producer_pubkey == 'ccdd22'
        assert req.message == 'Please let me predict'

        d = req.to_dict()
        assert d['stream_name'] == 'btc-price'
        assert d['producer_pubkey'] == 'ccdd22'

    def test_json_roundtrip(self):
        req = AccessRequest(
            stream_name='weather-nyc',
            requester_pubkey='sub1',
            producer_pubkey='prod1',
            timestamp=1000,
        )
        j = req.to_json()
        parsed = AccessRequest.from_json(j)
        assert parsed.stream_name == 'weather-nyc'
        assert parsed.requester_pubkey == 'sub1'
        assert parsed.timestamp == 1000

    def test_default_message_empty(self):
        req = AccessRequest(
            stream_name='s', requester_pubkey='r', producer_pubkey='p')
        assert req.message == ''
        assert req.timestamp == 0

    def test_inbound_wrapper(self):
        req = AccessRequest(
            stream_name='s', requester_pubkey='r', producer_pubkey='p')
        inbound = InboundAccessRequest(
            access_request=req, event_id='abc123')
        assert inbound.access_request.stream_name == 's'
        assert inbound.event_id == 'abc123'
        assert inbound.raw_event is None


class TestKindConstant:

    def test_kind_access_request_value(self):
        assert KIND_ACCESS_REQUEST == 34609


class TestApprovalRequiredFlag:

    def test_default_false(self):
        meta = DatastreamMetadata(
            stream_name='s', nostr_pubkey='k', name='n', description='d',
            encrypted=False, price_per_obs=0, created_at=0,
            cadence_seconds=None, tags=[])
        assert meta.approval_required is False

    def test_set_true(self):
        meta = DatastreamMetadata(
            stream_name='s', nostr_pubkey='k', name='n', description='d',
            encrypted=True, price_per_obs=10, created_at=0,
            cadence_seconds=3600, tags=[], approval_required=True)
        assert meta.approval_required is True

    def test_serializes_in_json(self):
        meta = DatastreamMetadata(
            stream_name='s', nostr_pubkey='k', name='n', description='d',
            encrypted=False, price_per_obs=0, created_at=0,
            cadence_seconds=None, tags=[], approval_required=True)
        j = json.loads(meta.to_json())
        assert j['approval_required'] is True

    def test_deserializes_from_json(self):
        meta = DatastreamMetadata(
            stream_name='s', nostr_pubkey='k', name='n', description='d',
            encrypted=False, price_per_obs=0, created_at=0,
            cadence_seconds=None, tags=[], approval_required=True)
        restored = DatastreamMetadata.from_json(meta.to_json())
        assert restored.approval_required is True


# ── Database schema tests ─────────────────────────────────────

class TestSchema:

    def test_access_requests_table_exists(self, db):
        conn = db._get_conn()
        tables = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'access_requests' in tables

    def test_approved_subscribers_table_exists(self, db):
        conn = db._get_conn()
        tables = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert 'approved_subscribers' in tables

    def test_publications_approval_required_column(self, db):
        """The approval_required column should exist on publications."""
        conn = db._get_conn()
        row = conn.execute(
            "SELECT approval_required FROM publications LIMIT 1"
        ).fetchone()
        # No rows, but the query succeeds = column exists
        assert row is None


# ── Access request CRUD tests ─────────────────────────────────

class TestAddAccessRequest:

    def test_add_and_get(self, db):
        db.add_access_request(
            stream_name='btc-price',
            requester_pubkey='sub1',
            message='hello',
            requested_at=1000,
        )
        reqs = db.get_access_requests('btc-price')
        assert len(reqs) == 1
        assert reqs[0]['requester_pubkey'] == 'sub1'
        assert reqs[0]['message'] == 'hello'
        assert reqs[0]['status'] == 'pending'

    def test_re_request_resets_to_pending(self, db):
        db.add_access_request('s', 'r', requested_at=100)
        db.reject_access_request('s', 'r')
        # Re-request after rejection
        db.add_access_request('s', 'r', message='please?', requested_at=200)
        reqs = db.get_access_requests('s')
        assert len(reqs) == 1
        assert reqs[0]['status'] == 'pending'
        assert reqs[0]['message'] == 'please?'

    def test_filter_by_status(self, db):
        db.add_access_request('s', 'r1', requested_at=100)
        db.add_access_request('s', 'r2', requested_at=200)
        db.reject_access_request('s', 'r1')
        pending = db.get_access_requests('s', status='pending')
        assert len(pending) == 1
        assert pending[0]['requester_pubkey'] == 'r2'

    def test_get_all_pending(self, db):
        db.add_access_request('s1', 'r1', requested_at=100)
        db.add_access_request('s2', 'r2', requested_at=200)
        db.reject_access_request('s1', 'r1')
        pending = db.get_all_pending_access_requests()
        assert len(pending) == 1
        assert pending[0]['stream_name'] == 's2'


# ── Approve / reject / revoke tests ──────────────────────────

class TestApproveRejectRevoke:

    def test_approve_adds_to_approved_list(self, db):
        db.add_access_request('s', 'r1', requested_at=100)
        db.approve_access_request('s', 'r1')
        assert db.is_subscriber_approved('s', 'r1') is True
        approved = db.get_approved_subscribers('s')
        assert len(approved) == 1
        assert approved[0]['subscriber_pubkey'] == 'r1'

    def test_approve_updates_request_status(self, db):
        db.add_access_request('s', 'r1', requested_at=100)
        db.approve_access_request('s', 'r1')
        reqs = db.get_access_requests('s', status='approved')
        assert len(reqs) == 1
        assert reqs[0]['resolved_at'] is not None

    def test_reject_sets_status(self, db):
        db.add_access_request('s', 'r1', requested_at=100)
        db.reject_access_request('s', 'r1')
        reqs = db.get_access_requests('s', status='rejected')
        assert len(reqs) == 1

    def test_reject_does_not_add_to_approved(self, db):
        db.add_access_request('s', 'r1', requested_at=100)
        db.reject_access_request('s', 'r1')
        assert db.is_subscriber_approved('s', 'r1') is False

    def test_revoke_removes_from_approved(self, db):
        db.add_access_request('s', 'r1', requested_at=100)
        db.approve_access_request('s', 'r1')
        assert db.is_subscriber_approved('s', 'r1') is True
        db.revoke_subscriber('s', 'r1')
        assert db.is_subscriber_approved('s', 'r1') is False

    def test_revoke_sets_status(self, db):
        db.add_access_request('s', 'r1', requested_at=100)
        db.approve_access_request('s', 'r1')
        db.revoke_subscriber('s', 'r1')
        reqs = db.get_access_requests('s', status='revoked')
        assert len(reqs) == 1

    def test_not_approved_by_default(self, db):
        assert db.is_subscriber_approved('s', 'unknown') is False

    def test_multiple_streams_independent(self, db):
        db.add_access_request('s1', 'r1', requested_at=100)
        db.add_access_request('s2', 'r1', requested_at=100)
        db.approve_access_request('s1', 'r1')
        assert db.is_subscriber_approved('s1', 'r1') is True
        assert db.is_subscriber_approved('s2', 'r1') is False


class TestStreamApprovalRequired:

    def test_not_required_by_default(self, db):
        assert db.is_stream_approval_required('nonexistent') is False

    def test_required_when_set(self, db):
        conn = db._get_conn()
        conn.execute("""
            INSERT INTO publications
                (stream_name, price_per_obs, encrypted, active,
                 created_at, last_seq_num, approval_required)
            VALUES ('private-stream', 10, 1, 1, ?, 0, 1)
        """, (int(time.time()),))
        conn.commit()
        assert db.is_stream_approval_required('private-stream') is True

    def test_not_required_when_unset(self, db):
        conn = db._get_conn()
        conn.execute("""
            INSERT INTO publications
                (stream_name, price_per_obs, encrypted, active,
                 created_at, last_seq_num, approval_required)
            VALUES ('public-stream', 0, 0, 1, ?, 0, 0)
        """, (int(time.time()),))
        conn.commit()
        assert db.is_stream_approval_required('public-stream') is False
