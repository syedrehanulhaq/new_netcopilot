"""
Device connection pool with per-device concurrency limits and circuit breaker.

Circuit Breaker states:
  CLOSED    — normal operation
  OPEN      — failing fast (device unreachable), no calls attempted
  HALF_OPEN — one probe allowed to test recovery

Usage:
    pool = DevicePool()

    async with pool.acquire("sandbox-iosxr-1") as ctx:
        # ctx is just a guard — actual connection made by the tool
        result = do_something()
        pool.record_success("sandbox-iosxr-1")

    # On failure:
    pool.record_failure("sandbox-iosxr-1")
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from contextlib import asynccontextmanager
from typing import Optional

logger = logging.getLogger("netcopilot.device_pool")

# Per-device limits
MAX_CONCURRENT_PER_DEVICE = int(3)
CIRCUIT_FAILURE_THRESHOLD = int(3)   # failures before opening circuit
CIRCUIT_OPEN_SECONDS = float(60)      # seconds circuit stays open
CIRCUIT_HALF_OPEN_TIMEOUT = float(30) # seconds to wait in half-open
MAX_TRACKED_DEVICES = 1000            # cap pool dict to prevent memory exhaustion


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _DeviceState:
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(MAX_CONCURRENT_PER_DEVICE))
    circuit: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    opened_at: float = 0.0
    last_failure_reason: str = ""


class DevicePool:
    """
    Manages per-device concurrency and circuit breaker state.
    One instance shared across the application (injected into adapters).
    """

    def __init__(self) -> None:
        self._devices: dict[str, _DeviceState] = {}
        self._lock = asyncio.Lock()

    async def _get_state(self, device: str) -> _DeviceState:
        async with self._lock:
            if device not in self._devices:
                if len(self._devices) >= MAX_TRACKED_DEVICES:
                    raise CircuitOpenError(
                        f"Device pool at capacity ({MAX_TRACKED_DEVICES} devices). "
                        "Remove stale devices or increase MAX_TRACKED_DEVICES."
                    )
                self._devices[device] = _DeviceState()
            return self._devices[device]

    def circuit_state(self, device: str) -> CircuitState:
        state = self._devices.get(device)
        return state.circuit if state else CircuitState.CLOSED

    def is_open(self, device: str) -> bool:
        """Returns True if circuit is OPEN (calls should be rejected fast)."""
        state = self._devices.get(device)
        if not state or state.circuit == CircuitState.CLOSED:
            return False
        if state.circuit == CircuitState.OPEN:
            elapsed = time.monotonic() - state.opened_at
            if elapsed >= CIRCUIT_OPEN_SECONDS:
                state.circuit = CircuitState.HALF_OPEN
                logger.info("Circuit HALF_OPEN for '%s' (will probe)", device)
                return False  # allow one probe
            return True
        return False  # HALF_OPEN — let probe through

    def record_success(self, device: str) -> None:
        state = self._devices.get(device)
        if not state:
            return
        if state.circuit in (CircuitState.OPEN, CircuitState.HALF_OPEN):
            logger.info("Circuit CLOSED for '%s' after successful probe", device)
        state.circuit = CircuitState.CLOSED
        state.failure_count = 0
        state.last_failure_reason = ""

    def force_probe(self, device: str) -> None:
        """Force circuit to HALF_OPEN so the next call gets a fresh attempt.
        Call this before alert investigations to bypass a stale OPEN state."""
        state = self._devices.get(device)
        if state and state.circuit == CircuitState.OPEN:
            state.circuit = CircuitState.HALF_OPEN
            logger.info("Circuit force-probed for '%s' — allowing one attempt", device)

    def record_failure(self, device: str, reason: str = "") -> None:
        state = self._devices.get(device)
        if not state:
            return
        state.failure_count += 1
        state.last_failure_reason = reason
        if state.circuit == CircuitState.HALF_OPEN:
            # Probe failed — re-open
            state.circuit = CircuitState.OPEN
            state.opened_at = time.monotonic()
            logger.warning("Circuit re-OPEN for '%s' (probe failed: %s)", device, reason)
        elif state.failure_count >= CIRCUIT_FAILURE_THRESHOLD:
            state.circuit = CircuitState.OPEN
            state.opened_at = time.monotonic()
            logger.warning(
                "Circuit OPEN for '%s' after %d failures (last: %s)",
                device, state.failure_count, reason,
            )

    @asynccontextmanager
    async def acquire(self, device: str):
        """
        Async context manager that:
          - Rejects immediately if circuit is OPEN
          - Acquires the per-device semaphore (enforces MAX_CONCURRENT_PER_DEVICE)
          - Yields control to the caller
        """
        if self.is_open(device):
            state = self._devices[device]
            remaining = max(0, CIRCUIT_OPEN_SECONDS - (time.monotonic() - state.opened_at))
            raise CircuitOpenError(
                f"Device '{device}' circuit is OPEN — not reachable. "
                f"Retry in {remaining:.0f}s. Last error: {state.last_failure_reason}"
            )

        dev_state = await self._get_state(device)
        async with dev_state.semaphore:
            yield

    def status_summary(self) -> list[dict]:
        """Return circuit breaker status for all known devices."""
        result = []
        for name, state in self._devices.items():
            result.append({
                "device": name,
                "circuit": state.circuit.value,
                "failures": state.failure_count,
                "last_error": state.last_failure_reason,
            })
        return result


class CircuitOpenError(Exception):
    """Raised when a device's circuit breaker is OPEN."""
    pass


# Singleton pool — imported by all device adapters
_pool: Optional[DevicePool] = None


def get_pool() -> DevicePool:
    global _pool
    if _pool is None:
        _pool = DevicePool()
    return _pool
