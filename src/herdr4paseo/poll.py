"""Status-poll scheduling and the one-agent probe. Pure: no I/O, no Textual, a clock you can fake.

Every poll spawns a Node `paseo` process, and the old fixed 3 s tick ran two of them
(`ls -g -a --json`, a ~1 MB listing of every agent ever, then `permit ls --json`) per
instance. Seven panes meant ~4.7 processes a second and, under swap pressure, a machine-wide
load spike. The schedule here polls one agent with `paseo agent inspect <id> --json`, runs
`permit ls` only when that says a request is pending, and slows down while nothing changes.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .core import paseo_bin

# Environment overrides: seconds, each clamped to MIN_SECONDS so a typo cannot spin the CLI.
ENV_ACTIVE = "HERDR4PASEO_POLL_ACTIVE"
ENV_IDLE = "HERDR4PASEO_POLL_IDLE"
ENV_IDLE_MAX = "HERDR4PASEO_POLL_IDLE_MAX"
ENV_FAST = "HERDR4PASEO_POLL_FAST"
ENV_BURST = "HERDR4PASEO_POLL_BURST"
MIN_SECONDS = 0.5


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env[name])
    except (KeyError, ValueError):
        return default
    return max(MIN_SECONDS, value) if math.isfinite(value) else default  # NaN and inf keep the default


@dataclass(frozen=True)
class PollConfig:
    """Intervals in seconds.

    active: agent running or a request pending. A permission request can only arise while a turn
        runs, so this bounds how late an allow banner appears (about `active`, plus one probe).
    idle: first interval for a settled (not running) agent; doubles each unchanged poll.
    idle_max: ceiling of that backoff. It is also the worst-case wait for a turn started elsewhere
        that never reaches the log stream (a hint wakes the poll when output does arrive).
    fast: cadence just after a user action, an observed change or fresh log activity.
    burst: how long `fast` lasts after such an event.
    """

    active: float = 5.0
    idle: float = 15.0
    idle_max: float = 30.0
    fast: float = 3.0
    burst: float = 15.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> PollConfig:
        env = os.environ if env is None else env
        base = cls()
        idle = _seconds(env, ENV_IDLE, base.idle)
        return cls(
            active=_seconds(env, ENV_ACTIVE, base.active),
            idle=idle,
            idle_max=max(idle, _seconds(env, ENV_IDLE_MAX, base.idle_max)),
            fast=_seconds(env, ENV_FAST, base.fast),
            burst=_seconds(env, ENV_BURST, base.burst),
        )


class PollSchedule:
    """When to poll next. The app asks `delay()`; it reports `observe`, `bump` and `log_activity`."""

    def __init__(self, config: PollConfig | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.config = config or PollConfig()
        self._clock = clock
        self._seen: tuple[str, int] | None = None  # (status, pending requests) at the last poll
        self._unchanged = 0  # consecutive polls that saw the same thing
        self._quiet = False  # the previous poll ran outside a burst and changed nothing
        self._boost_until = clock() + self.config.burst  # a fresh attach polls fast for a moment
        self._last_hint = float("-inf")

    @property
    def status(self) -> str | None:
        return self._seen[0] if self._seen else None

    def observe(self, status: str, pending: int) -> bool:
        """Record a poll result; True when it differs from the previous one (which starts a burst)."""
        seen = (status, pending)
        changed = self._seen is not None and seen != self._seen
        # polls inside a burst do not count towards the back-off: it starts from `idle` once the burst ends
        burst = self._clock() < self._boost_until
        self._unchanged = self._unchanged + 1 if self._quiet and not changed else 0
        self._quiet = not burst and not changed
        self._seen = seen
        if changed:
            self._boost_until = self._clock() + self.config.burst
        return changed

    def bump(self) -> None:
        """A user action (send, answer, stop, attach) probably changes the agent: poll fast for a while."""
        self._boost_until = self._clock() + self.config.burst
        self._unchanged = 0
        self._quiet = False

    def log_activity(self) -> bool:
        """The log stream produced output. True asks the app to poll now rather than wait.

        Only a settled agent can wake up unseen (it is the long wait), so only then, and at most
        once per `min(idle, burst)` seconds: output while running or replayed on reattach costs nothing.
        """
        now = self._clock()
        if self.status == "running" or (self._seen and self._seen[1]) or now < self._boost_until:
            return False
        if now - self._last_hint < min(self.config.idle, self.config.burst):
            return False
        self._last_hint = now
        self._boost_until = now + self.config.burst
        self._unchanged = 0
        self._quiet = False
        return True

    def delay(self) -> float:
        """Seconds until the next poll."""
        cfg = self.config
        if self._clock() < self._boost_until:
            return cfg.fast
        if self._seen and (self._seen[0] == "running" or self._seen[1]):
            return cfg.active
        return min(cfg.idle_max, cfg.idle * 2 ** min(self._unchanged, 16))


def inspect_argv(agent_id: str) -> list[str]:
    return [paseo_bin(), "agent", "inspect", agent_id, "--json"]


def parse_inspect(raw: str, agent_id: str) -> tuple[str, bool]:
    """`paseo agent inspect <id> --json` -> (status, has a pending permission request).

    Raises ValueError for anything that is not that one agent, so the caller falls back to the
    full listing. Checked live 2026-11-21: `PendingPermissions` listed exactly the requests
    `paseo permit ls --json` listed, with `Status` `running` while they waited.
    """
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("Id") != agent_id:
        raise ValueError("expected one agent object from `paseo agent inspect --json`")
    status, pending = data.get("Status"), data.get("PendingPermissions")
    if not isinstance(status, str) or not isinstance(pending, list):
        raise ValueError("`paseo agent inspect --json` lacks Status or PendingPermissions")
    return status, bool(pending)
