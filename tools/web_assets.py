"""HTML, CSS, JS, and template assembly for the local web dashboard."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import secrets


PICO_CDN = "https://cdn.jsdelivr.net/npm/@picocss/pico@2.1.1/css/pico.min.css"
ALPINE_CDN = "https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js"
MARKED_CDN = "https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.umd.js"
DOMPURIFY_CDN = "https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.min.js"
KATEX_CSS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.css"
KATEX_JS_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/katex.min.js"
KATEX_AUTO_RENDER_CDN = "https://cdn.jsdelivr.net/npm/katex@0.16.10/dist/contrib/auto-render.min.js"

APP_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"


def application_version(app_root: Path = APP_ROOT) -> str:
    """Return a fingerprint of the dashboard and core application files on disk."""
    digest = hashlib.sha256()
    paths: list[Path] = []
    for folder in (app_root / "scripts", app_root / "tools"):
        if folder.is_dir():
            paths.extend(sorted(folder.rglob("*.py")))
    web_dir = app_root / "tools" / "web"
    if web_dir.is_dir():
        paths.extend(sorted(path for path in web_dir.rglob("*") if path.is_file()))
    for path in sorted(set(paths)):
        try:
            stat = path.stat()
        except OSError:
            continue
        rel = str(path.relative_to(app_root))
        digest.update(f"{rel}:{stat.st_mtime_ns}:{stat.st_size}\n".encode("utf-8"))
    return digest.hexdigest()


def load_css(web_dir: Path = WEB_DIR) -> str:
    css_files = [
        "theme.css",
        "components.css",
        "layout.css",
        "board.css",
        "views.css",
        "detail.css",
        "composer.css",
        "popover.css",
    ]
    return "\n".join((web_dir / "styles" / f).read_text(encoding="utf-8") for f in css_files)


def load_js(web_dir: Path = WEB_DIR) -> str:
    js_files = [
        "markdown.js",
        "card_helpers.js",
        "picker.js",
        "metadata_editor.js",
        "draft_store.js",
        "capture_composer.js",
        "property_editor.js",
        "content_editor.js",
        "filters_query.js",
        "draft_coordinator.js",
        "card_detail.js",
        "board.js",
    ]
    return "\n".join((web_dir / "js" / f).read_text(encoding="utf-8") for f in js_files)


def fill_placeholders(page: str, values: dict[str, str]) -> str:
    pattern = re.compile("|".join(re.escape(key) for key in values))
    return pattern.sub(lambda match: values[match.group(0)], page)


def _render_picker_list(template: str, field: str, *, empty_text: str, selected_expr: str,
                        label_expr: str = "row.name", depth_style: str = "") -> str:
    return fill_placeholders(template, {
        "{field}": field,
        "{empty_text}": empty_text,
        "{selected_expr}": selected_expr,
        "{label_expr}": label_expr,
        "{depth_style}": depth_style,
    })


def load_template(web_dir: Path = WEB_DIR) -> str:
    templates_dir = web_dir / "templates"
    index_html = (web_dir / "index.html").read_text(encoding="utf-8")
    card = (templates_dir / "card.html").read_text(encoding="utf-8")
    toolbar = (templates_dir / "toolbar.html").read_text(encoding="utf-8")
    timer_menu = (templates_dir / "timer_menu.html").read_text(encoding="utf-8")
    picker_list = (templates_dir / "picker_list.html").read_text(encoding="utf-8")
    view_focus = (
        (templates_dir / "views" / "focus.html").read_text(encoding="utf-8")
        .replace("{card}", card)
        .replace("{timer_menu}", timer_menu.replace("{timer_task}", "focusTask()").replace("{timer_blocked}", ""))
    )
    view_cards = (templates_dir / "views" / "cards.html").read_text(encoding="utf-8").replace("{card}", card)
    view_deadlines = (templates_dir / "views" / "deadlines.html").read_text(encoding="utf-8")
    view_drafts = (templates_dir / "views" / "drafts.html").read_text(encoding="utf-8")
    drawer = (
        (templates_dir / "drawer.html").read_text(encoding="utf-8")
        .replace("{timer_menu}", timer_menu.replace("{timer_task}", "selected").replace("{timer_blocked}", " || !!contentEditor.draft"))
    )
    capture_composer = (templates_dir / "capture_composer.html").read_text(encoding="utf-8")
    card_metadata_editor = fill_placeholders(
        (templates_dir / "card_metadata_editor.html").read_text(encoding="utf-8"),
        {
            "{picker_list_project}": _render_picker_list(
                picker_list, "project",
                empty_text="No projects yet. Type to create one.",
                selected_expr="String(row.kind === 'option' && row.name === value.project)",
            ),
            "{picker_list_topic}": _render_picker_list(
                picker_list, "topic",
                empty_text="No topics yet. Type a path such as learning/rust.",
                selected_expr="String(row.kind === 'option' && row.name === value.topic)",
                label_expr="row.label",
                depth_style=':style="row.depth ? { paddingLeft: (8 + row.depth * 14) + \'px\' } : null" ',
            ),
            "{picker_list_tags}": _render_picker_list(
                picker_list, "tags",
                empty_text="Type to add a tag.",
                selected_expr="String(row.kind === 'option' && false)",
            ),
        },
    )

    return (
        index_html
        .replace("{toolbar}", toolbar)
        .replace("{view_focus}", view_focus)
        .replace("{view_cards}", view_cards)
        .replace("{view_deadlines}", view_deadlines)
        .replace("{view_drafts}", view_drafts)
        .replace("{drawer}", drawer)
        .replace("{capture_composer}", capture_composer)
        .replace("{card_metadata_editor}", card_metadata_editor)
    )


def safe_json(value: object) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def build_html(config: dict, *, web_dir: Path = WEB_DIR) -> str:
    nonce = secrets.token_urlsafe(18)
    template = load_template(web_dir)
    css = load_css(web_dir)
    js = load_js(web_dir)

    page = fill_placeholders(template, {
        "{pico_css}": PICO_CDN,
        "{katex_css}": KATEX_CSS_CDN,
        "{alpine_js}": ALPINE_CDN,
        "{marked_js}": MARKED_CDN,
        "{dompurify_js}": DOMPURIFY_CDN,
        "{katex_js}": KATEX_JS_CDN,
        "{katex_auto_render_js}": KATEX_AUTO_RENDER_CDN,
        "{dashboard_css}": css,
        "{nonce}": nonce,
        "{app_script}": js,
    })
    pending = ("{config_json}",)
    leftover = [m for m in re.findall(r"\{[a-z_]+\}", page) if m not in pending]
    if leftover:
        raise ValueError(f"unreplaced placeholders in dashboard HTML: {leftover}")
    return fill_placeholders(page, {"{config_json}": safe_json(config)})
