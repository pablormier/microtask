---
id: example-task
title: Example task
kind: task
status: active
priority: none
type: task
created: '2026-01-01T09:00:00'
updated: '2026-01-01T09:00:00'
next_action: |
  Optional first step.
processing_instructions: |
  Optional guidance for agents working on this card.
comments:
  - at: '2026-01-01T09:00:00'
    text: A comment belongs in frontmatter, not the Markdown body.
time_entries:
  - session_id: 0123456789abcdef0123456789abcdef
    start: '2026-01-01T09:00:00'
    end: '2026-01-01T09:30:00'
    elapsed_seconds: 1800
---
The Markdown body begins immediately after the frontmatter. Its headings, tables,
code fences, and whitespace are document content and have no application meaning.
