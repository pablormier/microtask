"""Shared test helpers for microtask unit, API, and CLI tests."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import subprocess
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent


def run_cli(
    root: Path,
    *args: str,
    input_text: str | None = None,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess:
    """Execute ``./mt`` in *root* and return the completed process."""
    kwargs: dict[str, object] = {
        "cwd": root,
        "capture_output": True,
        "check": False,
        "env": env,
    }
    if input_bytes is not None:
        kwargs["input"] = input_bytes
        kwargs["text"] = False
    elif input_text is not None:
        kwargs["input"] = input_text
        kwargs["text"] = text
    else:
        kwargs["text"] = text
    return subprocess.run([str(root / "mt"), *args], **kwargs)


class FakeClock:
    """Deterministic wall-clock helper for tests."""

    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 6, 15, 9, 0, 0)

    def now(self, tz=None) -> datetime:
        if tz is not None:
            if self.current.tzinfo is None:
                return self.current.replace(tzinfo=tz)
            return self.current.astimezone(tz)
        return self.current

    def today(self):
        return self.current.date()

    def time(self) -> float:
        return self.current.timestamp()

    def advance(
        self,
        seconds: float = 0,
        *,
        minutes: float = 0,
        hours: float = 0,
        days: float = 0,
    ) -> datetime:
        self.current = self.current + timedelta(
            seconds=seconds, minutes=minutes, hours=hours, days=days
        )
        return self.current

    def set(self, dt: datetime) -> datetime:
        self.current = dt
        return self.current

    @contextmanager
    def patch(self, *modules):
        """Patch ``datetime`` and ``clock_time`` / ``time`` on *modules*."""
        clock = self

        class _FakeDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.now(tz)

            @classmethod
            def today(cls):
                return clock.now()

        class _FakeTimeModule:
            def time(self) -> float:
                return clock.time()

            def sleep(self, seconds: float) -> None:
                clock.advance(seconds=seconds)

        fake_time = _FakeTimeModule()
        with ExitStack() as stack:
            for mod in modules:
                if hasattr(mod, "datetime"):
                    stack.enter_context(mock.patch.object(mod, "datetime", _FakeDateTime))
                if hasattr(mod, "clock_time"):
                    stack.enter_context(mock.patch.object(mod, "clock_time", fake_time))
                if hasattr(mod, "time") and hasattr(getattr(mod, "time"), "time"):
                    stack.enter_context(mock.patch.object(mod, "time", fake_time))
            yield clock
