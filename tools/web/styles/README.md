# Web UI component contracts

`theme.css` owns palette and size tokens. `components.css` owns shared control,
badge, surface, and dialog appearance. `layout.css`, `board.css`, and
`views.css` own placement, wrapping, and content arrangement.
The generator loads theme, components, then layout/view styles, so responsive
view adaptations override component defaults. Component modifiers use the same
selector specificity as their base and follow it in the stylesheet.

- **Buttons:** use `button.mt-button`; add one of `mt-button-primary`,
  `mt-button-quiet`, or `mt-button-danger` when needed. Add
  `mt-button-compact` for dense areas and `mt-button-icon` for icon-only
  controls. Native `disabled` and `aria-busy` carry state.
- **Button hierarchy:** the default white fill with a light border identifies
  routine actions (Move, Done, Start/Stop, Archive, filters, and inline metadata edits).
  Reserve solid blue for the main create/save/confirm action in a panel;
  use quiet buttons for Back and Close. Use pale red for actions that discard
  content, including draft dismissal and deadline removal. Disabled actions
  use a muted neutral fill, including disabled primary actions.
- **Selection:** segmented options sit on a gray track; the selected option is
  a raised white segment, distinct from solid blue action buttons. Quiet
  segmented choices (layout, text size) use a soft blue fill. Segments show
  their own focus ring, so `.mt-segmented` suppresses Pico's group ring.
  Preserve `aria-pressed` and `aria-expanded` states.
- **Toolbar:** brand and view tabs on the left; display choices, then actions
  (Filters, Clear filters, Resume, and the primary New last) on the right. The
  Filters toggle counts active filters. At narrow widths the view tabs move to
  their own horizontally scrolling row.
- **Filter chips:** the Filters toggle shows a row below the title row (not on
  Drafts): search, then one `mt-filter-chip` button per facet and a `...` menu.
  The row is a `search` element, never a
  `role="group"`, so Pico's group styling does not apply. An empty chip shows
  its name; an active chip is tinted, summarizes its values, and has a sibling
  reset button (not a nested button). A chip that does not apply on the current
  tab is dimmed with a tooltip. The row wraps at narrow widths, where each
  dropdown spans the row below it so none runs off screen.
- **Icons:** use the shared 16px stroke SVG style for New (plus),
  Back, and Close. Keep other actions text-only; icons aid recognition of these
  common controls rather than indicate importance. Keep visible text except
  on Close, which must have an accessible label. Do not mix emoji or text glyphs
  into control icons.
- **Card detail:** tasks and notes share a stable header, content column, and
  property sidebar. The ordinary Markdown body has one Edit content action with inline Write and
  Preview modes; its recovery copies use workspace drafts. Properties remain
  editable in place, with Reset and Save changes visible for pending changes.
  Keep the card width and content visible while changing properties.
  The header stays pinned while the dialog scrolls; status is a tinted pill.
  In the sidebar, properties are label/value rows whose fields show a border
  only on hover and focus; Save changes stays pinned to the panel bottom.
  The sidebar editor is keyed by card so it re-initializes on navigation. Keep Archive/Hide in the secondary menu, active ownership beside status,
  and created/updated dates in the footer. Stack the content and
  properties on narrow screens.
- **Property forms:** reuse the same metadata editor for both kinds. Use
  `mt-field-property` for 36px controls and helper/errors below inputs. Reveal
  time and date meaning only after a date is set. Capture reuses this editor above the writing area, with
  Status, Priority, and Date immediately visible. Type and organization fields
  live under More properties during creation and stay expanded during editing.
  Use the shared segmented control for Note/Task selection in the composer
  header, before the title. The title is a large borderless heading input;
  optional sections are quiet disclosures rather than boxed rows.
- **Pickers:** Project, Topic, and Tags accept any text but choose from a
  `mt-picker` list that always shows every option (most-used first, with
  counts), a Create row for new text, and Clear at the top when set. Tags are
  chips with a remove button; a comma or Enter adds one. Enter picks only a
  highlighted row; Escape closes the list before the dialog. Wrap pickers in
  a `div`, not a `label`, so option clicks do not refocus the input.
  Topic rows are the derived topic tree (indented, counts include descendants);
  typing `a/` lists that folder's descendants and a bare word matches any
  segment. The toolbar filter chips reuse the picker list inside a menu panel
  (`mt-card-menu-items`), as an inline list (`mt-picker-inline`), with a narrowing field for Project, Topic, and Tags;
  multi-value lists keep it open and tick values. One selected topic also shows
  as a `mt-topic-crumbs` breadcrumb above the board, and cards show their path
  as plain `mt-card-topic` text (not a nested button, hidden when grouped by topic).
- **Fields:** use `mt-field` on inputs, selects, and textareas. Use
  `mt-field-compact` for dense controls. Wrapper labels set widths and
  label/help/error placement.
- **Checkbox filters:** wrap a native checkbox in `label.mt-checkbox-filter`.
  The checkbox remains the state source. Include hidden and Needs review only
  live in the toolbar's `...` overflow menu.
- **Segmented choices:** use a `mt-segmented` container and `mt-segment`
  buttons with `aria-pressed`. Use `mt-segmented-quiet` for text-size choices.
- **Layout groups:** add `mt-layout-group` to a `role="group"` wrapper when
  the role names related controls but the wrapper itself should stay visually
  plain. Segmented choices use `mt-segmented` instead.
- **Badges:** use `mt-badge` with one tone (`mt-badge-neutral`, `-accent`,
  `-success`, `-warning`, or `-danger`). Add `mt-badge-square` when the content
  is a short kind or status label. Priority renders as a colored dot and label
  without a fill; tags are neutral chips. Use sentence case, not uppercase;
  uppercase is reserved for small section labels.
- **Card activity:** cards with comments show a muted speech-bubble icon and
  count at the trailing edge of the tag row; zero comments shows nothing.
  The count is an indicator, not a control, with a screen-reader label.
- **Card urgency:** overdue and due-today cards keep a white surface and add a
  3px inset side rail; the due badge carries the date color.
- **Feedback:** action and capture results appear in the shared bottom toast
  (`notify(text, error)`), never as inline status lines. Confirmations fade
  after a few seconds; errors stay until dismissed. Keep inline messages for
  field validation and in-composer recovery warnings.
- **Surfaces:** add `mt-surface` to cards, panels, and rows. Dialogs use
  `mt-dialog-backdrop` and `mt-dialog`; view classes then set their size and
  internal layout.

Pico 2.1.1 is retained in the lower-priority `vendor` layer. New controls must
declare their component class so appearance does not depend on their parent or
on Pico's global native-element rules. View styles should not set descendant
button or field appearance.
The `--pico-*` bridge in `theme.css` makes Pico's native form rendering and
interaction states follow MicroTask's semantic tokens. Change `--mt-*` tokens
to restyle the UI; retain Pico's responsive font scale for the text-size choices.
Interactive cards explicitly retain document alignment and selectable text
despite Pico's default presentation for `role="button"`.

Card Markdown never defines application panels. Comments and time history use
collapsible panels populated from frontmatter records, outside the body
container. Wide tables and code scroll inside the content column at narrow
widths. An unfinished body fence must not affect properties or activity.
