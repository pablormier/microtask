"""Small, dependency-free terminal capture form. Writes belong to tasklib."""
from __future__ import annotations

import os
import select
import shutil
import sys
import termios
import tty


def display(value: object) -> str:
    """Keep pasted content and policy labels from controlling the terminal."""
    return " ".join("".join(c for c in str(value) if c.isprintable() or c == "\n").split())


def choose(label: str, options: list[str], selected: int = 0) -> int:
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    try:
        tty.setcbreak(descriptor, termios.TCSANOW)
        print(f"{label} (↑/↓ or ←/→, Enter to accept; Esc to cancel)")
        print(" / ".join(display(option).split(":", 1)[0] for option in options))
        while True:
            width = max(10, shutil.get_terminal_size().columns - 3)
            print("\r\033[2K› " + display(options[selected])[:width], end="", flush=True)
            key = os.read(descriptor, 1).decode("ascii", errors="replace")
            if key in ("\r", "\n"):
                print()
                return selected
            if key in ("", "\x03", "\x04"):
                raise EOFError
            if key == "\x1b":
                # Read escape sequences without swallowing the next prompt's input.
                sequence = ""
                for _ in range(2):
                    if not select.select([sys.stdin], [], [], 0.1)[0]:
                        break
                    sequence += os.read(descriptor, 1).decode("ascii", errors="replace")
                if sequence in ("[A", "[D", "OA", "OD"):
                    selected = (selected - 1) % len(options)
                elif sequence in ("[B", "[C", "OB", "OC"):
                    selected = (selected + 1) % len(options)
                elif not sequence:
                    raise EOFError
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def ask(label: str, current: str = "") -> str:
    suffix = f" [{display(current)}]" if current else ""
    value = input(f"{label}{suffix}: ").strip()
    return current if not value else ("" if value == "-" else value)


def read_text(label: str = "Text") -> str:
    """Read canonical terminal lines; Ctrl+D also finishes a partial last line."""
    descriptor = sys.stdin.fileno()
    previous = termios.tcgetattr(descriptor)
    settings = termios.tcgetattr(descriptor)
    settings[3] |= termios.ICANON | termios.ECHO | termios.ISIG
    settings[6][termios.VEOF] = b"\x04"
    chunks: list[bytes] = []
    print(f"\n  {label}")
    try:
        termios.tcsetattr(descriptor, termios.TCSANOW, settings)
        print("  Enter: new line · Ctrl+D: finish · Ctrl+C: cancel")
        while True:
            print("> ", end="", flush=True)
            chunk = os.read(descriptor, 65536)
            chunks.append(chunk)
            if not chunk.endswith(b"\n"):
                print()
                # A newline submits a line rather than becoming a compulsory
                # trailing newline in the saved body. Interior blank lines stay.
                text = b"".join(chunks).decode(sys.stdin.encoding or "utf-8")
                return text[:-1] if text.endswith("\n") else text
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, previous)


def review(fields: dict, cfg: dict) -> None:
    width = min(64, shutil.get_terminal_size().columns)
    print("\n  Review " + fields["kind"])
    print("  " + "─" * max(4, width - 4))
    print("  Title     " + display(fields["title"] or "Automatic from text"))
    print("  Text")
    for line in fields["text"].splitlines():
        print("  > " + display(line))
    if fields.get("instructions", "").strip():
        print("  Processing instructions")
        for line in fields["instructions"].splitlines():
            print("  > " + display(line))
    if fields["kind"] == "task":
        values = [
            ("Status", cfg["statuses"][fields["status"]]),
        ]
    else:
        values = []
    values += [("Priority", fields["priority"]), ("Date", fields["due"] or "No date")]
    if fields["due"]:
        values.append(("Meaning", fields["due_kind"]))
    if fields["kind"] == "task":
        values.append(("Type", fields["task_type"]))
    values += [("Project", fields["project"] or "—"), ("Topic", fields["topic"] or "—"),
               ("Tags", ", ".join(fields["tags"]) or "—"),
               ("Review", "Yes" if fields["needs_review"] else "No"),
               ("Hidden", "Yes" if fields["hidden"] else "No"),
               ("Archived", "Yes" if fields["archived"] else "No")]
    print()
    for label, value in values:
        print(f"  {label:<10}{display(value)}")
    print()


def enum(label: str, values: dict, current: str) -> str:
    keys = list(values)
    return keys[choose(label, [f"{key}: {display(values[key])}" for key in keys], keys.index(current))]


def collect(cfg: dict, defaults: tuple[str, str, str], text: str, edit_text, valid_due) -> dict | None:
    status, priority, task_type = defaults
    fields = dict(kind="task", text=text, title="", status=status, priority=priority,
                  task_type=task_type, project="", topic="", tags=[], due=None, due_kind=None,
                  instructions="", needs_review=False, hidden=False, archived=False)
    print("\n  microtask · Capture\n")
    print("  Enter accepts menu defaults · Ctrl+C cancels")
    print("  Use '-' to clear an optional text property.\n")
    fields["kind"] = ("task", "note")[choose("Kind", ["Task", "Note"])]
    if not fields["text"].strip():
        fields["text"] = read_text()
    while True:
        review(fields, cfg)
        actions = ["Create", "Edit text", "Title"]
        if fields["kind"] == "task":
            actions += ["Status"]
        actions += ["Priority", "Date", "Processing instructions", "More properties"]
        actions += ["Cancel"]
        action = actions[choose("Action", actions)]
        if action == "Cancel":
            return None
        if action == "Create":
            if fields["text"].strip():
                return fields
            print("Enter some text before creating the card.")
        elif action == "Edit text":
            fields["text"] = edit_text(fields["text"])
        elif action == "Title":
            fields["title"] = ask("Optional title", fields["title"])
        elif action == "Status":
            statuses = {key: value for key, value in cfg["statuses"].items() if key != "done"}
            fields["status"] = enum("Status", statuses, fields["status"])
        elif action == "Priority":
            fields["priority"] = enum("Priority", cfg["priorities"], fields["priority"])
        elif action == "Date":
            due = ask("Date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS; '-' clears)", fields["due"] or "")
            if due and not valid_due(due):
                print("Enter a valid date or local timestamp.")
                continue
            fields["due"] = due or None
            if due:
                kinds = cfg["due_kinds"]
                current = fields["due_kind"]
                options = ["Choose meaning…"] + [f"{key}: {display(value)}" for key, value in kinds.items()]
                index = choose("Date meaning (choose explicitly)", options,
                               list(kinds).index(current) + 1 if current in kinds else 0)
                while index == 0:
                    index = choose("Choose a date meaning", options)
                fields["due_kind"] = list(kinds)[index - 1]
            else:
                fields["due_kind"] = None
        elif action == "Processing instructions":
            fields["instructions"] = read_text("Processing instructions")
        elif action == "More properties":
            properties = (["Type"] if fields["kind"] == "task" else []) + [
                "Project", "Topic", "Tags", "Needs review", "Hidden", "Archived", "Back"
            ]
            prop = properties[choose("More properties", properties)]
            if prop == "Type":
                fields["task_type"] = enum("Type", cfg["types"], fields["task_type"])
            elif prop in ("Project", "Topic"):
                fields[prop.lower()] = ask(prop, fields[prop.lower()])
            elif prop == "Tags":
                fields["tags"] = list(dict.fromkeys(tag.strip() for tag in ask("Tags (comma-separated)", ", ".join(fields["tags"])).split(",") if tag.strip()))
            elif prop in ("Needs review", "Hidden", "Archived"):
                key = {"Needs review": "needs_review", "Hidden": "hidden", "Archived": "archived"}[prop]
                current = fields[key]
                fields[key] = bool(choose(prop, ["No", "Yes"], int(current)))
