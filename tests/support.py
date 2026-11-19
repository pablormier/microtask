"""Shared test helpers for microtask unit, API, and CLI tests."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import io
import shutil
import subprocess
import tarfile
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent


def legacy_workspace(root: Path) -> Path:
    """Turn a copy of the application into an old-style workspace: cards and settings beside the code.

    A developer's own workspace/ and update leftovers are dropped from the copy, so the copy
    is resolved as a legacy workspace.
    """
    root = Path(root)
    for name in ("workspace", ".update-tmp", "VERSION"):
        path = root / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    (root / "cards").mkdir(exist_ok=True)
    (root / "cards" / ".gitkeep").touch()
    shutil.copy2(ROOT / "templates" / "settings.yaml", root / "settings.yaml")
    return root


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
        # Without input, `mt add` and `mt note` read a non-terminal stdin; never inherit one that may stay open.
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run([str(root / "mt"), *args], **kwargs)


RELEASE_ENTRIES = ("mt", "install.sh", "README.md", "LICENSE", "AGENTS.md", "CLAUDE.md", "GEMINI.md",
                   "CONTRIBUTING.md", ".gitignore", "scripts", "tools", "templates", "examples", ".agents", "deploy")


def make_release(archive: Path, tag: str, *, extra: dict[str, bytes] | None = None,
                 drop: tuple[str, ...] = (), raw_members: list[tuple[str, bytes, int]] | None = None) -> Path:
    """Write a GitHub-style release tarball (one top-level folder) built from this checkout.

    extra maps paths inside the release to contents; drop removes top-level entries;
    raw_members adds members with exactly the given names, for unsafe-archive tests.
    """
    top = f"microtask-{tag.lstrip('v')}"
    with tarfile.open(archive, "w:gz") as output:
        for name in RELEASE_ENTRIES:
            source = ROOT / name
            if name in drop or not source.exists():
                continue
            output.add(source, arcname=f"{top}/{name}",
                       filter=lambda info: None if "__pycache__" in info.name or info.name.endswith(".DS_Store")
                       else info)
        for name, contents in (extra or {}).items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(contents)
            info.mode = 0o755 if name == "mt" else 0o644
            output.addfile(info, io.BytesIO(contents))
        for name, contents, mode in raw_members or []:
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = mode
            output.addfile(info, io.BytesIO(contents))
    return archive


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
