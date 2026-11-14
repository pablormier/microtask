# Dashboard JavaScript

`tools/taskweb.py::load_js` defines script order and embeds the scripts in the
local server dashboard. JavaScript behavior
tests use that same loader.

- `markdown.js`: Markdown rendering and card references.
- `card_helpers.js`: pure calculations for dates, durations
  and metadata values. These functions receive their inputs explicitly and do
  not access Alpine, the DOM, storage or the network. They do not mutate cards.
- `draft_store.js`: workspace draft requests, current-format browser recovery copies,
  revision/hash guards and per-ID autosave queues. Each instance owns
  its draft list; timers and pending requests stay private in its closure.
- `capture_composer.js`: capture input, dirty/recovery state, validation and
  save/discard workflows. It receives the shared draft store and narrow board
  adapters when an action runs.
- `picker.js`: state and keyboard navigation for searchable option lists, shared
  by the card editor (project, topic, tags) and the toolbar filter chips. The host
  supplies `pickerRows`, `choosePickerRow`, `pickerMulti` and `pickerSelected`.
- `metadata_editor.js`: shared `cardMetadataEditor` Alpine component used by
  both capture and card detail.
- `property_editor.js`: selected-card property values, dirty baseline, conflict
  state, validation and patch creation.
- `content_editor.js`: inline content input, local persistence, preview, save
  state and editor errors.
- `filters_query.js`: toolbar filter facets, URL query sync, and collection query builder.
- `draft_coordinator.js`: draft store wiring, editor context adapters, and draft recovery orchestration.
- `card_detail.js`: card detail selection, history navigation, preview rendering, hover popovers, and dialog focus trapping.
- `board.js`: Alpine `taskBoard()` component, lifecycle, polling, grouping, and payload application.

Keep policy and clock access at the component boundary. Metadata initialization
and reset share `normalizeMetadataValue`; it copies tags so editors cannot
mutate the source card through a shared array.

`createDraftStore` receives live workspace context, target lookup and capture
metadata adapters. Its status, warning and save callbacks report persistence
results; they do not open editors or apply card payloads. Editors use `schedule`,
`save`, `flush`, `finish`, `discard` and `recover`; the board applies successful
finish responses through `applyPayload` and owns selection reconciliation.
`finishContentDraft` rebases pending properties only when their baseline matches
the server's saved properties; the content editor simply returns its save result. Keep
draft storage and request bookkeeping out of the board. Store tests run without
the DOM or Alpine; UI tests use the complete assembled scripts.

The initial page contains only the API token and workspace identity. `loadWorkspace`
loads cards and policy through `/api/items`, then drafts through `/api/drafts`,
and resolves the initial card URL after the view mounts. Failed card loading
keeps the workspace hidden and offers Retry. `applyPayload` is the shared data
boundary for initial loading, refresh and mutations.

Focus, visibility and minute polling share one automatic refresh guard. It skips
hidden or unready pages, pending searches, collection requests still in flight,
and the five seconds after a successful current collection refresh. Explicit
searches and mutations bypass this guard; failed or stale responses do not extend
the grace period.

`/api/items` accepts the query fields of `scripts/card_query.py` as parameters:
text, status, priority, type, project, topic (the path and everything under
it), tag, due (`overdue`, `today`, `week`, `any`, `none`), `updated_since` and
`updated_before` (a date or an age such as `3m`), `hidden=include`,
`needs_review=true`, and the other fields `mt list` accepts. List fields may
repeat (`status=doing&status=waiting`): a card matches any value of a field and
must match every field. The page address uses the same names, so `mt web`
filters and bookmarks open the board as filtered; parameters no control shows
stay in the address and in requests. A bad parameter returns 400 with a message.
The page never selects cards itself: `tools/web_queries.py` derives each view's
query from the request's and returns ID arrays in `collections` (`tasks`,
`notes`, `archive`, `archive_tasks`, `archive_notes`, `open`, `deadlines`)
alongside card summaries, and the page renders them. Mutation requests send the
same query as JSON, and the client keeps a response's collections only if the
filters have not changed since it was sent. Summaries omit body, references and
reference spans; counts and filter choices still use the entire summary
collection. Changes to filters request new results after a short debounce.
Request generations reject stale successes and failures; failed queries retain
existing cards and offer Retry. Draft finish responses use the default query
and trigger a refresh when filters differ.

Opening a card requests `/api/item?id=...` for its full content and resolved
references. Only the selected card retains details; collection refreshes reuse
those details when its hash and the reference targets are unchanged. A changed
card or reference target reloads the selected details, preserving pending editor
values and hash conflict checks. Detail request generations prevent late
responses from changing a newer selection or reopening a closed dialog. Loading
and failed content disable content editing and offer Retry. Resuming a content
draft waits for the target details before starting the editor.

There is no pagination or server cache yet: collection responses still contain
summaries for the entire workspace, and the server still discovers Markdown
files for requests. The smaller payload is preparation for bounded loading.

Collection views mount with `x-if` only while active. Their filters and grouping
stay in board state; card, capture and draft dialogs remain outside the collection
view lifecycle. Mutation handlers call `scripts/operations.py` in-process under
the shared mutation lock (`sync=False`, with `BackgroundSync` running
`command_sync` in-process afterwards) and return receipts; the HTTP boundary builds
one queried dashboard response after a successful action. Target lookup discovers
cards without constructing a dashboard. Serialization shares one policy snapshot
within a response; it does not cache policy across requests.

Cards have one logical Markdown body for both kinds. Property controls and
activity panels read frontmatter properties. `markdown.js` renders bodies,
Markdown properties, and comments without parsing card headings or decoding
storage wrappers. The content editor and standalone preview use the same body
renderer as saved cards; draft bodies contain no serialized card frontmatter.
