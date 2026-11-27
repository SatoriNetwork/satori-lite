"""Watching the relays a neuron holds from central's relay plan.

Central's stream publisher writes every value to every relay, so the relays a
neuron holds should all deliver the same values. This keeps what each relay
delivered and says when one should be left for the next relay on the list:

- it does not answer;
- it is behind another held relay on a stream (connected, but not being fed);
- a value is overdue on every held relay: the next relay is tried once, and
  taken if it has a newer value. If it has not, the publisher is late and
  nothing moves.

A relay is left only when its fault has lasted a moment the neuron picks for
itself between MOVE_AFTER's two ends, so a short drop moves nobody and the
neurons of a failed relay do not all arrive at the others in the same second.

Only the bookkeeping is here; start.py does the connecting.
"""

import random
import time

WATCH_EVERY = 15.0          # seconds between looks at the held relays
MOVE_AFTER = (60.0, 120.0)  # a fault must last this long, a moment in the range per neuron
OVERDUE_MARGIN = 180        # seconds past a stream's cadence before its value counts as overdue
PROBE_EVERY = 600.0         # while a value is overdue, another relay is tried this often
PROBE_WAIT = 10.0           # how long a tried relay gets to deliver what it holds
LET_GO_FOR = 3600.0         # a relay that was left is tried last for this long
FILL_EVERY = 60.0           # an empty place is tried to be filled this often

_NEVER = float('-inf')


class RelayWatch:

    def __init__(self, clock=time.monotonic, spread=random.uniform):
        self._clock = clock
        self._spread = spread
        self.values: dict = {}   # relay_url -> {stream_name: time of the newest value it delivered}
        self.faults: dict = {}   # relay_url -> (reason, the moment to leave it)
        self.let_go: dict = {}   # relay_url -> when it was left
        self.probe = None        # (relay_url, when) while another relay is being tried
        self.probes = 0
        self._last: dict = {}

    def now(self) -> float:
        return self._clock()

    def delivered(self, relay_url: str, stream_name: str, value_time: int) -> None:
        seen = self.values.setdefault(relay_url, {})
        if value_time > seen.get(stream_name, 0):
            seen[stream_name] = value_time

    def newest(self, stream_name: str, relays) -> int:
        """The time of the newest value of a stream these relays delivered."""
        return max((self.values.get(r, {}).get(stream_name, 0) for r in relays), default=0)

    def behind(self, relay_url: str, held) -> bool:
        """Another held relay delivered a newer value of some stream."""
        mine = self.values.get(relay_url, {})
        streams = {s for r in held for s in self.values.get(r, {})}
        return any(mine.get(s, 0) < self.newest(s, held) for s in streams)

    def due(self, relay_url: str, reason: str) -> bool:
        """Note a relay's fault. True once it has lasted long enough to leave the relay."""
        now = self._clock()
        if relay_url not in self.faults:
            self.faults[relay_url] = (reason, now + self._spread(*MOVE_AFTER))
        return now >= self.faults[relay_url][1]

    def well(self, relay_url: str) -> None:
        self.faults.pop(relay_url, None)

    def retry_later(self, relay_url: str) -> None:
        """No other relay could take its place: look again in a while."""
        reason = self.faults.get(relay_url, ('', 0))[0]
        self.faults[relay_url] = (reason, self._clock() + MOVE_AFTER[0])

    def left(self, relay_url: str) -> None:
        self.forget(relay_url)
        self.let_go[relay_url] = self._clock()

    def forget(self, relay_url: str) -> None:
        self.faults.pop(relay_url, None)
        self.values.pop(relay_url, None)

    def candidates(self, plan, held) -> list:
        """The relays of the plan not held, in order; those left lately go last."""
        now = self._clock()
        free = [u for u in plan if u not in held]
        return sorted(free, key=lambda u: now - self.let_go.get(u, _NEVER) < LET_GO_FOR)

    def overdue(self, cadences: dict, held, wall_now: float) -> list:
        """The streams whose next value should have come by now and has not, on any held relay."""
        return sorted(
            s for s, cadence in cadences.items()
            if cadence and 0 < self.newest(s, held) < wall_now - cadence - OVERDUE_MARGIN)

    def every(self, what: str, seconds: float) -> bool:
        """True at most once in `seconds` for `what`."""
        now = self._clock()
        if now - self._last.get(what, _NEVER) < seconds:
            return False
        self._last[what] = now
        return True
