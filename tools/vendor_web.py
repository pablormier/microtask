#!/usr/bin/env python3
"""Refresh the third-party libraries vendored for the web dashboard.

Run by hand after changing a version in LIBRARIES; tests and the dashboard never
run it. It downloads each pinned npm tarball, verifies the registry's sha512
integrity value, copies only the listed files under tools/web/vendor, and
rewrites tools/web/vendor/VENDOR.json. Review the resulting diff.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tarfile
import urllib.request

VENDOR_DIR = Path(__file__).resolve().parent / "web" / "vendor"
REGISTRY = "https://registry.npmjs.org"

# files: package paths to copy (the same relative layout is kept under VENDOR_DIR/<folder>).
# license_file: the package's own license file, saved as <folder>/LICENSE.
LIBRARIES = {
    "pico": {
        "package": "@picocss/pico", "version": "2.1.1", "license": "MIT",
        "license_file": "LICENSE.md", "files": ["css/pico.min.css"],
    },
    "alpinejs": {
        "package": "alpinejs", "version": "3.15.12", "license": "MIT",
        "files": ["dist/cdn.min.js"],
        # The npm package ships no license file; use the one tagged in the upstream repository.
        "license_url": "https://raw.githubusercontent.com/alpinejs/alpine/v3.15.12/LICENSE.md",
        "license_sha256": "08b7502da6e7aa1d0bbdc97d220fbf669b9366c61bd0f072238283c89bc4773a",
    },
    "marked": {
        "package": "marked", "version": "15.0.12", "license": "MIT",
        "license_file": "LICENSE.md", "files": ["lib/marked.umd.js"],
    },
    "dompurify": {
        "package": "dompurify", "version": "3.2.6", "license": "Apache-2.0",
        "license_note": "Published as (MPL-2.0 OR Apache-2.0); used here under Apache-2.0.",
        "license_file": "LICENSE", "files": ["dist/purify.min.js"],
    },
    "katex": {
        "package": "katex", "version": "0.16.10", "license": "MIT",
        "license_file": "LICENSE",
        "files": ["dist/katex.min.css", "dist/katex.min.js", "dist/contrib/auto-render.min.js"],
        # Only woff2 fonts are kept. katex.min.css lists woff2 first, then woff and ttf
        # fallbacks that no supported browser requests.
        "css_fonts": "dist/katex.min.css",
    },
}


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_package(package: str, version: str) -> tuple[dict, dict[str, bytes]]:
    """Return the registry record and the verified tarball's files keyed by package path."""
    record = json.loads(fetch(f"{REGISTRY}/{package}/{version}"))
    name = package.split("/")[-1]
    url = f"{REGISTRY}/{package}/-/{name}-{version}.tgz"
    data = fetch(url)
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if integrity != record["dist"]["integrity"]:
        raise SystemExit(f"integrity mismatch for {package}@{version}: {integrity}")
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            if member.isfile() and member.name.startswith("package/"):
                files[member.name[len("package/"):]] = archive.extractfile(member).read()
    return {"url": url, "integrity": integrity, "published_license": record.get("license")}, files


def vendor_library(folder: str, spec: dict) -> dict:
    info, package_files = read_package(spec["package"], spec["version"])
    wanted = list(spec["files"])
    if "css_fonts" in spec:
        css = package_files[spec["css_fonts"]].decode("utf-8")
        base = Path(spec["css_fonts"]).parent
        wanted += sorted({(base / m).as_posix() for m in re.findall(r"url\((fonts/[^)]+\.woff2)\)", css)})
    target = VENDOR_DIR / folder
    if target.exists():
        shutil.rmtree(target)
    written: dict[str, bytes] = {}
    for path in wanted:
        written[path] = package_files[path]
    if "license_file" in spec:
        written["LICENSE"] = package_files[spec["license_file"]]
    else:
        text = fetch(spec["license_url"])
        if sha256(text) != spec["license_sha256"]:
            raise SystemExit(f"license text for {folder} does not match the pinned sha256")
        written["LICENSE"] = text
    for path, data in written.items():
        out = target / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    entry = {
        "package": spec["package"],
        "version": spec["version"],
        "url": info["url"],
        "integrity": info["integrity"],
        "license": spec["license"],
    }
    if info["published_license"] != spec["license"]:
        entry["published_license"] = info["published_license"]
    if "license_note" in spec:
        entry["license_note"] = spec["license_note"]
    if "license_url" in spec:
        entry["license_file_source"] = spec["license_url"]
    entry["files"] = {f"{folder}/{path}": sha256(data) for path, data in sorted(written.items())}
    entry["vendored_changes"] = []
    return entry


def main() -> int:
    VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {"libraries": {folder: vendor_library(folder, spec) for folder, spec in LIBRARIES.items()}}
    (VENDOR_DIR / "VENDOR.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"vendored {len(LIBRARIES)} libraries into {VENDOR_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
