"""Attachment naming, path safety, MIME helpers, and Markdown link extraction."""
from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
import re
from urllib.parse import quote, unquote

DEFAULT_MAX_ATTACHMENT_MB = 10

IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".bmp", ".ico", ".svg",
})

# Active/scriptable types that must be forced to download if opened directly.
DOWNLOAD_ONLY_EXTENSIONS = frozenset({
    ".html", ".htm", ".xhtml", ".xml", ".js", ".mjs", ".cjs", ".css",
})

_GENERIC_PASTE_STEMS = frozenset({
    "", "image", "img", "pasted-image", "paste", "clipboard", "blob", "screenshot", "file",
})

_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
}

_EXT_RE = re.compile(r"^\.[a-z0-9]{1,12}$")
_CODE_OR_LINK_RE = re.compile(
    r"(?ms)(?:^|\n)(`{3,}|~{3,})[^\n]*\n.*?\n\1(?=\r?\n|$)"
    r"|(`+)(?:[^\r\n`]|(?!\2)`)*?\2"
    r"|!?\[[^\]]*\]\(\s*(?:<([^>\r\n]+)>|([^\s()]+(?:\([^()]*\)[^\s()]*)*))(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)"
)


def max_attachment_bytes(policy: dict | None) -> int:
    rules = policy.get("rules", {}) if isinstance(policy, dict) else {}
    raw = rules.get("max_attachment_mb", DEFAULT_MAX_ATTACHMENT_MB) if isinstance(rules, dict) else DEFAULT_MAX_ATTACHMENT_MB
    try:
        mb = float(raw)
    except (TypeError, ValueError):
        mb = float(DEFAULT_MAX_ATTACHMENT_MB)
    if mb <= 0 or mb == float("inf"):
        mb = float(DEFAULT_MAX_ATTACHMENT_MB)
    return int(mb * 1024 * 1024)


def normalize_extension(filename: str = "", content_type: str | None = None) -> str:
    if filename:
        base = Path(filename.replace("\\", "/")).name
        suffix = Path(base).suffix.lower()
        if _EXT_RE.fullmatch(suffix):
            return suffix
    if content_type:
        mime = content_type.split(";", 1)[0].strip().lower()
        if mime in _MIME_TO_EXT:
            return _MIME_TO_EXT[mime]
        guessed = mimetypes.guess_extension(mime) or ""
        if guessed == ".jpe":
            guessed = ".jpg"
        if _EXT_RE.fullmatch(guessed.lower()):
            return guessed.lower()
    return ""


def sanitize_stem(filename: str = "") -> str:
    if not filename:
        return ""
    base = Path(filename.replace("\\", "/")).name
    stem = Path(base).stem.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")[:48].strip("-")
    if slug in _GENERIC_PASTE_STEMS:
        return ""
    return slug


def build_attachment_filename(data: bytes, filename: str = "", content_type: str | None = None) -> str:
    digest = hashlib.sha256(data).hexdigest()[:12]
    ext = normalize_extension(filename, content_type)
    stem = sanitize_stem(filename)
    if stem:
        return f"{digest}-{stem}{ext}"
    return f"{digest}{ext}"


def is_image_filename(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


def guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".webp":
        return "image/webp"
    if suffix == ".avif":
        return "image/avif"
    if suffix == ".md":
        return "text/markdown; charset=utf-8"
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def should_force_download(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in DOWNLOAD_ONLY_EXTENSIONS:
        return True
    if suffix in IMAGE_EXTENSIONS or suffix == ".pdf":
        return False
    return True


def attachment_csp(path: Path) -> str:
    """Return a restrictive Content-Security-Policy for serving an attachment."""
    if path.suffix.lower() == ".pdf":
        return "default-src 'none'; object-src 'self'"
    return "sandbox"


def content_disposition_attachment(filename: str) -> str:
    """Build an ASCII-safe RFC 6266 Content-Disposition attachment header value."""
    cleaned = re.sub(r'[\x00-\x1f\x7f"\\]+', "_", filename).strip() or "attachment"
    ascii_fallback = cleaned.encode("ascii", "replace").decode("ascii").replace("?", "_").strip() or "attachment"
    if ascii_fallback == cleaned:
        return f'attachment; filename="{ascii_fallback}"'
    encoded = quote(cleaned, safe="")
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{encoded}"


def markdown_link(relative_path: str, label: str = "", *, is_image: bool | None = None) -> str:
    image = is_image_filename(relative_path) if is_image is None else is_image
    clean_label = re.sub(r"[\[\]\r\n]+", " ", label).strip()
    if image:
        return f"![{clean_label}]({relative_path})"
    return f"[{clean_label or Path(relative_path).name}]({relative_path})"


def validate_relative_subpath(rel_path: str) -> Path | None:
    """Return a normalized relative Path inside attachments/, or None if unsafe."""
    if not rel_path or "\x00" in rel_path or "\\" in rel_path:
        return None
    if rel_path.startswith("/"):
        return None
    parts = [part for part in rel_path.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return Path(*parts)


def resolve_attachment_path(attachments_root: Path, rel_path: str) -> Path | None:
    """Resolve an existing non-symlink file strictly inside attachments_root."""
    subpath = validate_relative_subpath(rel_path)
    if subpath is None:
        return None
    if attachments_root.is_symlink() or not attachments_root.is_dir():
        return None
    root_resolved = attachments_root.resolve()
    current = attachments_root
    for part in subpath.parts:
        current = current / part
        if current.is_symlink():
            return None
    if not current.is_file():
        return None
    resolved = current.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return resolved


def extract_attachment_targets(text: str) -> list[str]:
    """Return relative paths under attachments/ referenced via ../attachments/... links."""
    targets: list[str] = []
    seen: set[str] = set()
    for match in _CODE_OR_LINK_RE.finditer(text or ""):
        raw_target = match.group(3) or match.group(4)
        if not raw_target:
            continue
        cleaned = raw_target.split("#", 1)[0].split("?", 1)[0]
        if not cleaned.startswith("../attachments/"):
            continue
        rel = unquote(cleaned[len("../attachments/"):])
        if rel and rel not in seen:
            seen.add(rel)
            targets.append(rel)
    return targets
