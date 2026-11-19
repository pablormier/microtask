#!/usr/bin/env python3
"""Update the application folder to the next release, leaving the workspace alone.

Before 1.0 a minor version can break things and carry migration code that the next minor
removes, so an update never skips a minor version: it installs the newest patch of the
current minor, or else the newest patch of the next minor, and asks you to run it again.
Releases are downloaded as tarballs, so no Git is needed. Only the application's
top-level entries are replaced. The workspace is never touched, whether it lives in
workspace/ or in the folder MT_WORKSPACE names.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath

import workspace as workspace_module

ROOT = Path(__file__).resolve().parent.parent
RELEASES_URL = "https://api.github.com/repos/pablormier/microtask/releases?per_page=100"
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
MAX_PAGES = 20
ARCHIVE_URL = "https://github.com/pablormier/microtask/archive/refs/tags/{tag}.tar.gz"
STAGING = ".update-tmp"
# The top-level entries the installed release brought. Only these are ever removed, so
# files you added to the application folder yourself stay where they are.
RECORD = ".release-files"
# Written by deploy/upgrade.sh into every version folder it installs on a server.
HOSTED_MARKER = ".mt-hosted"
# Never replaced or removed: the workspace, the update's own scratch folder, the
# version files, and the hosting kit for the server operator, which `mt update` never
# installs or changes.
KEEP = {"workspace", STAGING, "deploy", "VERSION", RECORD, HOSTED_MARKER}
IGNORED_NAMES = {"__pycache__", ".DS_Store"}
TAG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UpdateError(Exception):
    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code


def current_version(root: Path = ROOT) -> str:
    try:
        text = (root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return text or "unknown"


def parse_version(tag: str) -> tuple[int, int, int] | None:
    match = VERSION_PATTERN.fullmatch(tag.strip())
    return tuple(int(part) for part in match.groups()) if match else None


def release_tags() -> dict[tuple[int, int, int], str]:
    """Published releases (no drafts or prereleases, no tags that are not versions), by version.

    Read from the GitHub releases list, or from MT_RELEASES_URL, following the Link header
    from page to page.
    """
    url: str | None = os.environ.get("MT_RELEASES_URL") or RELEASES_URL
    found: dict[tuple[int, int, int], str] = {}
    for _ in range(MAX_PAGES):
        if not url:
            break
        request = urllib.request.Request(
            url, headers={"User-Agent": "microtask-update", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                entries_ = json.loads(response.read().decode("utf-8"))
                link = response.headers.get("Link") or ""
        except urllib.error.HTTPError as error:
            if error.code in {403, 429}:
                raise UpdateError("GitHub is limiting requests right now, so the releases could not be listed. "
                                  "Try again later, or choose a release with MT_RELEASE_TAG=vX.Y.Z") from error
            raise UpdateError(f"could not list the releases at {url}: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise UpdateError(f"could not read the releases at {url}: {error}") from error
        if not isinstance(entries_, list):
            raise UpdateError(f"{url} did not return a list of releases")
        for entry in entries_:
            if not isinstance(entry, dict) or entry.get("draft") or entry.get("prerelease"):
                continue
            tag = entry.get("tag_name")
            version = parse_version(tag) if isinstance(tag, str) else None
            if version and TAG_PATTERN.fullmatch(tag):
                found.setdefault(version, tag)
        match = re.search(r'<([^>]+)>\s*;\s*rel="next"', link)
        url = match.group(1) if match else None
    if not found:
        raise UpdateError("no releases found; set MT_RELEASE_TAG=vX.Y.Z to choose one")
    return found


def next_step(current: tuple[int, int, int], releases: dict[tuple[int, int, int], str]) -> str | None:
    """The release to install after current, never skipping a minor version.

    The newest patch of current's minor when it is newer than current; otherwise the newest
    patch of the smallest later minor; otherwise None.
    """
    same = [version for version in releases if version[:2] == current[:2] and version > current]
    if same:
        return releases[max(same)]
    later = {version[:2] for version in releases if version[:2] > current[:2]}
    if not later:
        return None
    nearest = min(later)
    return releases[max(version for version in releases if version[:2] == nearest)]


def plan_target(version: str) -> tuple[str | None, str, str | None, str | None]:
    """Return (target, latest, note, following) for the installed version.

    target is None when up to date; following is the release after target, if any.

    MT_RELEASE_TAG forces one exact tag and skips stepping.
    """
    override = os.environ.get("MT_RELEASE_TAG")
    if override:
        tag = check_tag(override)
        return (None if tag == version else tag), tag, None, None
    releases = release_tags()
    latest = releases[max(releases)]
    current = parse_version(version)
    if current is None:
        note = (f"The current version ({version}) is unknown, so no stepping was applied: "
                f"going straight to the latest release.")
        return (None if latest == version else latest), latest, note, None
    target = next_step(current, releases)
    following = next_step(parse_version(target), releases) if target else None
    return target, latest, None, following


def check_tag(tag: str) -> str:
    if not TAG_PATTERN.fullmatch(tag):
        raise UpdateError(f"not a valid release tag: {tag}")
    return tag


def archive_url(tag: str) -> str:
    return os.environ.get("MT_ARCHIVE_URL") or ARCHIVE_URL.format(tag=tag)


def download(url: str, destination: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=60) as response, destination.open("wb") as output:
            shutil.copyfileobj(response, output)
    except (urllib.error.URLError, OSError) as error:
        raise UpdateError(f"could not download {url}: {error}") from error


def extract(archive_path: Path, destination: Path) -> Path:
    """Unpack a release tarball into destination and return its single top-level folder.

    Every member is checked first: no absolute paths, no '..', no links or devices.
    """
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            top = None
            for member in members:
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise UpdateError(f"unsafe path in the release archive: {member.name}")
                if not (member.isfile() or member.isdir()):
                    raise UpdateError(f"unsupported entry in the release archive: {member.name}")
                if top is None:
                    top = path.parts[0]
                elif path.parts[0] != top:
                    raise UpdateError("the release archive has more than one top-level folder")
            if top is None:
                raise UpdateError("the release archive is empty")
            options = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            archive.extractall(destination, members=members, **options)
    except (tarfile.TarError, OSError, EOFError) as error:
        raise UpdateError(f"could not read the release archive: {error}") from error
    root = destination / top
    if not (root / "mt").is_file() or not (root / "scripts" / "cli.py").is_file():
        raise UpdateError("the release archive does not contain microtask")
    return root


def fetch_release(tag: str, scratch: Path) -> Path:
    """Download and unpack release tag under scratch; return the folder holding the new files."""
    archive_path = scratch / "release.tar.gz"
    download(archive_url(tag), archive_path)
    unpacked = scratch / "new"
    unpacked.mkdir()
    return extract(archive_path, unpacked)


def entries(folder: Path) -> list[str]:
    return sorted(item.name for item in folder.iterdir()
                  if item.name not in KEEP and item.name not in IGNORED_NAMES)


def tree_digest(path: Path) -> str:
    """Hash of a file or folder: names, contents and the executable bit, ignoring caches."""
    digest = hashlib.sha256()
    if path.is_dir():
        for current, folders, files in os.walk(path):
            folders[:] = sorted(name for name in folders if name not in IGNORED_NAMES)
            for name in sorted(files):
                if name in IGNORED_NAMES or name.endswith(".pyc"):
                    continue
                file = Path(current) / name
                digest.update(str(file.relative_to(path)).encode() + b"\0")
                digest.update(b"x" if os.access(file, os.X_OK) else b"-")
                digest.update(file.read_bytes())
    else:
        digest.update(b"x" if os.access(path, os.X_OK) else b"-")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def recorded_entries(root: Path = ROOT) -> set[str]:
    """Top-level entries of the installed release; empty when there is no record."""
    try:
        text = (root / RECORD).read_text(encoding="utf-8")
    except OSError:
        return set()
    return {line.strip() for line in text.splitlines() if line.strip()}


def plan(new_root: Path) -> dict[str, list[str]]:
    old, new = set(entries(ROOT)), set(entries(new_root))
    recorded = recorded_entries()
    return {
        "added": sorted(new - old),
        "removed": sorted((old - new) & recorded),
        "kept": sorted((old - new) - recorded),
        "replaced": sorted(name for name in old & new
                           if tree_digest(ROOT / name) != tree_digest(new_root / name)),
    }


def refuse_unsuitable_folder() -> None:
    if (ROOT / HOSTED_MARKER).exists():
        raise UpdateError("this is a hosted version folder; install new versions on the server with "
                          "deploy/upgrade.sh instead")
    if (ROOT / ".git").exists():
        raise UpdateError("this folder is a Git clone; update it with 'git pull' instead")
    if (ROOT / "cards").is_dir() and any((ROOT / name).is_file() for name in ("settings.yaml", "mt-policy.yaml")):
        raise UpdateError("this folder still keeps your cards next to the application, and an update would "
                          "remove them. Run 'mt migrate' first to move them into workspace/")


def refuse_unmigrated_workspace(version: str, target: str) -> None:
    """A step to a new minor version needs a migrated workspace; patch updates do not."""
    old, new = parse_version(version), parse_version(target)
    if old and new and old[:2] == new[:2]:
        return
    root = workspace_module.resolve_workspace_root(app_root=ROOT)
    if workspace_module.is_unmigrated_workspace(root):
        raise UpdateError(f"the workspace {root} still uses {workspace_module.OLD_SETTINGS_FILE}, and {target} "
                          f"is a new minor version. Run 'mt migrate' first, then run 'mt update' again")


def hold_lock():
    """Take the update lock, or fail when another update is running."""
    (ROOT / STAGING).mkdir(exist_ok=True)
    handle = (ROOT / STAGING / "lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise UpdateError("another mt update is running")
    return handle


def swap(new_root: Path, changes: dict[str, list[str]], backup: Path) -> list[tuple[str, str]]:
    """Replace the changed top-level entries one by one, and return how to undo them.

    Old entries wait in backup until the update has finished. If a step fails, everything
    already moved is put back.
    """
    backup.mkdir()
    undo: list[tuple[str, str]] = []
    try:
        for name in changes["removed"] + changes["replaced"]:
            os.rename(ROOT / name, backup / name)
            undo.append(("restore", name))
        for name in changes["replaced"] + changes["added"]:
            os.rename(new_root / name, ROOT / name)
            undo.append(("remove", name))
    except OSError:
        rollback(backup, undo)
        raise
    return undo


def rollback(backup: Path, undo: list[tuple[str, str]]) -> None:
    for action, name in reversed(undo):
        target = ROOT / name
        if action == "remove":
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists() or target.is_symlink():
                target.unlink()
        else:
            os.rename(backup / name, target)


def write_file(name: str, text: str) -> None:
    handle, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=ROOT)
    with os.fdopen(handle, "w", encoding="utf-8") as output:
        output.write(text)
    os.chmod(temporary, 0o644)
    os.replace(temporary, ROOT / name)


def write_version(tag: str, release_entries: list[str]) -> None:
    write_file(RECORD, "".join(name + "\n" for name in release_entries))
    write_file("VERSION", tag + "\n")


def run_mt(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(ROOT / "mt"), *arguments], cwd=ROOT, text=True, capture_output=True, check=False)


def report(version: str, target: str | None, latest: str, changes: dict[str, list[str]] | None,
           as_json: bool, note: str | None = None, more: bool = False) -> None:
    summary = {"current_version": version, "latest_version": latest, "target_version": target,
               "steps_remaining": more, "up_to_date": target is None}
    if changes is not None:
        summary.update(changes)
    if as_json:
        print(json.dumps(summary))
        return
    print(f"Current version: {version}")
    print(f"Latest version:  {latest}")
    if note:
        print(note)
    if changes is None:
        print("Already up to date.")
        return
    print(f"Target version:  {target}")
    if more:
        print("More releases follow; run 'mt update' again after this one.")
    for label in ("added", "removed", "replaced"):
        if changes[label]:
            print(f"Would be {label}: {', '.join(changes[label])}")
    if changes["kept"]:
        print(f"Not from a release, left as they are: {', '.join(changes['kept'])}")
    if not any(changes[label] for label in ("added", "removed", "replaced")):
        print("No application files differ.")
    print("Your workspace is not changed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mt update",
        description="Replace the application files with the next release (one minor version at a time), leaving the workspace as it is. "
                    "Needs no Git. A Git clone of the application is updated with git pull instead.",
    )
    parser.add_argument("--check", action="store_true",
                        help="show the current, next and latest versions and which top-level entries would change")
    parser.add_argument("--json", action="store_true", help="print the --check report as JSON")
    parser.add_argument("--force", action="store_true", help="accepted for compatibility; does nothing")
    args = parser.parse_args(argv)
    if args.json and not args.check:
        parser.error("--json requires --check")
    try:
        refuse_unsuitable_folder()
        version = current_version()
        tag, latest, note, following = plan_target(version)
        if args.check:
            if tag is None:
                report(version, None, latest, None, args.json, note)
                return 0
            refuse_unmigrated_workspace(version, tag)
            with tempfile.TemporaryDirectory(prefix="mt-update-") as scratch:
                new_root = fetch_release(tag, Path(scratch))
                report(version, tag, latest, plan(new_root), args.json, note, bool(following))
            return 0
        if tag is None:
            print(f"Already up to date ({version}). No files changed.")
            return 0
        refuse_unmigrated_workspace(version, tag)
        if note:
            print(note)
        lock = hold_lock()
        scratch = ROOT / STAGING
        try:
            for leftover in scratch.iterdir():
                if leftover.name != "lock":
                    shutil.rmtree(leftover, ignore_errors=True) if leftover.is_dir() else leftover.unlink()
            new_root = fetch_release(tag, scratch)
            release_entries = entries(new_root)
            changes = plan(new_root)
            backup = scratch / "old"
            undo = swap(new_root, changes, backup)
            check = run_mt("help")
            if check.returncode:
                rollback(backup, undo)
                detail = check.stderr.strip() or check.stdout.strip()
                raise UpdateError(f"the new version did not start, so nothing was changed: {detail}")
            write_version(tag, release_entries)
            validation = run_mt("validate")
        finally:
            lock.close()
            shutil.rmtree(scratch, ignore_errors=True)
        print(f"Updated microtask: {version} -> {tag}")
        for label in ("added", "removed", "replaced"):
            if changes[label]:
                print(f"  {label}: {', '.join(changes[label])}")
        if validation.returncode:
            print("warning: the new version reports problems in the workspace:", file=sys.stderr)
            print((validation.stderr.strip() or validation.stdout.strip()), file=sys.stderr)
        else:
            print("Workspace checked with the new version.")
        print("Your workspace was not changed. If the web dashboard is running, restart it.")
        if following:
            print(f"A newer release exists: {following}. If the new version asks you to run 'mt migrate', "
                  f"do that first.")
            print(f"Run 'mt update' again to continue to {following}")
        return 0
    except (OSError, UpdateError) as error:
        print(f"error: {error}", file=sys.stderr)
        return error.code if isinstance(error, UpdateError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
