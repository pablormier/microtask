#!/usr/bin/env python3
"""Render a Markdown file with GFM and KaTeX in the default browser."""
from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "scripts"))
sys.path.insert(0, str(APP_ROOT / "tools"))

import card_format  # noqa: E402
import cli_helpers  # noqa: E402
import web_assets  # noqa: E402


VENDOR_DIR = web_assets.WEB_DIR / "vendor"
FONT_FACE_SRC = re.compile(r'src:url\(fonts/([^)]+\.woff2)\) format\("woff2"\)[^;}]*')


def vendor_text(url_path: str) -> str:
    return (VENDOR_DIR / url_path[len(web_assets.VENDOR_URL):]).read_text(encoding="utf-8")


def inline_script(source: str) -> str:
    """Make library source safe to place inside a <script> element."""
    return re.sub(r"</(script)", r"<\\/\1", source, flags=re.IGNORECASE)


def katex_css_with_fonts() -> str:
    """Return katex.min.css with its woff2 fonts embedded as data URIs.

    The page is opened from file://, where browsers may refuse fonts loaded from
    other local files. The woff and ttf fallbacks are not vendored, so they are dropped.
    """
    fonts = VENDOR_DIR / "katex/dist/fonts"

    def embed(match: re.Match[str]) -> str:
        data = base64.b64encode((fonts / match.group(1)).read_bytes()).decode("ascii")
        return f'src:url(data:font/woff2;base64,{data}) format("woff2")'

    css = FONT_FACE_SRC.sub(embed, vendor_text(web_assets.KATEX_CSS))
    if "fonts/" in css:
        raise ValueError("katex.min.css has font references that were not embedded")
    return re.sub(r"</(style)", r"<\\/\1", css, flags=re.IGNORECASE)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; font-src data:; img-src file: data: https:; base-uri file:; object-src 'none'">
  <base href="{base_uri}">
  <title>{title}</title>
  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3CclipPath id='c'%3E%3Crect width='32' height='32' rx='7.5'/%3E%3C/clipPath%3E%3C/defs%3E%3Cg clip-path='url(%23c)'%3E%3Crect width='32' height='32' fill='%232563eb'/%3E%3Crect width='6.5' height='32' fill='%23172554'/%3E%3Crect x='6.5' width='1' height='32' fill='%2393c5fd' fill-opacity='0.35'/%3E%3C/g%3E%3Cpath fill='%23fff' d='M11.6 9.5L14.6 8.5V16.2C14.6 18.2 15.8 19.3 17.7 19.3C19.6 19.3 20.9 18.1 20.9 16V9.5L23.9 8.5V18.2C23.9 19 24.4 19.4 25.3 19.4L26.2 19.2V21.5C25.4 21.8 24.6 22 23.6 22C22 22 21.1 21.2 20.8 20C19.8 21.3 18.3 22 16.5 22C15.7 22 15.1 21.8 14.6 21.5V24.8L11.6 25.8V9.5Z'/%3E%3C/svg%3E">
  <style>{katex_css}</style>
  <script nonce="{nonce}">{marked_js}</script>
  <script nonce="{nonce}">{dompurify_js}</script>
  <script nonce="{nonce}">{katex_js}</script>
  <script nonce="{nonce}">{katex_auto_render_js}</script>
  <style>
    :root {{ color-scheme: light dark; }}
    body {{
      box-sizing: border-box;
      max-width: 980px;
      margin: 0 auto;
      padding: 40px 32px 80px;
      color: CanvasText;
      background: Canvas;
      font: 16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main, section {{ min-width: 0; overflow-wrap: break-word; }}
    details {{ margin: 1rem 0; }}
    summary {{ cursor: pointer; }}
    article {{ margin: 1rem 0; }}
    time {{ font-size: .85em; opacity: .75; }}
    h1, h2 {{ border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); padding-bottom: .3em; }}
    h1, h2, h3, h4, h5, h6 {{ line-height: 1.25; margin: 1.5em 0 .6em; }}
    a {{ color: LinkText; }}
    pre {{ padding: 16px; overflow: auto; border-radius: 6px; background: color-mix(in srgb, CanvasText 8%, transparent); }}
    code {{ padding: .15em .3em; border-radius: 4px; background: color-mix(in srgb, CanvasText 8%, transparent); font: .9em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
    pre code {{ padding: 0; background: transparent; }}
    ul {{ list-style: disc; padding-left: 1.35rem; }}
    ol {{ list-style: decimal; padding-left: 1.35rem; }}
    blockquote {{ margin-left: 0; padding-left: 1em; border-left: 4px solid color-mix(in srgb, CanvasText 22%, transparent); color: color-mix(in srgb, CanvasText 72%, transparent); }}
    table {{ display: block; max-width: 100%; overflow-x: auto; width: 100%; margin: 1em 0; border-spacing: 0; border-collapse: collapse; }}
    th, td {{ padding: 6px 12px; border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); }}
    th {{ background: color-mix(in srgb, CanvasText 7%, transparent); }}
    img {{ max-width: 100%; }}
    .metadata {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px 20px; margin-bottom: 32px; padding: 16px 18px; border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); border-radius: 8px; background: color-mix(in srgb, CanvasText 4%, transparent); }}
    .metadata div {{ min-width: 0; }}
    .metadata dt {{ font-size: .75em; font-weight: 650; text-transform: uppercase; color: color-mix(in srgb, CanvasText 62%, transparent); }}
    .metadata dd {{ margin: 2px 0 0; overflow-wrap: anywhere; }}
    .error {{ color: #c62828; white-space: pre-wrap; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
  </style>
</head>
<body>
  {heading}
  {metadata}
  <main id="content"></main>
  <section id="activity"></section>
  <script nonce="{nonce}">
    {renderer_js}
    const card = {card_json};
    window.addEventListener("DOMContentLoaded", () => {{
      const root = document.getElementById("content");
      try {{
        if (!window.marked || !window.DOMPurify || !window.renderMathInElement) {{
          throw new Error("Preview libraries could not be loaded. Reload the preview or reinstall microtask.");
        }}
        renderPreview(card, root);
        renderPropertyPanels(card, document.getElementById("activity"));
      }} catch (error) {{
        root.replaceChildren();
        const message = document.createElement("pre");
        message.className = "error";
        message.textContent = String(error && error.stack ? error.stack : error);
        root.appendChild(message);
      }}
    }});
  </script>
</body>
</html>
"""


def render_metadata(properties: dict[str, object], *, is_card: bool) -> str:
    hidden = {"title", "comments", "time_entries", "timer", "processing_instructions", "next_action"} if is_card else set()
    fields = []
    for key, value in properties.items():
        if key in hidden:
            continue
        if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
            text = ", ".join(str(item) for item in value)
        else:
            text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value) if value is not None else "—"
        fields.append(f"<div><dt>{html.escape(key)}</dt><dd>{html.escape(text)}</dd></div>")
    result = '<details><summary>Properties</summary><dl class="metadata" aria-label="Document metadata">' + "".join(fields) + "</dl></details>" if fields else ""
    return result


safe_json = web_assets.safe_json


def default_output_path(source: Path) -> Path:
    digest = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:8]
    name = source.stem or "markdown"
    # One private folder per user, so a preview of the same file reuses one path.
    folder = Path(tempfile.gettempdir()) / f"mt-mdviz-{os.getuid()}"
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise OSError(f"{folder} is not a private folder owned by you; pass --out")
    return folder / f"{name}-{digest}.mdviz.html"


def build_html(source: Path, text: str) -> str:
    header, body = card_format.parse_frontmatter(text)
    properties = card_format.parse_yaml(header) if header is not None else {}
    is_card = properties.get("kind") in {"task", "note"}
    if is_card:
        properties, body = card_format.parse_document(text)
    base_uri = source.resolve().parent.as_uri() + "/"
    card = {"body": body, "base_uri": base_uri}
    if is_card:
        card.update({field: properties[field] for field in
                     ("kind", "next_action", "processing_instructions", "comments", "time_entries") if field in properties})
    nonce = secrets.token_urlsafe(18)
    title = str(properties.get("title") or source.name)
    return HTML_TEMPLATE.format(
        nonce=nonce, base_uri=html.escape(base_uri, quote=True), title=html.escape(title),
        heading="<h1>" + html.escape(title) + "</h1>" if is_card else "",
        metadata=render_metadata(properties, is_card=is_card), card_json=safe_json(card),
        renderer_js="\n".join((APP_ROOT / "tools/web/js" / name).read_text(encoding="utf-8")
                              for name in ("markdown.js", "card_helpers.js")),
        marked_js=inline_script(vendor_text(web_assets.MARKED_JS)),
        dompurify_js=inline_script(vendor_text(web_assets.DOMPURIFY_JS)),
        katex_css=katex_css_with_fonts(),
        katex_js=inline_script(vendor_text(web_assets.KATEX_JS)),
        katex_auto_render_js=inline_script(vendor_text(web_assets.KATEX_AUTO_RENDER_JS)),
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview Markdown with GFM and KaTeX.")
    parser.add_argument("file", type=Path, help="Markdown file to preview")
    parser.add_argument("--out", type=Path, help="output HTML path; defaults to a temporary file")
    parser.add_argument("--no-open", action="store_true", help="write HTML without opening a browser")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    source = args.file
    if not source.is_file():
        print(f"error: not a file: {source}", file=sys.stderr)
        return 1
    try:
        text = source.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        print(f"error: failed to read Markdown: {error}", file=sys.stderr)
        return 1
    try:
        output = args.out or default_output_path(source)
        output.write_text(build_html(source, text), encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"error: failed to write preview: {error}", file=sys.stderr)
        return 1
    if not args.no_open:
        cli_helpers.open_in_browser(output.resolve().as_uri())
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
