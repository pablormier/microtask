#!/usr/bin/env python3
"""Render a Markdown file with GFM and KaTeX in the default browser."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import secrets
import sys
import tempfile

APP_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_ROOT / "scripts"))

import tasklib  # noqa: E402


MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@15.0.12/marked.min.js"
DOMPURIFY_CDN = "https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js"
KATEX_CSS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css"
KATEX_JS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.js"
KATEX_AUTO_RENDER_CDN = (
    "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/contrib/auto-render.min.js"
)


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-{nonce}' https://cdn.jsdelivr.net; style-src 'unsafe-inline' https://cdn.jsdelivr.net; font-src https://cdn.jsdelivr.net; img-src file: data: https:; base-uri file:; object-src 'none'">
  <base href="{base_uri}">
  <title>{title}</title>
  <link rel="stylesheet" href="{katex_css}">
  <script nonce="{nonce}" defer src="{marked_js}"></script>
  <script nonce="{nonce}" defer src="{dompurify_js}"></script>
  <script nonce="{nonce}" defer src="{katex_js}"></script>
  <script nonce="{nonce}" defer src="{katex_auto_render_js}"></script>
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
          throw new Error("Preview libraries could not be loaded. Check the network connection and reload.");
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
    hidden = {"title", "comments", "time_entries", "processing_instructions", "next_action"} if is_card else set()
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


def safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def default_output_path(source: Path) -> Path:
    digest = hashlib.sha256(str(source.resolve()).encode()).hexdigest()[:8]
    name = source.stem or "markdown"
    return Path(tempfile.gettempdir()) / f"{name}-{digest}.mdviz.html"


def build_html(source: Path, text: str) -> str:
    header, body = tasklib.card_format.parse_frontmatter(text)
    properties = tasklib.card_format.parse_yaml(header) if header is not None else {}
    is_card = properties.get("kind") in {"task", "note"}
    if is_card:
        properties, body = tasklib.card_format.parse_document(text)
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
        marked_js=MARKED_CDN, dompurify_js=DOMPURIFY_CDN,
        katex_css=KATEX_CSS_CDN, katex_js=KATEX_JS_CDN,
        katex_auto_render_js=KATEX_AUTO_RENDER_CDN,
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
    output = args.out or default_output_path(source)
    try:
        output.write_text(build_html(source, text), encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"error: failed to write preview: {error}", file=sys.stderr)
        return 1
    if not args.no_open:
        tasklib.open_in_browser(output.resolve().as_uri())
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
