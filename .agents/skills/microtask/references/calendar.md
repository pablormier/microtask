# Calendar setup

Focus and weekly planning can use calendars connected to your agent app
(also called a harness). Microtask stores preferences; it does not connect to
calendar services, authenticate, or fetch events itself. Calendar access is
optional. Without it, agents continue planning from cards.

## Connect your calendar

Use your app's calendar connector, plugin, MCP server, or an authenticated
calendar CLI available to the agent. Follow the app and integration's own
instructions to connect the intended account. For example:

- [Claude Code: connect tools with MCP](https://code.claude.com/docs/en/mcp)
- [VS Code: add and use MCP servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers)

These explain how to connect tools; the chosen calendar integration supplies
its own authentication instructions. In another app, use its official
connector or tool setup documentation. There is no universal calendar tool
name or microtask command for connecting an account. Prefer read-only calendar
access where the integration supports it. Keep credentials in that integration,
not in microtask settings.

Ask your agent:

> List the calendars available through my connected calendar tools, showing
> their names, IDs, and accounts. Help me choose which to use for planning.

Calendar names help you choose; save the actual IDs returned by the tool.
Do not automatically select every calendar or assume the primary calendar
contains all your commitments. If the agent cannot list calendars, use the
integration's calendar picker or ID documentation. If no tool is connected,
the agent should explain the missing capability and help find setup instructions
for your app, rather than guess IDs or report an empty schedule.

## Save preferences

Create or edit `settings.local.yaml` in the selected microtask workspace,
preserving any unrelated settings:

```yaml
calendar:
  include:
    - id: your-work-calendar-id
    - id: your-personal-calendar-id
  guidance: >
    Location entries provide context.
    Holiday entries do not necessarily mean I am unavailable.
```

Replace the placeholder IDs with the discovered IDs. A calendar can contain
both commitments and contextual events; no calendar category is required.

- `include` is the allowlist for automatic focus/planning reads. Entries can
  be ID strings or mappings with a required `id`. Optional `account` and
  `provider` strings qualify IDs when needed; use values that identify the
  account and service to your connected tool, not guessed tool names.
- `guidance` is optional text about interpreting events. It can span multiple
  lines. It does not grant permission to modify events or cards.
- Missing `calendar`, `calendar: null`, or an empty `include` disables automatic
  calendar lookup. There is no separate `enabled` setting.

Shared defaults can go in `settings.yaml`. Local settings override shared
settings recursively, and local lists replace shared lists rather than append
to them. To disable inherited calendars locally, use:

```yaml
calendar:
  include: []
```

An empty local mapping inherits shared settings. Use `guidance: ""` to clear
inherited guidance. Neither settings file is touched by `mt update`; `mt sync` omits
`settings.local.yaml`. This omission does not prevent other Git commands from
tracking or publishing it.

You can ask the agent to do this for you:

> Use my Work and Personal calendars for planning, and save that preference
> locally. Treat location entries as context.

When asked to configure calendars, agents should read the effective settings,
discover IDs through the available integration, and clarify ambiguous selections
before saving. Edit only the requested settings in the selected workspace's
local file, preserve unrelated YAML, and validate afterward. Read the existing
settings files for this edit; ordinary planning uses the merged CLI output.
Use the same workspace for the commands below and for the local settings edit.
Do not edit the bundled skills for personal calendar preferences.

## Verify

Check the effective settings and validate the workspace.

```sh
./mt settings --json
./mt validate
```

The effective `calendar` object contains `include` entries normalized to
mappings and `guidance` text (empty by default). Invalid field names, entry
shapes, and guidance types are rejected. Settings validation checks configuration
structure, not whether the calendars exist or can be accessed.

Then ask:

> Read my configured calendars for the rest of today in my timezone. Tell me
> whether each was accessible, without changing any events.

This checks actual access separately from YAML validation. Focus reads the
remainder of today; weekly planning reads the planning week or requested period.
Agents use event availability and your guidance without treating every gap as
working time or every all-day event as time off. If configured access fails or
only some calendars can be read, they state the limitation and continue from
cards. Ordinary planning skips unconfigured calendars without prompting setup.
