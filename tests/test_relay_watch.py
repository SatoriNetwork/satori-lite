"""When a neuron leaves a relay it holds for the next one on central's list."""

from satorineuron.relay_watch import (
    RelayWatch, MOVE_AFTER, OVERDUE_MARGIN, LET_GO_FOR)

A, B, C = 'ws://relay-a', 'ws://relay-b', 'ws://relay-c'


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _watch(spread=lambda low, high: low):
    clock = Clock()
    return RelayWatch(clock=clock, spread=spread), clock


def test_a_relay_is_behind_when_another_held_relay_delivered_a_newer_value():
    watch, _ = _watch()
    watch.delivered(A, 'gold', 100)
    watch.delivered(B, 'gold', 100)
    assert not watch.behind(A, [A, B]) and not watch.behind(B, [A, B])
    watch.delivered(A, 'gold', 160)
    assert watch.behind(B, [A, B]) and not watch.behind(A, [A, B])
    watch.delivered(B, 'gold', 160)
    assert not watch.behind(B, [A, B])


def test_a_stream_a_relay_never_delivered_puts_it_behind():
    watch, _ = _watch()
    watch.delivered(A, 'gold', 100)
    watch.delivered(A, 'bitcoin', 100)
    watch.delivered(B, 'gold', 100)
    assert watch.behind(B, [A, B])


def test_a_relay_held_alone_is_never_behind_and_one_not_held_does_not_count():
    watch, _ = _watch()
    watch.delivered(A, 'gold', 100)
    watch.delivered(C, 'gold', 500)   # not held
    assert not watch.behind(A, [A])
    assert not watch.behind(A, [A, B]) and watch.newest('gold', [A, B]) == 100


def test_an_older_copy_does_not_take_a_relay_back():
    watch, _ = _watch()
    watch.delivered(A, 'gold', 160)
    watch.delivered(A, 'gold', 100)
    assert watch.newest('gold', [A]) == 160


def test_a_fault_must_last_before_the_relay_is_left():
    watch, clock = _watch(spread=lambda low, high: 90.0)
    assert not watch.due(A, 'does not answer')
    clock.now += 89
    assert not watch.due(A, 'does not answer')
    clock.now += 1
    assert watch.due(A, 'does not answer')


def test_a_relay_that_recovers_in_time_is_kept():
    watch, clock = _watch()
    assert not watch.due(A, 'does not answer')
    clock.now += 30
    watch.well(A)
    clock.now += 60
    assert not watch.due(A, 'does not answer')   # a new fault, counted from now


def test_each_neuron_picks_its_own_moment_in_the_range():
    asked = []

    def spread(low, high):
        asked.append((low, high))
        return high
    watch, clock = _watch(spread)
    watch.due(A, 'does not answer')
    assert asked == [MOVE_AFTER]
    clock.now += MOVE_AFTER[1] - 1
    assert not watch.due(A, 'does not answer')
    clock.now += 1
    assert watch.due(A, 'does not answer')


def test_with_no_relay_to_take_its_place_it_is_looked_at_again_later():
    watch, clock = _watch()
    watch.due(A, 'does not answer')
    clock.now += MOVE_AFTER[0]
    assert watch.due(A, 'does not answer')
    watch.retry_later(A)
    assert not watch.due(A, 'does not answer')
    clock.now += MOVE_AFTER[0]
    assert watch.due(A, 'does not answer')


def test_candidates_are_the_relays_not_held_in_order_those_left_lately_last():
    watch, clock = _watch()
    plan = [A, B, C, 'ws://relay-d']
    assert watch.candidates(plan, [A, B]) == [C, 'ws://relay-d']
    watch.left(C)
    assert watch.candidates(plan, [A, B]) == ['ws://relay-d', C]
    clock.now += LET_GO_FOR
    assert watch.candidates(plan, [A, B]) == [C, 'ws://relay-d']


def test_leaving_a_relay_forgets_what_it_delivered():
    watch, _ = _watch()
    watch.delivered(A, 'gold', 100)
    watch.delivered(B, 'gold', 160)
    watch.left(B)
    assert watch.newest('gold', [A, B]) == 100 and not watch.behind(A, [A, B])


def test_a_value_is_overdue_once_its_cadence_and_a_margin_have_passed_on_every_held_relay():
    watch, _ = _watch()
    watch.delivered(A, 'gold', 10_000)
    watch.delivered(B, 'gold', 10_000)
    cadences = {'gold': 3600, 'never_seen': 3600, 'no_cadence': None}
    assert watch.overdue(cadences, [A, B], 10_000 + 3600 + OVERDUE_MARGIN) == []
    assert watch.overdue(cadences, [A, B], 10_000 + 3600 + OVERDUE_MARGIN + 1) == ['gold']
    watch.delivered(B, 'gold', 13_600)    # one held relay has the new value: not overdue
    assert watch.overdue(cadences, [A, B], 10_000 + 3600 + OVERDUE_MARGIN + 1) == []


def test_every_is_true_once_in_its_time():
    watch, clock = _watch()
    assert watch.every('probe', 600) and not watch.every('probe', 600)
    assert watch.every('fill', 60)
    clock.now += 600
    assert watch.every('probe', 600)
