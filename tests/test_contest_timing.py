"""When Satori Lite polls Central and sends its predictions: in a contest's hour.

Central's contests count a prediction only if Central receives it within an
hour of the contest's open (GET /api/v1/contests/open; the daily run at 00:01
UTC writes the opens). After each poll the neuron waits for the next open
(waitForContestOpen), acts on it once, restart or not (the config keeps the
open it acted on), 0 to 15 minutes after it sees it; the day's first check
comes at 00:05 UTC plus the neuron's own 0 to 5 minutes; a Central without
contests (404) or one that keeps failing keeps the 11 hours of before. The
watchdog (checkinCheck) lets a day pass between polls. The real StartupDag
methods against a fake clock (start.time) and a fake Central (requests.get).
"""
import random
import types
from datetime import datetime, timezone

import pytest
import requests

from tests.neuron_start import start

MINUTE = 60
HOUR = 60 * MINUTE
URL = 'http://central'


def utc(text):
    return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class FakeConfig:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self):
        return dict(self.data)

    def add(self, data=None):
        self.data.update(data or {})


class Clock:
    """start.time: sleep moves time.time() on."""

    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class Response:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self.body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f'HTTP {self.status_code}')

    def json(self):
        return self.body


class Central:
    """GET /api/v1/contests/open: one stream's open contest, the latest of
    `opens` the clock has passed (none before the first), like Central's
    daily run; or `status` alone; or a connection error."""

    def __init__(self, clock, opens=(), status=200, down=False):
        self.clock = clock
        self.opens = [utc(o) for o in opens]
        self.status = status
        self.down = down
        self.gets = []

    def get(self, url, timeout=None):
        assert url == URL + '/api/v1/contests/open' and timeout
        self.gets.append(iso(self.clock.now))
        if self.down:
            raise requests.ConnectionError('Central is down')
        if self.status != 200:
            return Response(self.status, {'detail': 'Not Found'})
        opened = [o for o in self.opens if o <= self.clock.now][-1:]
        return Response(200, [
            {'stream_uuid': '98d306e5-29e0-4de3-9ce0-0a0f3fe78724', 'stream_name': 'bitcoin',
             'opened_at': iso(o), 'counts_until': iso(o + HOUR)} for o in opened])


@pytest.fixture
def neuron(monkeypatch):
    """make(now, opens, ...) -> (a neuron, its clock, Central, its config);
    neuron() again is a restarted one: the same clock, Central and config.
    The day's first check comes at 00:05 sharp here; its 0 to 5 minutes have
    their own test."""
    made = {}
    monkeypatch.setattr(random, 'uniform', lambda a, b: 0)

    def make(now=None, opens=(), last=None, status=200, down=False):
        if not made:
            made['clock'] = Clock(utc(now))
            made['central'] = Central(made['clock'], opens, status, down)
            made['config'] = FakeConfig(
                {} if last is None else {'contest last open': utc(last)})
            monkeypatch.setattr(start, 'time', made['clock'])
            monkeypatch.setattr(start, 'config', made['config'])
            monkeypatch.setattr(requests, 'get', made['central'].get)
        n = object.__new__(start.StartupDag)
        n.server = types.SimpleNamespace(url=URL)
        return n, made['clock'], made['central'], made['config']
    return make


def test_the_next_open_is_acted_on_once(neuron):
    # A poll at 14:30 after acting on today's open: tomorrow's is next.
    n, clock, central, config = neuron(
        '2026-11-26T14:30:00Z',
        opens=['2026-11-26T00:02:05Z', '2026-11-27T00:03:10Z', '2026-11-28T00:02:40Z'],
        last='2026-11-26T00:02:05Z')
    n.waitForContestOpen()
    # Checked at once, then from 00:05 UTC: tomorrow's open, acted on.
    assert central.gets == ['2026-11-26T14:30:00Z', '2026-11-27T00:05:00Z']
    assert config.data['contest last open'] == utc('2026-11-27T00:03:10Z')
    assert utc('2026-11-27T00:05:00Z') <= clock.now <= utc('2026-11-27T00:20:00Z')

    # After the poll, the same open again and again, never acted on: every
    # 5 minutes until 01:05, then the next day's open from 00:05.
    clock.now = utc('2026-11-27T00:22:00Z')
    central.gets.clear()
    n.waitForContestOpen()
    assert central.gets[:3] == ['2026-11-27T00:22:00Z', '2026-11-27T00:27:00Z', '2026-11-27T00:32:00Z']
    assert central.gets[-2:] == ['2026-11-27T01:07:00Z', '2026-11-28T00:05:00Z']
    assert config.data['contest last open'] == utc('2026-11-28T00:02:40Z')
    assert utc('2026-11-28T00:05:00Z') <= clock.now <= utc('2026-11-28T00:20:00Z')


def test_a_restart_does_not_act_again_on_one_open(neuron):
    n, clock, central, config = neuron(
        '2026-11-27T00:05:00Z',
        opens=['2026-11-27T00:03:10Z', '2026-11-28T00:02:40Z'],
        last='2026-11-26T00:02:05Z')
    n.waitForContestOpen()
    assert config.data['contest last open'] == utc('2026-11-27T00:03:10Z')

    # Restarted at 00:12 (its first poll at startup, as before, then this):
    # the open it acted on is kept, so it waits for the next one.
    restarted, clock, central, config = neuron()
    clock.now = utc('2026-11-27T00:40:00Z')
    restarted.waitForContestOpen()
    assert clock.now >= utc('2026-11-28T00:05:00Z')
    assert config.data['contest last open'] == utc('2026-11-28T00:02:40Z')


def test_an_open_past_its_hour_is_acted_on(neuron):
    # Down through today's hour: it polls and predicts anyway (counts for
    # nothing in that contest, as before), 0 to 15 minutes on.
    n, clock, central, config = neuron(
        '2026-11-27T09:00:00Z',
        opens=['2026-11-26T00:02:05Z', '2026-11-27T00:03:10Z'],
        last='2026-11-26T00:02:05Z')
    n.waitForContestOpen()
    assert central.gets == ['2026-11-27T09:00:00Z']
    assert config.data['contest last open'] == utc('2026-11-27T00:03:10Z')
    assert clock.now <= utc('2026-11-27T09:15:00Z')


def test_a_new_neuron_acts_on_the_open_it_finds(neuron):
    n, clock, central, config = neuron(
        '2026-11-27T00:30:00Z', opens=['2026-11-27T00:03:10Z'])
    n.waitForContestOpen()
    assert central.gets == ['2026-11-27T00:30:00Z']
    assert config.data['contest last open'] == utc('2026-11-27T00:03:10Z')


def test_the_days_first_check_is_each_neurons_own_moment(neuron, monkeypatch):
    # Thousands of neurons wait for the same 00:05 UTC: each adds its own 0 to
    # 5 minutes, so they do not all ask Central in one second, and keeps its
    # 5-minute steps from there.
    asked = []
    monkeypatch.setattr(random, 'uniform', lambda a, b: asked.append((a, b)) or 137)
    n, clock, central, config = neuron(
        '2026-11-26T14:30:00Z', opens=['2026-11-26T00:02:05Z', '2026-11-27T00:12:00Z'],
        last='2026-11-26T00:02:05Z')
    n.waitForContestOpen()
    assert asked == [(0, 5 * MINUTE)]
    assert central.gets == ['2026-11-26T14:30:00Z', '2026-11-27T00:07:17Z', '2026-11-27T00:12:17Z']
    assert config.data['contest last open'] == utc('2026-11-27T00:12:00Z')


@pytest.mark.parametrize('opened', ['2026-11-27T00:02:05Z', '2026-11-27T00:41:30Z'])
def test_the_spread_stays_inside_the_hour(neuron, monkeypatch, opened):
    # The latest first check and the longest spread, waiting since the evening
    # before; the run done in minutes, or a slow one.
    asked = []
    monkeypatch.setattr(random, 'uniform', lambda a, b: b)
    monkeypatch.setattr(random, 'randint', lambda a, b: asked.append((a, b)) or b)
    n, clock, central, config = neuron(
        '2026-11-26T20:00:00Z', opens=[opened], last='2026-11-26T00:02:05Z')
    n.waitForContestOpen()
    assert asked == [(0, 15 * MINUTE)]
    assert clock.sleeps[-1] == 15 * MINUTE
    # Seen by 00:10 or within 5 minutes of the open, then at most 15: inside its hour.
    seen = max(utc('2026-11-27T00:10:00Z'), utc(opened) + 5 * MINUTE)
    assert clock.now <= seen + 15 * MINUTE < utc(opened) + HOUR


def test_a_central_without_contests_keeps_the_11_hours(neuron):
    # Today's Central: no /contests/open (404).
    n, clock, central, config = neuron('2026-11-27T14:30:00Z', status=404)
    n.waitForContestOpen()
    assert central.gets == ['2026-11-27T14:30:00Z']
    assert clock.sleeps == [11 * HOUR]
    assert 'contest last open' not in config.data


@pytest.mark.parametrize('failing', [{'status': 502}, {'down': True}])
def test_a_central_that_keeps_failing_keeps_the_11_hours(neuron, failing):
    n, clock, central, config = neuron('2026-11-27T00:05:00Z', **failing)
    n.waitForContestOpen()
    assert central.gets == ['2026-11-27T00:05:00Z', '2026-11-27T00:10:00Z', '2026-11-27T00:15:00Z']
    assert clock.sleeps == [5 * MINUTE, 5 * MINUTE, 11 * HOUR]


class Stop(Exception):
    pass


@pytest.mark.parametrize('since, restarts', [(25 * HOUR, False), (49 * HOUR, True)])
def test_the_watchdog_lets_a_day_pass_between_polls(neuron, since, restarts):
    # Polls a day apart (each contest's open, give or take the spread): a day
    # and an hour without observations is no reason to restart; two days is.
    n, clock, central, config = neuron('2026-11-27T12:00:00Z')
    n.server.checkinCheck = lambda: False
    n.latestObservationTime = clock.now + 6 * HOUR - since
    restarted = []
    n.triggerRestart = lambda: restarted.append(clock.now)

    def sleepOnce(seconds):
        if clock.sleeps:
            raise Stop
        Clock.sleep(clock, seconds)
    clock.sleep = sleepOnce
    with pytest.raises(Stop):
        n.checkinCheck()
    assert bool(restarted) is restarts
