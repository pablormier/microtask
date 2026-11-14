// Card views (Tasks, Notes, Archive) share Kind and Group by; Deadlines only groups.
const CARD_GROUPS = [
  ["none", "None"], ["status", "Status"], ["priority", "Priority"], ["project", "Project"],
  ["topic", "Topic"], ["tag", "Tag"], ["type", "Type"], ["due_kind", "Due kind"]
];
const TASK_ONLY_GROUPS = ["status", "type"];
const DEADLINE_GROUPS = ["none", "due_kind", "priority", "status", "type", "project", "topic"];
const CARD_KINDS = ["task", "note", "all"];
// Tasks and Notes show one kind; only Archive has a Kind setting.
const TAB_KINDS = { tasks: "task", notes: "note" };
const VIEW_DEFAULTS = {
  tasks: { group: "status" },
  notes: { group: "none" },
  archive: { kind: "task", group: "none" },
  deadlines: { group: "due_kind" }
};
const VIEW_SETTINGS_KEY = "mt-view-settings";
// Board filters live in the page address so a reload or bookmark keeps them. The address and
// the requests use the server's query field names (scripts/card_query.py); the page does not
// select cards itself, it only builds the query and shows the collections the server returns.
const PAGE_PARAMS = ["layout"];
const FILTER_LISTS = ["status", "priority", "type", "project", "topic", "tag", "due"];

// Every facet but Updated holds a list of values: a card matches any of them, and all
// facets must match. Updated holds the key of one UPDATED_RANGES option, or "".
function defaultFilters() {
  return { text: "", status: [], priority: [], type: [], project: [], topic: [], tag: [], due: [], updated: "" };
}

// Filter chips in toolbar order, as [key, label].
const FILTER_FACETS = [["status", "Status"], ["priority", "Priority"], ["type", "Type"], ["project", "Project"],
  ["topic", "Topic"], ["tag", "Tags"], ["due", "Due"], ["updated", "Updated"]];
const SEARCHABLE_FILTERS = ["project", "topic", "tag"];
const FILTER_OPTIONS = {
  due: [["overdue", "Overdue"], ["today", "Today"], ["week", "Next 7 days"], ["any", "Has due"], ["none", "No due"]],
  updated: [["2w", "Last 2 weeks"], ["1m", "Last month"], ["3m", "Last 3 months"], ["1y", "Last year"],
    ["older-3m", "Older than 3 months"], ["older-1y", "Older than a year"]]
};
// The query each Updated option sends; the server turns an age into a date.
const UPDATED_RANGES = {
  "2w": { updated_since: "2w" }, "1m": { updated_since: "1m" }, "3m": { updated_since: "3m" },
  "1y": { updated_since: "1y" }, "older-3m": { updated_before: "3m" }, "older-1y": { updated_before: "1y" }
};
const ARCHIVE_COLLECTIONS = { task: "archive_tasks", note: "archive_notes", all: "archive" };

// Add one parameter per value, so lists repeat the name instead of joining.
function appendParams(params, name, value) {
  for (const item of [].concat(value)) params.append(name, item);
}

function cardGroupOptions(kind) {
  return CARD_GROUPS.filter(([value]) => kind !== "note" || !TASK_ONLY_GROUPS.includes(value))
    .map(([value, label]) => ({ value, label }));
}

// Fill missing or invalid saved settings from each tab's defaults.
function normalizeViewSettings(saved) {
  const settings = {};
  for (const [tab, defaults] of Object.entries(VIEW_DEFAULTS)) {
    const entry = saved && typeof saved[tab] === "object" && saved[tab] ? saved[tab] : {};
    const kind = tab === "archive" ? (CARD_KINDS.includes(entry.kind) ? entry.kind : defaults.kind) : TAB_KINDS[tab];
    const groups = tab === "deadlines" ? DEADLINE_GROUPS : cardGroupOptions(kind).map(option => option.value);
    const group = [entry.group, defaults.group].find(value => groups.includes(value)) || "none";
    settings[tab] = tab === "archive" ? { kind, group } : { group };
  }
  return settings;
}

// Toolbar filter facets, URL query sync, and collection query builder.
function createFilterController() {
  return {

    // Selected values of a facet; empty when it is unset ("", or Updated's "all").
    filterValues(key) {
      const value = this.filters[key];
      return (Array.isArray(value) ? value : [value]).map(item => String(item).trim()).filter(Boolean);
    },

    activeFilterCount() {
      return Object.keys(this.filters).filter(key => this.filterValues(key).length).length
        + Object.keys(this.extraQuery).length + (this.showHidden ? 1 : 0) + (this.needsReviewOnly ? 1 : 0);
    },

    filterHas(key, name) {
      return this.filterValues(key).some(value => value.toLowerCase() === name.toLowerCase());
    },

    toggleFilter(key, name) {
      const values = this.filterValues(key);
      this.filters[key] = this.filterHas(key, name)
        ? values.filter(value => value.toLowerCase() !== name.toLowerCase())
        : [...values, name];
    },

    clearFilter(key) {
      this.filters[key] = defaultFilters()[key];
    },

    // The server ignores Status on Notes and Archive (see tools/web_queries.py).
    filterFacets() {
      const statusIgnored = this.mode === "notes" || this.mode === "archive";
      return FILTER_FACETS.map(([key, label]) => {
        const names = this.filterValues(key);
        // Topics show their full path; other values show their option label.
        const labels = new Map(names.length && key !== "topic" ? this.filterChoices(key).map(row => [row.name, row.label]) : []);
        const shown = names.map(name => labels.get(name) || name);
        return {
          key, label, search: SEARCHABLE_FILTERS.includes(key), multi: this.pickerMulti(key), active: names.length > 0,
          summary: !names.length ? label : label + (names.length > 2 ? " · " + names.length : ": " + shown.join(", ")),
          note: key === "status" && statusIgnored ? "Status does not filter " + (this.mode === "notes" ? "notes" : "the archive") : ""
        };
      });
    },

    // Every option of a facet, plus any selected value no card uses (say, from a link),
    // so each selection has a row to untick.
    filterChoices(key) {
      const rows = this.facetOptions(key);
      const known = new Set(rows.map(row => row.name.toLowerCase()));
      return rows.concat(this.filterValues(key).filter(name => !known.has(name.toLowerCase()))
        .map(name => ({ kind: "option", name, label: name, depth: 0, count: 0 })));
    },
    facetOptions(key) {
      if (key === "topic") return topicPickerRows(this.topicTree, "", { create: false });
      if (key === "project") return usageChoices(this.projects, this.workspaceUsage.project || {});
      if (key === "tag") return usageChoices(this.workspaceTags, this.workspaceUsage.tag || {});
      const options = FILTER_OPTIONS[key] || {
        status: this.statuses.map(name => [name, this.statusText(name)]),
        priority: this.priorities.map(name => [name, this.priorityLabel(name)]),
        type: this.types.map(name => [name, name])
      }[key];
      return options.map(([name, label]) => ({ kind: "option", name, label, count: 0 }));
    },

    // The toolbar filters use the shared picker: lists keep it open and toggle values, Updated closes.
    pickerMulti(field) {
      return field !== "updated";
    },
    pickerSelected(field, row) {
      return this.filterHas(field, row.name);
    },
    pickerRows(field) {
      const query = this.picker.field === field ? this.picker.query.trim() : "";
      const lower = query.toLowerCase();
      // A typed topic narrows the tree by path segments; other lists narrow by label.
      const rows = field === "topic" && query
        ? topicPickerRows(this.topicTree, query, { create: false })
        : this.filterChoices(field).filter(row => row.label.toLowerCase().includes(lower));
      if (!query && this.filterValues(field).length) rows.unshift({ kind: "clear", name: "", label: "", depth: 0, count: 0 });
      return rows;
    },
    choosePickerRow(field, row) {
      if (!row) return;
      if (row.kind === "clear") this.clearFilter(field);
      else if (this.pickerMulti(field)) return this.toggleFilter(field, row.name);
      else this.filters[field] = row.name;
      this.closePicker();
    },
    toggleFilterMenu(key, event) {
      if (this.pickerOpen(key)) return this.closePicker();
      const menu = event.currentTarget.closest(".mt-filter");
      this.openPicker(key);
      this.$nextTick(() => menu.querySelector("input")?.focus());
    },

    // The breadcrumb shows one selected topic; several topics show only in the chip.
    topicCrumbs() {
      const parts = this.filters.topic.length === 1 ? topicSegments(this.filters.topic[0]) : [];
      return parts.map((label, index) => ({ label, path: parts.slice(0, index + 1).join("/") }));
    },

    readFilterLocation() {
      let params;
      try { params = new URLSearchParams(location.search); } catch (_) { return; }
      this.applyFilterParams(params);
    },

    // Set the filter controls from query parameters in the server's field names.
    applyFilterParams(params) {
      const filters = defaultFilters();
      const extra = {};
      this.showHidden = false;
      this.needsReviewOnly = false;
      for (const name of new Set(params.keys())) {
        if (PAGE_PARAMS.includes(name)) continue;
        const values = params.getAll(name).map(value => (name === "topic" ? topicSegments(value).join("/") : value).trim()).filter(Boolean);
        if (name === "text") filters.text = values[values.length - 1] || "";
        else if (FILTER_LISTS.includes(name)) filters[name] = values;
        else if (name === "needs_review" && values[values.length - 1] === "true") this.needsReviewOnly = true;
        else if (name === "hidden" && values[values.length - 1] === "include") this.showHidden = true;
        else if (values.length) extra[name] = values;
      }
      // One Updated option when the address holds exactly its range; any other date range stays as is.
      const range = Object.keys(extra).filter(name => name.startsWith("updated_")).sort().join();
      const option = Object.entries(UPDATED_RANGES).find(([, query]) => range === Object.keys(query).join() &&
        Object.entries(query).every(([name, value]) => extra[name].join() === value));
      if (option) {
        filters.updated = option[0];
        for (const name of Object.keys(UPDATED_RANGES[option[0]])) delete extra[name];
      }
      this.filters = filters;
      this.extraQuery = extra;
    },

    writeFilterLocation() {
      let params;
      try { params = new URLSearchParams(location.search); } catch (_) { return; }
      for (const name of Array.from(new Set(params.keys())).filter(name => !PAGE_PARAMS.includes(name))) params.delete(name);
      for (const [name, value] of Object.entries(this.collectionQuery())) appendParams(params, name, value);
      const search = params.toString();
      const target = location.pathname + (search ? "?" + search : "") + location.hash;
      if (target !== location.pathname + location.search + location.hash) history.replaceState(history.state, "", target);
    },

    clearFilters() {
      this.showHidden = false;
      this.needsReviewOnly = false;
      this.extraQuery = {};
      this.filters = defaultFilters();
    },

    // The query in the server's field names, with only the fields that are set.
    collectionQuery() {
      const query = { ...this.extraQuery };
      for (const key of FILTER_LISTS) if (this.filterValues(key).length) query[key] = this.filterValues(key);
      if (this.filters.text.trim()) query.text = this.filters.text.trim();
      Object.assign(query, UPDATED_RANGES[this.filters.updated]);
      if (this.showHidden) query.hidden = "include";
      if (this.needsReviewOnly) query.needs_review = "true";
      return query;
    },

    cardIdInLocation() {
      if (!location.hash.startsWith("#mt-card-")) return null;
      try { return decodeURIComponent(location.hash.slice("#mt-card-".length)); }
      catch (_) { return null; }
    },

    // Cards the server must send even when the filters leave them out: the open card and its trail.
    includedCardIds() {
      return Array.from(new Set([this.selectedId, this.previousCardId, this.cardIdInLocation()].filter(Boolean)));
    },

  };
}
