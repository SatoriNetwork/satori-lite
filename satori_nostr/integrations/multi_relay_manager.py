"""Multi-relay coordination with deduplication and failover.

Handles connecting to multiple Nostr relays, deduplicating events,
and switching relays when connections fail.
"""
import asyncio
import time
from typing import Optional, Callable, Awaitable
from collections import defaultdict
from dataclasses import dataclass

from ..dedupe import DedupeCache


@dataclass
class RelayStatus:
    """Status of a single relay connection."""
    url: str
    connected: bool = False
    last_event_time: int = 0  # Unix timestamp of last event received
    error_count: int = 0
    last_error: Optional[str] = None
    connection_time: int = 0  # When we connected


class MultiRelayManager:
    """Manages connections to multiple Nostr relays with failover.

    Features:
    - Connect to multiple relays simultaneously
    - Deduplicate events across relays (by event ID)
    - Track relay health (connection status, last event time)
    - Automatic failover to working relays
    - Circuit breaker pattern for failing relays

    Example:
        >>> manager = MultiRelayManager(
        ...     relay_urls=["wss://relay1.com", "wss://relay2.com"],
        ...     min_active_relays=1
        ... )
        >>>
        >>> await manager.start()
        >>>
        >>> # Check relay health
        >>> healthy = manager.get_healthy_relays()
        >>> print(f"Connected to {len(healthy)} relays")
        >>>
        >>> # Get deduplicated event
        >>> if not manager.is_duplicate(event_id):
        ...     process_event(event)
        >>>
        >>> await manager.stop()
    """

    def __init__(
        self,
        relay_urls: list[str],
        min_active_relays: int = 1,
        max_error_count: int = 5,
        reconnect_delay: int = 30,
    ):
        """Initialize multi-relay manager.

        Args:
            relay_urls: List of relay URLs to connect to
            min_active_relays: Minimum number of active relays to maintain
            max_error_count: Max errors before marking relay as unhealthy
            reconnect_delay: Seconds to wait before reconnecting failed relay
        """
        self.relay_urls = relay_urls
        self.min_active_relays = min_active_relays
        self.max_error_count = max_error_count
        self.reconnect_delay = reconnect_delay

        # Relay status tracking
        self._relay_status: dict[str, RelayStatus] = {
            url: RelayStatus(url=url)
            for url in relay_urls
        }

        # Event deduplication
        self._dedupe = DedupeCache()

        # Statistics
        self._stats = {
            "events_received": 0,
            "events_deduplicated": 0,
            "relay_reconnects": 0,
            "relay_failures": 0,
        }

        # Background tasks
        self._running = False
        self._health_check_task: Optional[asyncio.Task] = None

    async def start(self):
        """Start the multi-relay manager."""
        if self._running:
            raise RuntimeError("Manager already running")

        self._running = True

        # Start health monitoring
        self._health_check_task = asyncio.create_task(self._health_check_loop())

    async def stop(self):
        """Stop the multi-relay manager."""
        if not self._running:
            return

        self._running = False

        # Cancel health check
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

    def mark_relay_connected(self, relay_url: str):
        """Mark a relay as connected.

        Args:
            relay_url: Relay URL
        """
        if relay_url in self._relay_status:
            status = self._relay_status[relay_url]
            status.connected = True
            status.error_count = 0
            status.connection_time = int(time.time())

    def mark_relay_disconnected(self, relay_url: str, error: str = ""):
        """Mark a relay as disconnected.

        Args:
            relay_url: Relay URL
            error: Optional error message
        """
        if relay_url in self._relay_status:
            status = self._relay_status[relay_url]
            status.connected = False
            status.error_count += 1
            status.last_error = error
            self._stats["relay_failures"] += 1

    def mark_event_received(self, relay_url: str, event_id: str) -> bool:
        """Mark that an event was received from a relay.

        Args:
            relay_url: Relay URL
            event_id: Nostr event ID

        Returns:
            True if event is new (not duplicate), False if duplicate
        """
        # Update relay status
        if relay_url in self._relay_status:
            self._relay_status[relay_url].last_event_time = int(time.time())

        # Check for duplicate
        if self._dedupe.contains(event_id):
            self._stats["events_deduplicated"] += 1
            return False

        # New event
        self._dedupe.add(event_id)
        self._stats["events_received"] += 1
        return True

    def is_duplicate(self, event_id: str) -> bool:
        """Check if event ID has been seen before.

        Args:
            event_id: Nostr event ID

        Returns:
            True if duplicate, False if new
        """
        return self._dedupe.contains(event_id)

    def add_event(self, event_id: str):
        """Add event ID to dedupe cache.

        Args:
            event_id: Nostr event ID
        """
        self._dedupe.add(event_id)

    def get_relay_status(self, relay_url: str) -> Optional[RelayStatus]:
        """Get status of a specific relay.

        Args:
            relay_url: Relay URL

        Returns:
            RelayStatus or None if relay not tracked
        """
        return self._relay_status.get(relay_url)

    def get_all_relay_status(self) -> dict[str, RelayStatus]:
        """Get status of all relays.

        Returns:
            Dictionary mapping relay URLs to RelayStatus
        """
        return self._relay_status.copy()

    def get_healthy_relays(self) -> list[str]:
        """Get list of currently healthy relay URLs.

        A relay is healthy if:
        - Connected
        - Error count below threshold

        Returns:
            List of healthy relay URLs
        """
        healthy = []
        for url, status in self._relay_status.items():
            if status.connected and status.error_count < self.max_error_count:
                healthy.append(url)
        return healthy

    def get_unhealthy_relays(self) -> list[str]:
        """Get list of currently unhealthy relay URLs.

        Returns:
            List of unhealthy relay URLs
        """
        unhealthy = []
        for url, status in self._relay_status.items():
            if not status.connected or status.error_count >= self.max_error_count:
                unhealthy.append(url)
        return unhealthy

    def needs_more_relays(self) -> bool:
        """Check if we need to connect to more relays.

        Returns:
            True if connected relays < min_active_relays
        """
        healthy_count = len(self.get_healthy_relays())
        return healthy_count < self.min_active_relays

    def get_best_relay(self) -> Optional[str]:
        """Get the best relay to use (lowest error count, most recent events).

        Returns:
            Relay URL or None if no healthy relays
        """
        healthy = self.get_healthy_relays()
        if not healthy:
            return None

        # Sort by error count (asc), then last event time (desc)
        def score_relay(url: str) -> tuple:
            status = self._relay_status[url]
            return (status.error_count, -status.last_event_time)

        return min(healthy, key=score_relay)

    def get_statistics(self) -> dict[str, int]:
        """Get manager statistics.

        Returns:
            Dictionary with stats:
            - events_received: Total unique events
            - events_deduplicated: Duplicate events filtered
            - relay_reconnects: Number of reconnection attempts
            - relay_failures: Number of relay failures
            - healthy_relays: Current healthy relay count
            - unhealthy_relays: Current unhealthy relay count
        """
        stats = self._stats.copy()
        stats["healthy_relays"] = len(self.get_healthy_relays())
        stats["unhealthy_relays"] = len(self.get_unhealthy_relays())
        return stats

    async def _health_check_loop(self):
        """Background task to check relay health and trigger reconnects."""
        while self._running:
            try:
                # Check each relay
                now = int(time.time())

                for url, status in self._relay_status.items():
                    # If disconnected and enough time has passed, try reconnect
                    if not status.connected:
                        time_since_disconnect = now - status.connection_time
                        if time_since_disconnect >= self.reconnect_delay:
                            # This is just tracking - actual reconnection
                            # happens in the client using this manager
                            self._stats["relay_reconnects"] += 1

                    # Check for stale relays (no events in a while)
                    if status.connected and status.last_event_time > 0:
                        time_since_event = now - status.last_event_time
                        # If no events in 5 minutes, mark as suspicious
                        if time_since_event > 300:
                            # Don't disconnect, but flag it
                            pass  # Could add warning here

                # Sleep before next check
                await asyncio.sleep(10)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in health check loop: {e}")
                await asyncio.sleep(10)

    def should_reconnect_relay(self, relay_url: str) -> bool:
        """Check if a relay should attempt reconnection.

        Args:
            relay_url: Relay URL

        Returns:
            True if relay should reconnect
        """
        status = self.get_relay_status(relay_url)
        if not status:
            return False

        # Don't reconnect if already connected
        if status.connected:
            return False

        # Don't reconnect if too many errors
        if status.error_count >= self.max_error_count:
            return False

        # Check if enough time has passed
        now = int(time.time())
        time_since_disconnect = now - status.connection_time
        return time_since_disconnect >= self.reconnect_delay
