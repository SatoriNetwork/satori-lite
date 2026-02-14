"""Continuous stream health monitoring.

Monitors datastream health over time, alerting when streams go stale
or become active again.
"""
import asyncio
import time
from typing import Optional, Callable, Awaitable
from dataclasses import dataclass
from enum import Enum

from ..models import DatastreamMetadata
from ..client import SatoriNostr


class StreamHealth(Enum):
    """Stream health status."""
    ACTIVE = "active"          # Publishing within expected cadence
    STALE = "stale"            # No observations beyond 2x cadence
    DEAD = "dead"              # No observations beyond 5x cadence
    UNKNOWN = "unknown"        # No observations ever seen


@dataclass
class StreamStatus:
    """Status of a monitored stream."""
    stream_name: str
    nostr_pubkey: str
    metadata: DatastreamMetadata
    health: StreamHealth = StreamHealth.UNKNOWN
    last_observation_time: Optional[int] = None
    last_check_time: int = 0
    consecutive_stale_checks: int = 0


class StreamHealthMonitor:
    """Continuously monitor datastream health.

    Features:
    - Periodic health checks for streams
    - Track stream state (active, stale, dead)
    - Alert callbacks when health changes
    - Detect stream revival after being stale

    Example:
        >>> def on_stale(stream_name: str):
        ...     print(f"Stream {stream_name} went stale!")
        >>>
        >>> def on_active(stream_name: str):
        ...     print(f"Stream {stream_name} is active again!")
        >>>
        >>> monitor = StreamHealthMonitor(
        ...     client=nostr_client,
        ...     check_interval=60,
        ...     on_stream_stale=on_stale,
        ...     on_stream_active=on_active
        ... )
        >>>
        >>> # Add streams to monitor
        >>> await monitor.add_stream(metadata)
        >>>
        >>> # Start monitoring
        >>> await monitor.start()
        >>>
        >>> # Get current status
        >>> status = monitor.get_stream_status("bitcoin-price")
        >>> print(f"Health: {status.health}")
        >>>
        >>> await monitor.stop()
    """

    def __init__(
        self,
        client: SatoriNostr,
        check_interval: int = 60,
        on_stream_stale: Optional[Callable[[str], Awaitable[None]]] = None,
        on_stream_active: Optional[Callable[[str], Awaitable[None]]] = None,
        on_stream_dead: Optional[Callable[[str], Awaitable[None]]] = None,
    ):
        """Initialize stream health monitor.

        Args:
            client: SatoriNostr client to query relay
            check_interval: Seconds between health checks
            on_stream_stale: Callback when stream goes stale
            on_stream_active: Callback when stream becomes active again
            on_stream_dead: Callback when stream is considered dead
        """
        self.client = client
        self.check_interval = check_interval
        self.on_stream_stale = on_stream_stale
        self.on_stream_active = on_stream_active
        self.on_stream_dead = on_stream_dead

        # Tracked streams
        self._streams: dict[str, StreamStatus] = {}  # stream_name -> status

        # Background task
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = {
            "checks_performed": 0,
            "streams_monitored": 0,
            "state_changes": 0,
        }

    async def start(self):
        """Start health monitoring."""
        if self._running:
            raise RuntimeError("Monitor already running")

        if not self.client.is_running():
            raise RuntimeError("Client must be running before starting monitor")

        self._running = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """Stop health monitoring."""
        if not self._running:
            return

        self._running = False

        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

    async def add_stream(self, metadata: DatastreamMetadata):
        """Add a stream to monitor.

        Args:
            metadata: Stream metadata
        """
        stream_key = metadata.stream_name

        if stream_key not in self._streams:
            self._streams[stream_key] = StreamStatus(
                stream_name=metadata.stream_name,
                nostr_pubkey=metadata.nostr_pubkey,
                metadata=metadata
            )
            self._stats["streams_monitored"] = len(self._streams)

            # Do initial health check
            await self._check_stream_health(stream_key)

    def remove_stream(self, stream_name: str):
        """Remove a stream from monitoring.

        Args:
            stream_name: Stream name to remove
        """
        if stream_name in self._streams:
            del self._streams[stream_name]
            self._stats["streams_monitored"] = len(self._streams)

    def get_stream_status(self, stream_name: str) -> Optional[StreamStatus]:
        """Get current status of a stream.

        Args:
            stream_name: Stream name

        Returns:
            StreamStatus or None if not monitored
        """
        return self._streams.get(stream_name)

    def get_all_streams(self) -> list[StreamStatus]:
        """Get status of all monitored streams.

        Returns:
            List of StreamStatus objects
        """
        return list(self._streams.values())

    def get_streams_by_health(self, health: StreamHealth) -> list[StreamStatus]:
        """Get streams with specific health status.

        Args:
            health: StreamHealth to filter by

        Returns:
            List of matching streams
        """
        return [s for s in self._streams.values() if s.health == health]

    def get_statistics(self) -> dict[str, int]:
        """Get monitoring statistics.

        Returns:
            Dictionary with stats
        """
        stats = self._stats.copy()
        stats["active_streams"] = len(self.get_streams_by_health(StreamHealth.ACTIVE))
        stats["stale_streams"] = len(self.get_streams_by_health(StreamHealth.STALE))
        stats["dead_streams"] = len(self.get_streams_by_health(StreamHealth.DEAD))
        return stats

    async def _monitor_loop(self):
        """Background task to continuously check stream health."""
        while self._running:
            try:
                # Check all streams
                for stream_name in list(self._streams.keys()):
                    await self._check_stream_health(stream_name)

                # Sleep until next check
                await asyncio.sleep(self.check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"Error in monitor loop: {e}")
                await asyncio.sleep(self.check_interval)

    async def _check_stream_health(self, stream_name: str):
        """Check health of a single stream.

        Args:
            stream_name: Stream to check
        """
        status = self._streams.get(stream_name)
        if not status:
            return

        try:
            # Query relay for last observation time
            last_time = await self.client.get_last_observation_time(stream_name)

            # Update status
            status.last_observation_time = last_time
            status.last_check_time = int(time.time())
            self._stats["checks_performed"] += 1

            # Determine health
            old_health = status.health
            new_health = self._calculate_health(status, last_time)

            # Update health
            status.health = new_health

            # Track consecutive stale checks
            if new_health in (StreamHealth.STALE, StreamHealth.DEAD):
                status.consecutive_stale_checks += 1
            else:
                status.consecutive_stale_checks = 0

            # Fire callbacks if health changed
            if old_health != new_health:
                self._stats["state_changes"] += 1
                await self._handle_health_change(stream_name, old_health, new_health)

        except Exception as e:
            print(f"Error checking stream {stream_name}: {e}")

    def _calculate_health(
        self,
        status: StreamStatus,
        last_observation_time: Optional[int]
    ) -> StreamHealth:
        """Calculate stream health based on last observation time.

        Args:
            status: Stream status
            last_observation_time: Unix timestamp or None

        Returns:
            StreamHealth
        """
        if last_observation_time is None:
            return StreamHealth.UNKNOWN

        metadata = status.metadata
        now = int(time.time())
        age = now - last_observation_time

        # No cadence = check if updated in last 24 hours
        if metadata.cadence_seconds is None:
            if age < 86400:  # 1 day
                return StreamHealth.ACTIVE
            elif age < 432000:  # 5 days
                return StreamHealth.STALE
            else:
                return StreamHealth.DEAD

        # Regular cadence
        cadence = metadata.cadence_seconds

        # Active: within 2x cadence
        if age < cadence * 2:
            return StreamHealth.ACTIVE

        # Stale: within 5x cadence
        elif age < cadence * 5:
            return StreamHealth.STALE

        # Dead: beyond 5x cadence
        else:
            return StreamHealth.DEAD

    async def _handle_health_change(
        self,
        stream_name: str,
        old_health: StreamHealth,
        new_health: StreamHealth
    ):
        """Handle stream health state change.

        Args:
            stream_name: Stream name
            old_health: Previous health status
            new_health: New health status
        """
        # Active -> Stale
        if old_health == StreamHealth.ACTIVE and new_health == StreamHealth.STALE:
            if self.on_stream_stale:
                try:
                    await self.on_stream_stale(stream_name)
                except Exception as e:
                    print(f"Error in on_stream_stale callback: {e}")

        # Stale/Dead -> Active (revival)
        elif old_health in (StreamHealth.STALE, StreamHealth.DEAD) and new_health == StreamHealth.ACTIVE:
            if self.on_stream_active:
                try:
                    await self.on_stream_active(stream_name)
                except Exception as e:
                    print(f"Error in on_stream_active callback: {e}")

        # Stale -> Dead
        elif old_health == StreamHealth.STALE and new_health == StreamHealth.DEAD:
            if self.on_stream_dead:
                try:
                    await self.on_stream_dead(stream_name)
                except Exception as e:
                    print(f"Error in on_stream_dead callback: {e}")
