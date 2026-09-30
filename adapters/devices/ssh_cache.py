"""
Session cache shared by the device adapters: Netmiko SSH (Cisco, Nokia, Aruba)
and PyEZ NETCONF (Junos).

Sessions are checked out for exclusive use and checked back in when the call
succeeds. A Netmiko connection is a single interactive channel, so two tool
calls must never drive the same connection at once — the previous per-adapter
caches handed the same object to concurrent calls, which interleaved commands
and mixed their output.

Usage:
    _SESSIONS = SSHSessionCache("cisco")

    with _SESSIONS.session(device, lambda: ConnectHandler(**params)) as conn:
        conn.send_command("show version")
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("netcopilot.ssh_cache")


def _disconnect(conn: Any) -> None:
    try:
        if hasattr(conn, "disconnect"):
            conn.disconnect()  # Netmiko
        else:
            conn.close()       # PyEZ Device
    except Exception:
        pass


class SSHSessionCache:
    def __init__(self, name: str, ttl: float = 90.0, max_idle: int = 20,
                 alive: Optional[Callable[[Any], bool]] = None,
                 target: Optional[Callable[[str], Any]] = None) -> None:
        self.name = name
        self.ttl = ttl
        self.max_idle = max_idle
        self.alive = alive    # optional liveness check applied when a session is checked out
        self.target = target  # device -> its current address: after an inventory change, old sessions are never reused
        self._idle: dict[str, list[tuple[Any, float]]] = {}
        self._lock = threading.Lock()

    def _key(self, device: str) -> str:
        if self.target is None:
            return device
        try:
            return f"{device}@{self.target(device)}"
        except Exception:
            return device

    def checkout(self, device: str) -> Optional[Any]:
        """Remove and return a fresh-enough idle session for device, or None."""
        device = self._key(device)
        stale: list[Any] = []
        conn = None
        now = time.monotonic()
        with self._lock:
            entries = self._idle.get(device, [])
            while entries:
                candidate, last_used = entries.pop()
                if now - last_used <= self.ttl and (self.alive is None or self._is_alive(candidate)):
                    conn = candidate
                    break
                stale.append(candidate)
            if not entries:
                self._idle.pop(device, None)
        for old in stale:
            _disconnect(old)
        return conn

    def checkin(self, device: str, conn: Any) -> None:
        """Return a healthy session to the idle pool, evicting the oldest if full."""
        device = self._key(device)
        evicted: list[Any] = []
        with self._lock:
            while sum(len(v) for v in self._idle.values()) >= self.max_idle:
                oldest_dev = min(self._idle, key=lambda d: self._idle[d][0][1])
                evicted.append(self._idle[oldest_dev].pop(0)[0])
                if not self._idle[oldest_dev]:
                    del self._idle[oldest_dev]
            self._idle.setdefault(device, []).append((conn, time.monotonic()))
        for old in evicted:
            _disconnect(old)

    def _is_alive(self, conn: Any) -> bool:
        try:
            return bool(self.alive(conn))
        except Exception:
            return False

    def discard(self, device: str) -> None:
        """Close every idle session for device (e.g. after one turned out to be dead)."""
        device = self._key(device)
        with self._lock:
            entries = self._idle.pop(device, [])
        for conn, _ in entries:
            _disconnect(conn)

    def idle_count(self, device: Optional[str] = None) -> int:
        with self._lock:
            if device is not None:
                return len(self._idle.get(self._key(device), []))
            return sum(len(v) for v in self._idle.values())

    @contextmanager
    def session(self, device: str, connect: Callable[[], Any], fresh: bool = False) -> Iterator[Any]:
        """
        Yield an exclusive session. On success it goes back to the pool; on any
        exception it is disconnected so the next call starts clean.
        """
        conn = None if fresh else self.checkout(device)
        if conn is None:
            conn = connect()
            logger.debug("[%s] SSH new connection to %s", self.name, device)
        else:
            logger.debug("[%s] SSH reusing cached connection to %s", self.name, device)
        try:
            yield conn
        except BaseException:
            _disconnect(conn)
            raise
        self.checkin(device, conn)
