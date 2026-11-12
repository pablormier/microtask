"""References in Markdown prose, independent of workspace and presentation."""
from __future__ import annotations

import re

from card_format import fenced_line_numbers

REFERENCE_RE = re.compile(r"(?<![\w@])@[0-9a-f]{3,8}(?![\w-])", re.IGNORECASE)
URL_RE = re.compile(r"(?:https?://|mailto:)[^\s<>]+", re.IGNORECASE)
HTML_RE = re.compile(r"<!--[\s\S]*?(?:-->|$)|<(a|code|pre|script|style)\b[^>]*>[\s\S]*?</\1\s*>|<[^>\n]*>", re.IGNORECASE)


def _closing(text: str, start: int, opening: str, closing: str) -> int | None:
    """Find the end of a balanced bracket/destination, respecting escapes."""
    depth = 0
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


def reference_spans(markdown: str) -> list[tuple[int, int]]:
    """Return prose @refs, excluding code, URLs, HTML, and existing links.

    This is a deliberately small Markdown scanner, not a Markdown renderer.
    Positions are character offsets in the supplied text.
    """
    masked = list(markdown)
    definitions: set[str] = set()
    offset = 0
    # Blockquote markers are containers, including around fenced code.
    lines = markdown.splitlines(keepends=True)
    prose_lines: list[str] = []
    indented_lines: set[int] = set()
    list_indents: list[int] = []
    for number, line in enumerate(lines):
        prose = re.sub(r"^(?: {0,3}>[ \t]?)+", "", line).expandtabs(4)
        indent = len(prose) - len(prose.lstrip(" "))
        if prose.strip():
            while list_indents and indent < list_indents[-1]:
                list_indents.pop()
            marker = re.match(r"^ *(?:[-+*]|\d+[.)]) +", prose)
            base = list_indents[-1] if list_indents else 0
            if marker and indent < base + 4:
                list_indents.append(marker.end())
        base = list_indents[-1] if list_indents else 0
        if indent >= base + 4:
            indented_lines.add(number)
        prose_lines.append(prose[base:] if indent >= base else prose)
    fences = fenced_line_numbers("".join(prose_lines))
    for number, line in enumerate(lines):
        prose = prose_lines[number]
        definition = re.match(r"^ {0,3}\[([^\]]+)\]:", prose)
        if definition and number not in fences:
            definitions.add(" ".join(definition[1].casefold().split()))
        if number in fences or definition or number in indented_lines:
            masked[offset:offset + len(line)] = " " * len(line)
        offset += len(line)
    text = "".join(masked)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == "`":
            run = re.match(r"`+", text[index:])[0]
            close = re.search(rf"(?<!`){run}(?!`)", text[index + len(run):])
            index = index + len(run) + close.end() if close else index + len(run)
            continue
        if char == "<":
            html = HTML_RE.match(text, index)
            if html:
                index = html.end()
                continue
        url = URL_RE.match(text, index) if char in "hHmM" else None
        if url:
            index = url.end()
            continue
        if char == "[":
            end = _closing(text, index, "[", "]")
            if end:
                destination_end = None
                if text[end:end + 1] == "(":
                    destination_end = _closing(text, end, "(", ")")
                elif text[end:end + 1] == "[":
                    label_end = _closing(text, end, "[", "]")
                    if label_end:
                        label = text[end + 1:label_end - 1] or text[index + 1:end - 1]
                        if " ".join(label.casefold().split()) in definitions:
                            destination_end = label_end
                elif " ".join(text[index + 1:end - 1].casefold().split()) in definitions:
                    destination_end = end
                if destination_end:
                    index = destination_end
                    continue
        if char == "@":
            match = REFERENCE_RE.match(text, index)
            if match:
                spans.append(match.span())
                index = match.end()
                continue
        index += 1
    return spans


def extract_references(markdown: str) -> list[str]:
    """Distinct references in first-appearance order; preserve written case."""
    seen: set[str] = set()
    refs: list[str] = []
    for start, end in reference_spans(markdown):
        ref = markdown[start:end]
        if ref.casefold() not in seen:
            seen.add(ref.casefold())
            refs.append(ref)
    return refs
