---
id: 2026-01-03-140000-fix-login-timeout-bug
title: Fix login timeout bug
kind: task
status: active
priority: high
type: bug
project: null
topic: work
due: '2026-01-04'
due_kind: deadline
created: '2026-01-03T13:30:00'
updated: '2026-01-04T09:10:00'
archived: false
tags:
- bug
- auth
next_action: Reproduce the timeout with a throttled browser session and capture the failing request.
comments:
- at: '2026-01-04T09:10:00'
  text: Suspect the refresh token call is not retried; check the auth middleware first.
---
Users on slow connections get logged out after 30 seconds; the session timeout must tolerate long page loads.

## Notes

Some **bold** notes for active.

| a | b |
| - | - |
| 1 | $x^2$ |