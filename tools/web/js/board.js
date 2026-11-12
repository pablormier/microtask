const MT_CONFIG = JSON.parse(document.getElementById("mt-config").textContent);

function cardMetadataEditor(initial, createMode, prefix, config, fieldErrors, kind = "task") {
  config = config || {};
  const value = normalizeMetadataValue(initial, config);
  const due = String(value.due || "");
  const picker = createPicker();
  return {
    createMode: Boolean(createMode),
    cardKind: kind,
    morePropertiesOpen: false,
    prefix: prefix || "mt-card-metadata",
    identity: "",
    disabled: false,
    fieldErrors: fieldErrors && typeof fieldErrors === "object" ? fieldErrors : {},
    value,
    tagsText: formatMetadataTags(value.tags),
    dateValue: due.slice(0, 10),
    timeValue: due.includes("T") ? due.split("T")[1].slice(0, 5) : "",
    tagsEdited: false,
    dueEdited: false,
    priorities: config.task_priorities || {},
    types: config.task_types || {},
    dueKinds: config.task_due_kinds || {},
    openStatuses: config.task_statuses || [],
    statusLabels: config.status_labels || {},
    projects: Array.from(new Set(config.workspace_projects || [])).sort(),
    topicTree: Array.isArray(config.topic_tree) ? config.topic_tree : [],
    tags: Array.from(new Set([...(config.suggested_tags || []), ...(config.workspace_tags || [])])).sort(),
    usage: config.workspace_usage || {},
    // Project, topic, and tags share one searchable picker; only one is open at a time.
    ...picker,
    replaceValue(next) {
      this.value = normalizeMetadataValue(next, config);
      const due = String(this.value.due || "");
      this.tagsText = formatMetadataTags(this.value.tags);
      this.dateValue = due.slice(0, 10);
      this.timeValue = due.includes("T") ? due.split("T")[1].slice(0, 5) : "";
      this.tagsEdited = false;
      this.dueEdited = false;
    },
    syncIdentity(identity, sourceValue, errors) {
      const nextIdentity = String(identity || "");
      if (nextIdentity !== this.identity) {
        this.identity = nextIdentity;
        this.replaceValue(sourceValue);
      }
      this.fieldErrors = errors && typeof errors === "object" ? errors : {};
    },
    syncTags() {
      this.tagsEdited = true;
      this.value.tags = parseMetadataTags(this.tagsText);
    },
    syncDue() {
      this.dueEdited = true;
      if (!this.dateValue) {
        this.timeValue = "";
        this.value.due = null;
        this.value.due_kind = null;
        return;
      }
      this.value.due = this.dateValue + (this.timeValue ? "T" + this.timeValue + ":00" : "");
    },
    syncDueKind() {
      this.value.due_kind = this.dateValue ? (this.value.due_kind || null) : null;
    },
    copy() {
      return { ...this.value, tags: this.value.tags.slice() };
    },
    emitChange() {
      this.$dispatch("metadata-change", this.copy());
    },
    pickerChoices(field) {
      if (field === "topic") return topicPickerRows(this.topicTree, "", { create: false });
      const names = { project: this.projects, tags: this.tags }[field] || [];
      return usageChoices(names, this.usage[field === "tags" ? "tag" : field] || {});
    },
    pickerMatch(field, text) {
      if (field === "topic") {
        const path = normalizeTopicPath(text).toLowerCase();
        return path ? this.topicTree.find(node => node.path.toLowerCase() === path) : null;
      }
      const lower = String(text || "").trim().toLowerCase();
      return lower ? this.pickerChoices(field).find(choice => choice.name.toLowerCase() === lower) : null;
    },
    pickerRows(field) {
      const query = this.picker.field === field ? this.picker.query.trim() : "";
      if (field === "topic") {
        const rows = topicPickerRows(this.topicTree, query);
        if (!query && this.value.topic) rows.unshift({ kind: "clear", name: "", label: "", depth: 0, count: 0 });
        return rows;
      }
      const lower = query.toLowerCase();
      const chosen = field === "tags" ? this.value.tags : [];
      const rows = this.pickerChoices(field)
        .filter(choice => !chosen.includes(choice.name) && (!lower || choice.name.toLowerCase().includes(lower)));
      const known = this.pickerMatch(field, query) || chosen.some(tag => tag.toLowerCase() === lower);
      if (query && !known) rows.push({ kind: "create", name: query, count: 0 });
      if (field !== "tags" && !query && this.value[field]) rows.unshift({ kind: "clear", name: "", count: 0 });
      return rows;
    },
    pickerMulti(field) {
      return field === "tags";
    },
    pickerSelected(field, row) {
      return row.name === this.value[field];
    },
    setPickerQuery(field, text) {
      if (field === "tags" && String(text).includes(",")) {
        // A comma (typed or pasted) completes each tag before it.
        const parts = String(text).split(",");
        text = parts.pop().trimStart();
        parts.forEach(part => this.addTag(part));
      }
      picker.setPickerQuery.call(this, field, text);
    },
    choosePickerRow(field, row) {
      if (!row) return;
      if (field === "tags") {
        this.addTag(row.name);
        return;
      }
      this.value[field] = row.kind === "clear" ? "" : row.name;
      this.closePicker();
      this.emitChange();
    },
    leavePicker(field) {
      // Typed text is kept when focus leaves, matching an existing value's spelling.
      if (!this.pickerOpen(field)) return;
      const query = this.picker.query.trim();
      const match = this.pickerMatch(field, query);
      if (query) this.choosePickerRow(field, match || { kind: "create", name: query });
      this.closePicker();
    },
    addTag(name) {
      const tag = String(name || "").trim();
      if (this.picker.field === "tags") {
        this.picker.query = "";
        this.picker.active = 0;
        this.picker.navigated = false;
      }
      if (!tag || this.value.tags.some(existing => existing.toLowerCase() === tag.toLowerCase())) return;
      const canonical = (this.pickerMatch("tags", tag) || { name: tag }).name;
      this.value.tags = [...this.value.tags, canonical];
      this.tagsText = formatMetadataTags(this.value.tags);
      this.tagsEdited = true;
      this.emitChange();
    },
    removeTag(tag) {
      this.value.tags = this.value.tags.filter(existing => existing !== tag);
      this.tagsText = formatMetadataTags(this.value.tags);
      this.tagsEdited = true;
      this.emitChange();
    },
  };
}

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
// Board filters live in the page address so a reload or bookmark keeps them.
const FILTER_PARAMS = { search: "q", status: "status", priority: "priority", type: "type",
  project: "project", topic: "topic", tag: "tag", due: "due", updated: "updated" };

// Every facet but Updated holds a list of values: a card matches any of them, and all
// facets must match. Updated holds one value, or "all".
function defaultFilters() {
  return { search: "", status: [], priority: [], type: [], project: [], topic: [], tag: [], due: [], updated: "all" };
}

// Filter chips in toolbar order, as [key, label].
const FILTER_FACETS = [["status", "Status"], ["priority", "Priority"], ["type", "Type"], ["project", "Project"],
  ["topic", "Topic"], ["tag", "Tags"], ["due", "Due"], ["updated", "Updated"]];
const SEARCHABLE_FILTERS = ["project", "topic", "tag"];
const FILTER_OPTIONS = {
  due: [["overdue", "Overdue"], ["today", "Today"], ["next7", "Next 7 days"], ["none", "No due"]],
  updated: [["2w", "Last 2 weeks"], ["1m", "Last month"], ["3m", "Last 3 months"], ["1y", "Last year"],
    ["older-3m", "Older than 3 months"], ["older-1y", "Older than a year"]]
};

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

function taskBoard() {
  return {
    tasks: [],
    collections: {},
    querySequence: 0,
    queryTimer: null,
    queryLoading: false,
    itemRequestsInFlight: 0,
    lastItemsRefreshAt: 0,
    queryError: "",
    loading: true,
    workspaceLoading: false,
    ready: false,
    loadError: "",
    statuses: [],
    statusLabels: {},
    viewLabels: {},
    priorityOrder: [],
    dueKinds: [],
    taskDefaults: { status: "inbox", priority: "none", type: "task" },
    taskStatuses: [],
    taskPriorityPolicy: {},
    taskTypePolicy: {},
    taskDueKindPolicy: {},
    suggestedTags: [],
    workspaceProjects: [],
    topicTree: [],
    workspaceTags: [],
    workspaceUsage: {},
    maxDoingTasks: 1,
    doneLimit: 10,
    focusContext: { candidates: [], alerts: [], followup_ids: [] },
    runningTimers: {},
    timerWarnHours: 8,
    timerDuration: "",

    apiToken: MT_CONFIG.api_token || "",
    workspaceKey: MT_CONFIG.workspace_key || "",
    demo: false,
    view: null,
    showYearProgress: true,
    showDeadlineProgress: true,
    deadlineProgressStart: "created",
    mode: "tasks",
    layout: "vertical",
    settings: normalizeViewSettings({}),
    textSize: "medium",
    filtersOpen: false,
    ...createPicker(),
    showHidden: false,
    priorities: [],
    types: [],
    projects: [],
    filters: defaultFilters(),
    needsReviewOnly: false,
    detailLoading: false,
    detailError: "",
    detailSequence: 0,
    selectedId: null,
    selected: null,
    previousCardId: null,
    previewHtml: "",
    propertiesHtml: "",
    moveDestination: "",
    contentEditor: createContentEditor(),
    commentText: "",
    actionMessage: "",
    actionError: false,
    toast: null,
    toastTimer: null,
    busy: false,
    propertyEditor: createPropertyEditor(),
    captureComposer: createCaptureComposer(),
    draftStorage: null,
    get drafts() { return this.getDraftStore().drafts; },
    set drafts(value) { this.getDraftStore().drafts = value; },
    draftSearch: "",
    selectedWorkspaceDraft: null,
    taskDraftOpen: false,
    draftWorkspaceStatus: "",
    draftBusy: false,
    syncWarning: "",
    detailReturnFocus: null,
    hoverCard: null,
    hoverPos: { top: 0, left: 0, placeAbove: false },
    hoverTimer: null,
    hoverTarget: null,
    now: Date.now(),
    nowAtLoad: Date.now(),

    // ---------------------------------------------------------------------------
    // Lifecycle & Document State
    // ---------------------------------------------------------------------------

    init() {
      let savedTextSize = "medium";
      try { savedTextSize = localStorage.getItem("mt-text-size-v2") || "medium"; } catch (_) {}
      this.setTextSize(savedTextSize);
      let savedLayout = "vertical";
      try {
        const queryLayout = new URLSearchParams(window.location.search).get("layout");
        if (queryLayout === "horizontal" || queryLayout === "vertical") {
          savedLayout = queryLayout;
        } else {
          savedLayout = localStorage.getItem("mt-board-orientation") || "vertical";
        }
      } catch (_) {}
      this.setLayout(savedLayout);
      this.loadSettings();
      this.readFilterLocation();
      this.nowAtLoad = Date.now();
      setInterval(() => { this.now = Date.now(); }, 1000);
      window.addEventListener("load", () => {
        if (this.selected) this.renderSelectedPreview();
      }, { once: true });
      if (typeof this.$watch === "function") {
        for (const field of ["filters", "showHidden", "needsReviewOnly"]) {
          this.$watch(field, () => {
            this.writeFilterLocation();
            this.scheduleQuery();
          });
        }
        this.$watch("settings", () => this.saveSettings());
        this.$watch("mode", () => this.updatePageTitle());
        // Action and capture results surface as one transient toast instead of inline labels.
        this.$watch("actionMessage", (text) => { if (text) this.notify(text, this.actionError); });
        this.$watch("captureComposer.message", (text) => {
          if (text && !this.captureComposer.open) this.notify(text, this.captureComposer.messageError);
        });
        this.$watch("captureComposer.open", (open) => {
          if (!open && this.captureComposer.message) this.notify(this.captureComposer.message, this.captureComposer.messageError);
        });
        this.$watch("selected", () => this.updatePageTitle());
      }
      this.updatePageTitle();
      window.addEventListener("scroll", () => this.clearHoverCard(), { passive: true, capture: true });
      window.addEventListener("popstate", () => this.readCardLocation());
      window.addEventListener("hashchange", () => this.readCardLocation());
      window.addEventListener("beforeunload", event => {
        if (this.hasUnsavedChanges()) {
          event.preventDefault();
          event.returnValue = "";
          return "";
        }
      });
      const refreshVisibleBoard = () => this.refreshItemsAutomatically();
      window.addEventListener("focus", refreshVisibleBoard);
      document.addEventListener("visibilitychange", refreshVisibleBoard);
      setInterval(refreshVisibleBoard, 60000);
      return this.loadWorkspace();
    },

    async loadWorkspace() {
      if (this.workspaceLoading) return;
      this.workspaceLoading = true;
      this.loading = true;
      this.loadError = "";
      try {
        await this.refreshItems();
        if (!this.ready) {
          this.restoreLocalWorkspaceDrafts();
        }
        await this.loadWorkspaceDrafts();
        this.ready = true;
        this.$nextTick(() => this.readCardLocation());
      } catch (error) {
        this.loadError = error.message || "Could not load the workspace.";
      } finally {
        this.loading = false;
        this.workspaceLoading = false;
      }
    },

    updatePageTitle() {
      if (typeof document === "undefined") return;
      if (this.selected && this.selected.title) {
        document.title = this.selected.title + " · MicroTask";
      } else if (this.mode && this.mode !== "tasks") {
        const label = this.viewLabel(this.mode) || (this.mode.charAt(0).toUpperCase() + this.mode.slice(1));
        document.title = label + " · MicroTask";
      } else {
        document.title = "MicroTask";
      }
    },

    // ---------------------------------------------------------------------------
    // Text Size & Display Settings
    // ---------------------------------------------------------------------------

    setLayout(layout) {
      this.layout = layout === "horizontal" ? "horizontal" : "vertical";
      try { localStorage.setItem("mt-board-orientation", this.layout); } catch (_) {}
    },

    loadSettings() {
      let saved = {};
      try { saved = JSON.parse(localStorage.getItem(VIEW_SETTINGS_KEY) || "{}"); } catch (_) {}
      this.settings = normalizeViewSettings(saved);
    },

    saveSettings() {
      try { localStorage.setItem(VIEW_SETTINGS_KEY, JSON.stringify(this.settings)); } catch (_) {}
    },

    viewKind() {
      return TAB_KINDS[this.mode] || this.settings.archive.kind;
    },

    setKind(kind) {
      const view = this.settings.archive;
      view.kind = kind;
      if (!cardGroupOptions(kind).some(option => option.value === view.group)) view.group = "none";
    },

    setTextSize(size) {
      this.textSize = ["small", "medium", "large"].includes(size) ? size : "medium";
      document.documentElement.dataset.textSize = this.textSize;
      try { localStorage.setItem("mt-text-size-v2", this.textSize); } catch (_) {}
    },

    // ---------------------------------------------------------------------------
    // Filters, Search & Item Visibility
    // ---------------------------------------------------------------------------

    // Selected values of a facet; empty when it is unset ("", or Updated's "all").
    filterValues(key) {
      const value = this.filters[key];
      return (Array.isArray(value) ? value : [value]).map(item => String(item).trim()).filter(item => item && item !== "all");
    },

    activeFilterCount() {
      return Object.keys(FILTER_PARAMS).filter(key => this.filterValues(key).length).length
        + (this.showHidden ? 1 : 0) + (this.needsReviewOnly ? 1 : 0);
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
      const filters = defaultFilters();
      for (const [field, name] of Object.entries(FILTER_PARAMS)) {
        const values = params.getAll(name).map(value => (field === "topic" ? topicSegments(value).join("/") : value).trim()).filter(Boolean);
        if (Array.isArray(filters[field])) filters[field] = values;
        else if (values.length) filters[field] = values[0];
      }
      this.filters = filters;
      this.showHidden = params.get("hidden") === "1";
      this.needsReviewOnly = params.get("review") === "1";
    },

    writeFilterLocation() {
      let params;
      try { params = new URLSearchParams(location.search); } catch (_) { return; }
      for (const [field, name] of Object.entries(FILTER_PARAMS)) {
        params.delete(name);
        appendParams(params, name, this.filterValues(field));
      }
      for (const [name, on] of [["hidden", this.showHidden], ["review", this.needsReviewOnly]]) {
        if (on) params.set(name, "1");
        else params.delete(name);
      }
      const search = params.toString();
      const target = location.pathname + (search ? "?" + search : "") + location.hash;
      if (target !== location.pathname + location.search + location.hash) history.replaceState(history.state, "", target);
    },

    clearFilters() {
      this.showHidden = false;
      this.needsReviewOnly = false;
      this.filters = defaultFilters();
    },

    collectionItems(name) {
      const byId = new Map(this.tasks.map(item => [item.id, item]));
      return (this.collections[name] || []).map(id => byId.get(id)).filter(Boolean);
    },

    isCardsView() { return this.mode === "tasks" || this.mode === "notes" || this.mode === "archive"; },
    // Every tab except Drafts shows filtered cards, so it also shows the filter controls.
    filtersApply() { return this.mode !== "drafts"; },
    openTasks() { return this.collectionItems("open"); },

    taskItems() {
      return this.tasks.filter(t => t.kind === "task");
    },

    doingCount() {
      return this.taskItems().filter(t => !t.archived && t.status === "doing").length;
    },

    // ---------------------------------------------------------------------------
    // Due Dates, Deadlines & Countdown Calculations
    // ---------------------------------------------------------------------------

    dueState(task) {
      if (!task || !task.due) return "none";
      const d = this.dueDate(task.due);
      const diff = this.dueDayDiff(d);
      if (d < new Date()) return "overdue";
      if (diff === 0) return "today";
      if (diff <= 7) return "next7";
      return "future";
    },

    dueDate(value) {
      return parseCardDueDate(value);
    },

    dueDayDiff(value) {
      return cardDueDayDifference(value, new Date());
    },

    dueClass(task) {
      const state = this.dueState(task);
      return {
        "is-overdue": state === "overdue",
        "is-due-today": state === "today",
        "is-due-tomorrow": state === "next7" && this.dueDayDiff(this.dueDate(task.due)) === 1,
      };
    },

    deadlineItems() {
      return this.collectionItems("deadlines");
    },

    deadlineRows() {
      const items = this.deadlineItems();
      const field = this.settings.deadlines.group;
      if (field === "none") return items.map(item => ({ item, key: item.id }));
      return this.groupCards(items, field).flatMap(group =>
        [{ group: group.label, key: "group:" + group.key }, ...group.items.map(item => ({ item, key: item.id }))]);
    },

    spanUntil(target) {
      return formatTimeUntil(target, this.now);
    },

    timeLeft(task) {
      return this.spanUntil(this.dueDate(task.due));
    },

    deadlineTimeLeft(task) {
      const due = this.dueDate(task.due);
      const days = this.dueDayDiff(due);
      if (due.getTime() < this.now) {
        return days < 0 ? Math.abs(days) + "d overdue" : "Overdue";
      }
      if (days === 0) return "Due today";
      if (days === 1) return "Due tomorrow";
      return days + " days left";
    },

    dateLabel(value) {
      return formatCardDate(value);
    },

    deadlineDateLabel(task) {
      return this.dateLabel(task.due);
    },

    // ---------------------------------------------------------------------------
    // Timer & Time Tracking
    // ---------------------------------------------------------------------------

    savedTimeSeconds(task) {
      return Math.max(0, Number((task && task.time_seconds) || 0));
    },

    runningTimeSeconds(task) {
      const timer = task && this.runningTimers[task.id];
      if (!timer || timer.saved) return 0;
      return Math.max(0, Math.floor(this.now / 1000) - Number(timer.start_epoch || 0));
    },

    hasTrackedTime(task) {
      return this.savedTimeSeconds(task) > 0;
    },

    savedTimeLabel(task) {
      return "Total " + this.formatTrackedTime(this.savedTimeSeconds(task));
    },

    timerSessionLabel(task) {
      const timer = task && this.runningTimers[task.id];
      if (!timer) return "";
      if (timer.saved) return "Session saved";
      return "Session " + this.formatTrackedTime(this.runningTimeSeconds(task))
        + (this.timerStale(task) ? " · Check timer" : "");
    },

    timerStale(task) {
      return this.timerRunningFor(task) && this.runningTimeSeconds(task) > (this.timerWarnHours || 8) * 3600;
    },

    formatTrackedTime(seconds) {
      return formatTrackedDuration(seconds);
    },

    timerRunningFor(task) {
      return Boolean(task && this.runningTimers[task.id]);
    },

    showStartTimer(task) {
      return Boolean(task && task.kind === "task" && task.status !== "done" && !task.archived && !task.hidden
        && !this.timerRunningFor(task));
    },

    showStopTimer(task) {
      return Boolean(this.timerRunningFor(task));
    },

    canStartTimer(task) {
      return Boolean(this.showStartTimer(task) && !this.busy);
    },

    canStopTimer(task) {
      return Boolean(this.showStopTimer(task) && !this.busy);
    },

    // ---------------------------------------------------------------------------
    // Progress Computations (Year & Deadlines)
    // ---------------------------------------------------------------------------

    yearEnd() {
      const now = new Date(this.now);
      return new Date(now.getFullYear() + 1, 0, 1);
    },

    yearTimeLeft() {
      return this.spanUntil(this.yearEnd());
    },

    yearProgressPercent() {
      const now = new Date(this.now);
      const start = new Date(now.getFullYear(), 0, 1);
      const end = new Date(now.getFullYear() + 1, 0, 1);
      return this.elapsedPercent(start, end);
    },

    elapsedPercent(start, end) {
      return elapsedPercentage(start, end, this.now);
    },

    yearProgressLabel() {
      const year = new Date(this.now).getFullYear();
      const pct = this.yearProgressPercent();
      return pct >= 99.995 ? year + " · 100%" : year + " · " + pct.toFixed(2) + "%";
    },

    deadlineDueEnd(item) {
      return parseCardDueDate(item && item.due);
    },

    deadlineProgressPercent(item) {
      let start = item && item.created;
      if (this.deadlineProgressStart === "now") start = this.nowAtLoad;
      if (this.deadlineProgressStart === "today") {
        const d = new Date(this.now);
        start = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      }
      return this.elapsedPercent(start, this.deadlineDueEnd(item));
    },

    deadlineProgressLabel(item) {
      const pct = this.deadlineProgressPercent(item);
      return pct >= 99.995 ? "100%" : pct.toFixed(2) + "%";
    },

    isHardKind(kind) {
      return kind === "deadline";
    },

    hasDeadlineDue(task) {
      return this.isHardKind(task && task.due_kind);
    },

    // ---------------------------------------------------------------------------
    // Focus Mode & Task Prioritization
    // ---------------------------------------------------------------------------

    focusTask() {
      const visible = this.openTasks();
      const selected = visible.find(t => t.id === this.focusContext.selected?.id);
      if (selected) return selected;
      for (const candidate of this.focusContext.candidates) {
        const task = visible.find(t => t.id === candidate.id);
        if (task) return task;
      }
      return null;
    },

    focusReason(task) {
      if (!task) return "";
      if (task.id === this.focusContext.selected?.id) return this.focusContext.selected.reason;
      return this.focusContext.candidates.find(t => t.id === task.id)?.reason || "";
    },

    focusAlerts() {
      const selectedId = this.focusTask()?.id;
      const visibleIds = new Set(this.openTasks().map(t => t.id));
      return this.focusContext.alerts.filter(t => t.id !== selectedId && visibleIds.has(t.id));
    },

    followupTasks() {
      const tasks = this.openTasks();
      return this.focusContext.followup_ids.map(id => tasks.find(t => t.id === id)).filter(Boolean);
    },

    needsRestartStep(task) {
      return Boolean(task && task.needs_restart);
    },

    focusModeTasks() {
      const current = this.focusTask();
      return this.openTasks()
        .filter(t => (!current || t.id !== current.id) && (this.focusContext.alerts.some(alert => alert.id === t.id) || this.needsRestartStep(t)));
    },

    // ---------------------------------------------------------------------------
    // Card Grouping
    // ---------------------------------------------------------------------------

    groupOptions(kind) { return cardGroupOptions(kind); },

    // Cards of the current Tasks, Notes or Archive tab, filtered by Kind and grouped.
    viewGroups() {
      const kind = this.viewKind();
      const items = this.collectionItems(this.mode === "archive" ? "archive" : "cards")
        .filter(item => kind === "all" || item.kind === kind);
      const groups = this.groupCards(items, this.settings[this.mode].group, { workflow: this.mode !== "archive" });
      return groups.length ? groups : this.groupCards([], "none");
    },

    // Ordered {key, label, items, countLabel, className, empty} groups. Groups with
    // no value come last as "Ungrouped"; a card with several tags is in each tag's
    // group. With workflow, status groups are the policy statuses, empty ones too,
    // with the Done cap and the WIP label.
    groupCards(items, field, { workflow = false } = {}) {
      if (field === "none") {
        return [{ key: "", label: "", items, countLabel: "", className: {}, empty: "No cards." }];
      }
      const columns = workflow && field === "status";
      const buckets = new Map(columns ? this.statuses.map(status => [status, []]) : []);
      for (const item of items) {
        const values = field === "tag" ? item.tags || [] : [item[field]];
        // Topics are case-insensitive: group by the tree's spelling, like filtering.
        const keys = Array.from(new Set(values.map(value => field === "topic" ? this.topicSpelling(value) : String(value ?? "")).filter(Boolean)));
        for (const key of keys.length ? keys : [""]) {
          if (!buckets.has(key)) buckets.set(key, []);
          buckets.get(key).push(item);
        }
      }
      const policy = { status: this.statuses, priority: this.priorityOrder, due_kind: this.dueKinds }[field] || [];
      const others = Array.from(buckets.keys()).filter(key => key && !policy.includes(key))
        .sort((a, b) => a.localeCompare(b, undefined, { sensitivity: "base" }));
      const keys = policy.filter(key => buckets.has(key)).concat(others);
      if (buckets.has("")) keys.push("");
      return keys.map(key => {
        const all = buckets.get(key);
        const capped = columns && key === "done" && this.doneLimit > 0 && all.length > this.doneLimit;
        return {
          key,
          label: key === "" ? "Ungrouped" : this.groupLabel(field, key, workflow),
          items: capped ? all.slice(0, this.doneLimit) : all,
          countLabel: capped ? this.doneLimit + " of " + all.length : String(all.length),
          className: columns ? this.columnClass(key) : {},
          empty: columns ? "No tasks." : "No cards."
        };
      });
    },

    topicSpelling(path) {
      const text = normalizeTopicPath(path);
      const node = this.topicTree.find(item => item.path.toLowerCase() === text.toLowerCase());
      return node ? node.path : text;
    },

    groupLabel(field, key, workflow) {
      if (field === "topic") return topicSegments(key).join(" › ");
      if (field === "status") return workflow ? this.statusLabel(key) : this.statusText(key);
      if (field === "priority") return this.priorityLabel(key).replace(/^./, c => c.toUpperCase());
      if (field === "due_kind") return key.replace(/^./, c => c.toUpperCase());
      return key;
    },

    // ---------------------------------------------------------------------------
    // Workspace & Note Drafts Storage
    // ---------------------------------------------------------------------------

    getDraftStore() {
      if (!this.draftStorage) {
        this.draftStorage = createDraftStore({
          initialDrafts: [],
          context: () => ({ workspaceKey: this.workspaceKey, apiToken: this.apiToken,
            available: true }),
          findTarget: (ref, path) => this.tasks.find(item => ref ? item.ref === ref : item.path === path),
          captureFields: draft => this.captureDraftFields(draft),
          onStatus: status => { this.draftWorkspaceStatus = status; },
          onWarning: message => { this.captureComposer.recoveryWarning = message; },
          onSaved: draft => { if (draft === this.captureComposer.draft) this.captureComposer.persisted = true; },
        });
      }
      return this.draftStorage;
    },

    captureContext() {
      return {
        workspaceKey: this.workspaceKey,
        busy: this.busy,
        store: this.getDraftStore(),
        taskDefaults: this.taskDefaults,
        taskStatuses: this.taskStatuses,
        taskPriorityPolicy: this.taskPriorityPolicy,
        taskTypePolicy: this.taskTypePolicy,
        scopeDefaults: {
          // A card created under several selected projects or topics has no obvious home.
          project: this.filters.project.length === 1 ? this.filters.project[0] : "",
          topic: this.filters.topic.length === 1 ? this.filters.topic[0] : "",
        },
        taskOptionsFromDraft: draft => this.taskOptionsFromDraft(draft),
        metadataSnapshot: draft => this.draftMetadataSnapshot(draft),
        captureDraftFields: draft => this.captureDraftFields(draft),
        getDrafts: () => this.drafts,
        scheduleDraftSave: (draft, delay) => this.scheduleDraftSave(draft, delay),
        setMode: mode => { this.mode = mode; },
        openWorkspaceDraft: draft => this.openWorkspaceDraft(draft),
        finishWorkspaceDraft: draft => this.finishWorkspaceDraft(draft),
        nextTick: callback => this.$nextTick(callback),
        focusInput: () => this.$nextTick(() => {
          const input = document.getElementById("mt-composer-body");
          if (input) input.focus();
        }),
        confirm: message => typeof window !== "undefined" && typeof window.confirm === "function"
          ? window.confirm(message) : true,
        dismissLocalWorkspaceDraft: draft => this.dismissLocalWorkspaceDraft(draft),
        discardWorkspaceDraft: (draft, confirmed) => this.discardWorkspaceDraft(draft, confirmed),
        markLocalDraftClosed: draft => this.markLocalDraftClosed(draft),
      };
    },

    propertyContext() {
      return {
        selected: this.selected,
        busy: this.busy,
        draftBusy: this.draftBusy,
        contentOpen: Boolean(this.contentEditor.draft),
        defaults: this.taskDefaults,
        priorityPolicy: this.taskPriorityPolicy,
        typePolicy: this.taskTypePolicy,
        dueKindPolicy: this.taskDueKindPolicy,
        apiPost: (path, body) => this.apiPost(path, body),
        actionMessage: () => this.actionMessage,
        getSelected: () => this.selected,
      };
    },

    contentContext() {
      return {
        selected: this.selected,
        busy: this.busy,
        draftBusy: this.draftBusy,
        store: this.getDraftStore(),
        getDrafts: () => this.drafts,
        makeId: () => this.captureComposer.makeId(),
        scheduleDraftSave: draft => this.scheduleDraftSave(draft),
        needsReconciliation: draft => this.draftNeedsReconciliation(draft),
        finishWorkspaceDraft: draft => this.finishContentDraft(draft),
        clearStatus: () => { this.captureComposer.message = ""; this.captureComposer.messageError = false; },
        focusInput: () => this.$nextTick(() => this.$refs.detailContentInput?.focus()),
      };
    },

    noteDraftStorageKey(workspaceKey, draftId) {
      return this.getDraftStore().storageKey(workspaceKey, draftId);
    },

    async loadWorkspaceDrafts() {
      try {
        await this.getDraftStore().load();
        await this.importLocalWorkspaceDrafts();
        this.restoreLocalWorkspaceDrafts();
      } catch (_) {
        this.draftWorkspaceStatus = "Error";
      }
    },

    importLocalWorkspaceDrafts() {
      return this.getDraftStore().importLocal();
    },

    restoreLocalWorkspaceDrafts() {
      return this.getDraftStore().restoreLocal();
    },

    visibleDrafts() {
      const query = this.draftSearch.trim().toLowerCase();
      return this.drafts.filter(draft => {
        if (!query) return true;
        const target = this.tasks.find(item => item.path === draft.target);
        return [draft.title, draft.kind, draft.mode, draft.body, target && target.title, draft.target]
          .some(value => String(value || "").toLowerCase().includes(query));
      }).sort((a, b) => String(b.updated || "").localeCompare(String(a.updated || "")));
    },

    draftDisplayTitle(draft) {
      if (!draft) return "Untitled draft";
      if (String(draft.title || "").trim()) return String(draft.title).trim();
      const target = this.tasks.find(item => item.path === draft.target);
      if (target) return target.title;
      const firstLine = String(draft.body || "").split(/\r?\n/).find(line => line.trim());
      return firstLine ? firstLine.trim() : "Untitled draft";
    },

    cleanExcerpt(text) {
      if (!text) return "";
      let s = String(text);
      s = s.replace(/^#{1,6}\s+/gm, "");
      s = s.replace(/^\s*([-*+]|\d+\.)\s+/gm, "");
      s = s.replace(/^\s*>\s*/gm, "");
      s = s.replace(/!?\[([^\]]*)\]\((?:[^\s()]|\([^()]*\))*(?:\s+["'][^"']*["'])?\s*\)/g, "$1");
      s = s.replace(/\[([^\]]+)\]\([^\s)]*$/g, "$1");
      s = s.replace(/\[([^\]]+)$/g, "$1");
      s = s.replace(/`([^`]+)`/g, "$1");
      s = s.replace(/`/g, "");
      s = s.replace(/~~([^~]+)~~/g, "$1");
      s = s.replace(/\*\*([^*]+)\*\*/g, "$1");
      s = s.replace(/(^|\W)__([^_]+)__(\W|$)/g, "$1$2$3");
      s = s.replace(/(^|\W)\*([^*]+)\*(\W|$)/g, "$1$2$3");
      s = s.replace(/(^|\s|\W)_([^\s_]|(?:[^\s_].*?[^\s_]))_(\s|\W|$)/g, "$1$2$3");
      return s.replace(/\s+/g, " ").trim();
    },

    draftExcerpt(draft) {
      const body = String(draft && draft.body || "");
      const text = this.cleanExcerpt(body);
      if (!text) return "Empty draft";
      return text.length > 180 ? text.slice(0, 177).trimEnd() + "…" : text;
    },

    draftSavedLabel(value) {
      if (!value) return "just now";
      const parsed = new Date(String(value).replace(/^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)$/, "$1T$2"));
      return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    },

    draftNeedsReconciliation(draft) {
      const hash = String(draft && draft.base_hash || "");
      return Boolean(draft && draft.mode === "edit" && hash === "0".repeat(64));
    },

    async openWorkspaceDraft(draft) {
      if (!draft) return;
      if (this.captureComposer.saving || !this.captureComposer.preserveBeforeReplace(this.captureContext())) return;
      if (draft.mode === "edit" && draft.origin === "card-body") {
        const target = this.tasks.find(item => item.path === draft.target);
        if (target) {
          await this.select(target);
          if (this.selectedId === target.id && this.detailLoading) await this.loadSelectedDetails();
          if (this.selectedId === target.id && !this.detailLoading && !this.detailError) {
            this.contentEditor.begin(this.contentContext(), draft);
          }
          return;
        }
      }
      this.draftWorkspaceStatus = "Saved in workspace";
      if (draft.kind === "note" || draft.mode === "new") {
        const target = this.tasks.find(item => item.path === draft.target);
        const body = String(draft.body || "");
        this.captureComposer.draft = {
          version: 1, workspaceKey: this.workspaceKey, mode: draft.mode,
          kind: draft.kind || "note",
          taskOptions: this.taskOptionsFromDraft(draft),
          captureId: String(draft.capture_id || ""), createdAt: String(draft.created || ""),
          title: String(draft.title || ""), text: body,
          instructions: String(draft.instructions || ""), ref: target ? target.ref : "",
          originalText: body, draftId: draft.sourceId || draft.id, revision: Number(draft.revision),
          content_hash: draft.content_hash || "", _serverSaved: Boolean(draft._serverSaved || Number(draft.revision) > 0),
          _orphanedLocal: Boolean(draft._orphanedLocal), sourceId: draft.sourceId || draft.id,
          base_hash: draft.base_hash || "",
          base_updated: draft.base_updated || "",
          _savedBody: Object.prototype.hasOwnProperty.call(draft, "_savedBody") ? draft._savedBody : body,
          _savedTitle: Object.prototype.hasOwnProperty.call(draft, "_savedTitle") ? draft._savedTitle : String(draft.title || ""),
          _savedKind: Object.prototype.hasOwnProperty.call(draft, "_savedKind") ? draft._savedKind : (draft.kind || "note"),
          _savedMetadata: Object.prototype.hasOwnProperty.call(draft, "_savedMetadata")
            ? draft._savedMetadata
            : this.draftMetadataSnapshot({ taskOptions: this.taskOptionsFromDraft(draft) }),
        };
        this.captureComposer.open = true;
        this.$nextTick(() => {
          const input = document.getElementById("mt-composer-body");
          if (input) input.focus();
        });
        if (this.draftChangedFromSaved(this.captureComposer.draft)) {
          this.scheduleDraftSave(this.captureComposer.draft, 0);
        }
        return;
      }
      this.selectedWorkspaceDraft = {
        ...draft,
        id: draft.sourceId || draft.id,
        _savedBody: Object.prototype.hasOwnProperty.call(draft, "_savedBody") ? draft._savedBody : String(draft.body || ""),
        _savedTitle: Object.prototype.hasOwnProperty.call(draft, "_savedTitle") ? draft._savedTitle : String(draft.title || ""),
          _savedKind: Object.prototype.hasOwnProperty.call(draft, "_savedKind") ? draft._savedKind : (draft.kind || "task"),
          _savedMetadata: Object.prototype.hasOwnProperty.call(draft, "_savedMetadata")
            ? draft._savedMetadata : this.draftMetadataSnapshot(draft),
      };
      this.taskDraftOpen = true;
      this.mode = "drafts";
      this.$nextTick(() => {
        const input = document.getElementById("mt-task-draft-body");
        if (input) input.focus();
      });
      if (this.draftChangedFromSaved(this.selectedWorkspaceDraft)) {
        this.scheduleDraftSave(this.selectedWorkspaceDraft, 0);
      }
    },

    taskOptionsFromDraft(draft) {
      const nested = draft && (draft.taskOptions || draft.task_options);
      const saved = nested && typeof nested === "object" ? nested : {};
      const status = draft && draft.status || saved.status || this.taskDefaults.status;
      const due = draft && Object.prototype.hasOwnProperty.call(draft, "due") ? draft.due : saved.due;
      const dueKind = draft && Object.prototype.hasOwnProperty.call(draft, "due_kind") ? draft.due_kind : saved.due_kind;
      return {
        status: status || this.taskStatuses[0] || "inbox",
        priority: draft && draft.priority || saved.priority || this.taskDefaults.priority,
        type: draft && draft.type || saved.type || this.taskDefaults.type,
        project: draft && draft.project || saved.project || "",
        topic: draft && draft.topic || saved.topic || "",
        tags: Array.isArray(draft && draft.tags) ? draft.tags.slice() : (Array.isArray(saved.tags) ? saved.tags.slice() : []),
        due: due || null,
        due_kind: dueKind || null,
      };
    },

    draftMetadataSnapshot(draft) {
      return this.getDraftStore().metadataSnapshot(draft);
    },

    draftChangedFromSaved(draft) {
      return this.getDraftStore().hasChanges(draft);
    },

    captureDraftFields(draft) {
      if (!draft || draft.mode !== "new") return {};
      const options = this.taskOptionsFromDraft(draft);
      return {
        status: options.status || null,
        priority: options.priority || null,
        type: options.type || null,
        project: options.project || null,
        topic: options.topic || null,
        tags: options.tags || [],
        due: options.due || null,
        due_kind: options.due_kind || null,
        instructions: String(draft.instructions || ""),
      };
    },

    closeTaskDraft() {
      this.taskDraftOpen = false;
      this.selectedWorkspaceDraft = null;
      this.mode = "drafts";
    },

    persistSelectedDraft() {
      const draft = this.selectedWorkspaceDraft;
      if (!draft) return;
      this.saveDraftLocally(draft);
      if (!draft._orphanedLocal) this.scheduleDraftSave(draft);
    },

    saveDraftLocally(draft) {
      return this.getDraftStore().saveLocal(draft);
    },

    markLocalDraftClosed(draft) {
      return this.getDraftStore().markClosed(draft);
    },

    dismissLocalWorkspaceDraft(draft) {
      if (!draft || !draft._orphanedLocal) return;
      const recoveryId = this.getDraftStore().dismiss(draft);
      if (this.captureComposer.draft && (this.captureComposer.draft.draftId === recoveryId || this.captureComposer.draft.id === recoveryId) && this.captureComposer.draft._orphanedLocal) {
        this.captureComposer.draft = null;
        this.captureComposer.open = false;
      }
      if (this.selectedWorkspaceDraft === draft) this.closeTaskDraft();
      if (this.contentEditor.draft === draft) {
        this.contentEditor.draft = null;
        this.contentEditor.text = "";
        this.contentEditor.error = "";
        this.$nextTick(() => this.$refs.detailDialog?.querySelector('.mt-content-edit')?.focus());
      }
      this.draftWorkspaceStatus = "Local recovery copy dismissed";
    },

    async recoverLocalWorkspaceDraft(draft) {
      if (!draft || !draft._orphanedLocal || this.draftBusy) return null;
      const sourceId = draft.sourceId || draft.storageId || draft.id;
      const newId = this.captureComposer.makeId();
      this.draftBusy = true;
      try {
        const saved = await this.getDraftStore().recover(draft, newId, this.captureComposer.localIsoSeconds(new Date()));
        if (this.captureComposer.draft && this.captureComposer.draft.draftId === sourceId) {
          Object.assign(this.captureComposer.draft, {
            id: newId, draftId: newId, sourceId: newId, storageId: newId,
            ...(draft.mode === "new" ? { captureId: saved.capture_id, capture_id: saved.capture_id } : {}),
            revision: saved.revision,
            content_hash: saved.content_hash, updated: saved.updated,
            _serverSaved: true, _orphanedLocal: false, _localRecovery: false, _staleLocal: false,
            _savedBody: this.captureComposer.draft.text, _savedTitle: this.captureComposer.draft.title || "",
            _savedKind: this.captureComposer.draft.kind || "note",
            _savedMetadata: this.draftMetadataSnapshot(this.captureComposer.draft),
          });
          this.saveDraftLocally({ ...this.captureComposer.draft, id: newId, body: this.captureComposer.draft.text });
        }
        if (this.selectedWorkspaceDraft && (this.selectedWorkspaceDraft.sourceId === sourceId || this.selectedWorkspaceDraft.id === sourceId)) {
          this.selectedWorkspaceDraft = { ...saved,
            _savedBody: saved.body, _savedTitle: saved.title || "",
            _savedKind: saved.kind,
            _savedMetadata: this.draftMetadataSnapshot(saved) };
          this.saveDraftLocally(this.selectedWorkspaceDraft);
        }
        if (this.contentEditor.draft === draft) {
          this.contentEditor.draft = { ...saved,
            _savedBody: saved.body, _savedTitle: saved.title || "",
            _savedKind: saved.kind, _savedMetadata: this.draftMetadataSnapshot(saved) };
          this.contentEditor.error = "";
          this.saveDraftLocally(this.contentEditor.draft);
        }
        this.draftWorkspaceStatus = "Recovered as a separate workspace draft";
        this.captureComposer.recoveryWarning = "The recovered edit keeps its original target baseline and may need reconciliation before applying.";
        return saved;
      } catch (error) {
        this.draftWorkspaceStatus = "Error";
        this.captureComposer.recoveryWarning = error.message || "Could not recover this local copy.";
        return null;
      } finally {
        this.draftBusy = false;
      }
    },

    isUnsavedEmptyCapture(draft) {
      return this.getDraftStore().isEmpty(draft);
    },

    scheduleDraftSave(draft, delay = 450) {
      return this.getDraftStore().schedule(draft, delay);
    },

    saveWorkspaceDraft(draft) {
      return this.getDraftStore().save(draft);
    },

    flushWorkspaceDraft(draft) {
      return this.getDraftStore().flush(draft);
    },

    async finishWorkspaceDraft(draft) {
      if (!draft || draft._orphanedLocal || this.draftBusy) return null;
      this.draftBusy = true;
      try {
        const result = await this.getDraftStore().finish(draft);
        if (!result) return null;
        await this.applyPayload(result);
        if (draft === this.contentEditor.draft) {
          this.contentEditor.draft = null;
          this.contentEditor.text = "";
          this.$nextTick(() => this.$refs.detailDialog?.querySelector('.mt-content-edit')?.focus());
        } else if (draft === this.captureComposer.draft || (this.captureComposer.draft && this.captureComposer.draft.draftId === draft.id)) {
          const wasEdit = draft.mode === "edit";
          this.captureComposer.draft = null;
          this.captureComposer.persisted = false;
          this.captureComposer.close(this.captureContext());
          if (wasEdit) this.mode = "notes";
        } else {
          this.closeTaskDraft();
        }
        this.draftWorkspaceStatus = "";
        this.captureComposer.recoveryWarning = "";
        this.captureComposer.message = draft.origin === "card-body" ? "Content saved" : draft.mode === "new"
          ? (result.saved_ref ? "Created " + result.saved_ref : "Capture created")
          : (result.saved_ref ? "Draft applied to " + result.saved_ref : "Draft applied");
        this.captureComposer.messageError = false;
        return result;
      } catch (error) {
        this.draftWorkspaceStatus = "Error";
        this.captureComposer.message = error.message || "Could not apply draft";
        this.captureComposer.messageError = true;
        return null;
      } finally {
        this.draftBusy = false;
      }
    },

    // Run a body-only save, then rebase pending property edits onto the new
    // content hash, but only when the server preserved their baseline.
    async saveBodyKeepingProperties(save) {
      const baseline = this.propertyEditor.baseline;
      const result = await save();
      if (result && this.selected && this.propertyEditor.comparable(
        this.propertyEditor.values(this.selected, this.taskDefaults)) === baseline) {
        this.propertyEditor.baselineHash = this.selected.content_hash;
        this.propertyEditor.conflict = false;
      }
      return result;
    },

    finishContentDraft(draft) {
      return this.saveBodyKeepingProperties(() => this.finishWorkspaceDraft(draft));
    },

    async discardWorkspaceDraft(draft, confirmed = false) {
      if (draft && draft._orphanedLocal) {
        this.dismissLocalWorkspaceDraft(draft);
        return;
      }
      if (!draft || this.draftBusy) return;
      if (typeof window !== "undefined" && typeof window.confirm === "function" &&
          !confirmed && !window.confirm("Discard this draft? This removes it from the workspace.")) return;
      this.draftBusy = true;
      try {
        if (!await this.getDraftStore().discard(draft)) return;
        if (this.captureComposer.draft && (this.captureComposer.draft.draftId === draft.id || this.captureComposer.draft.draftId === draft.draftId)) {
          this.captureComposer.draft = null;
          this.captureComposer.open = false;
        }
        if (this.selectedWorkspaceDraft && this.selectedWorkspaceDraft.id === (draft.id || draft.draftId)) {
          this.closeTaskDraft();
        }
        this.draftWorkspaceStatus = "Draft discarded";
      } catch (error) {
        this.draftWorkspaceStatus = "Error";
        this.captureComposer.message = error.message || "Could not discard draft";
        this.captureComposer.messageError = true;
      } finally {
        this.draftBusy = false;
      }
    },

    // ---------------------------------------------------------------------------
    // New Capture & Note Composer
    // ---------------------------------------------------------------------------

    cardMetadataConfig() {
      return {
        task_defaults: this.taskDefaults,
        task_statuses: this.taskStatuses,
        task_priorities: this.taskPriorityPolicy,
        task_types: this.taskTypePolicy,
        task_due_kinds: this.taskDueKindPolicy,
        status_labels: this.statusLabels,
        suggested_tags: this.suggestedTags,
        workspace_projects: this.workspaceProjects,
        topic_tree: this.topicTree,
        workspace_tags: this.workspaceTags,
        workspace_usage: this.workspaceUsage,
      };
    },

    beginNewCapture(event) {
      if (!this.ready) return;
      this.captureComposer.beginNew(this.captureContext(), event, this.mode === "tasks" || this.mode === "focus" ? "task" : "note");
    },

    hasUnsavedChanges() {
      if (this.selected && this.propertyEditor.changed()) return true;
      if (this.contentEditor.draft && !this.contentEditor.localSaved &&
          this.contentEditor.draft.body !== this.contentEditor.draft._savedBody) return true;
      return this.captureComposer.hasUnpersisted(this.captureContext());
    },

    markReviewed(card) {
      if (!card || !card.needs_review || this.busy) return;
      this.apiPost("/api/review", {
        ref: card.ref,
        expected_hash: String(card.content_hash || ""),
      });
    },

    trapComposerFocus(event) {
      this.trapDialogFocus(event, this.$refs.composerDialog);
    },

    trapTaskDraftFocus(event) {
      this.trapDialogFocus(event, this.$refs.taskDraftDialog);
    },

    trapDetailFocus(event) {
      this.trapDialogFocus(event, this.$refs.detailDialog);
    },

    trapDialogFocus(event, dialog) {
      if (!dialog || !dialog.querySelectorAll) return;
      const focusable = Array.from(dialog.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
      )).filter(element => element.getClientRects().length
        && getComputedStyle(element).visibility !== "hidden"
        && !element.closest("[inert]")
        && !element.closest('[aria-hidden="true"]'));
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },

    // ---------------------------------------------------------------------------
    // Card Formatting, Badges & Labels
    // ---------------------------------------------------------------------------

    statusBadgeClass(status) {
      return status === "done" ? "mt-badge-success" : "mt-badge-accent";
    },

    showCardStatus(task) {
      const view = this.settings[this.mode];
      return !view || view.group !== "status";
    },

    cardDate(task) {
      if (task.kind !== "task") return "";
      if (task.status === "done") return task.completed || task.updated || "";
      return this.mode === "archive" ? task.updated || "" : "";
    },

    statusText(status) {
      if (Object.prototype.hasOwnProperty.call(this.statusLabels, status)) {
        return this.statusLabels[status];
      }
      return status || "";
    },

    viewLabel(name) {
      if (Object.prototype.hasOwnProperty.call(this.viewLabels, name)) {
        return this.viewLabels[name];
      }
      return name || "";
    },

    statusLabel(status) {
      const label = this.statusText(status);
      if (status !== "doing") return label;
      return label + " (" + this.doingCount() + "/" + this.maxDoingTasks + " max)";
    },

    columnClass(status) {
      return {
        "is-quiet": status === "done",
        "is-over-limit": status === "doing" && this.doingCount() >= this.maxDoingTasks
      };
    },

    hasPriority(priority) {
      return Boolean(priority && priority !== "none");
    },

    priorityLabel(priority) {
      return priority === "none" ? "Unassigned" : priority;
    },

    priorityClass(priority) {
      return "priority-" + String(priority || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
    },

    cardClass(task) {
      const due = this.dueState(task);
      return {
        [this.priorityClass(task.priority)]: this.hasPriority(task.priority),
        "is-selected": this.selectedId === task.id,
        "is-note": task.kind === "note",
        "is-done": task.status === "done",
        "is-overdue": due === "overdue",
        "is-due-today": due === "today"
      };
    },

    openStatuses() {
      return this.statuses.filter(status => status !== "done");
    },

    canMoveSelected() {
      return Boolean(this.selected && this.selected.kind === "task" && this.selected.status !== "done");
    },

    canDoneSelected() {
      return this.canMoveSelected();
    },

    // ---------------------------------------------------------------------------
    // Server State Synchronization & API Actions
    // ---------------------------------------------------------------------------

    refreshDerivedLists(meta) {
      const incomingWorkspaceKey = meta.workspace_key ? String(meta.workspace_key) : "";
      if (incomingWorkspaceKey && incomingWorkspaceKey !== this.workspaceKey && this.captureComposer.draft) {
        this.captureComposer.persist(this.captureContext());
        this.captureComposer.close(this.captureContext());
        this.captureComposer.message = "The workspace changed. Your draft is kept with its original workspace; return there to resume it.";
        this.captureComposer.messageError = true;
      }
      this.statusLabels = meta.status_labels || this.statusLabels;
      this.viewLabels = meta.view_labels || this.viewLabels;
      this.priorityOrder = meta.priority_order || [];
      this.dueKinds = meta.due_kinds || this.dueKinds;
      this.taskDefaults = meta.task_defaults || this.taskDefaults;
      this.taskStatuses = meta.task_statuses || this.taskStatuses;
      this.taskPriorityPolicy = meta.task_priorities || this.taskPriorityPolicy;
      this.taskTypePolicy = meta.task_types || this.taskTypePolicy;
      this.taskDueKindPolicy = meta.task_due_kinds || this.taskDueKindPolicy;
      this.suggestedTags = meta.suggested_tags || this.suggestedTags;
      this.workspaceProjects = meta.workspace_projects || [];
      this.topicTree = meta.topic_tree || [];
      this.workspaceTags = meta.workspace_tags || [];
      this.workspaceUsage = meta.workspace_usage || {};
      this.maxDoingTasks = meta.max_doing_tasks || 1;
      this.doneLimit = meta.done_limit || 10;
      this.focusContext = meta.focus || { candidates: [], alerts: [], followup_ids: [] };
      this.runningTimers = meta.time_running || {};
      this.timerWarnHours = meta.timer_warn_hours || 8;
      this.view = meta.view || null;
      this.demo = Boolean(meta.demo);
      this.showYearProgress = meta.show_year_progress !== false;
      this.showDeadlineProgress = meta.show_deadline_progress !== false;
      this.deadlineProgressStart = meta.deadline_progress_start || "created";
      if (incomingWorkspaceKey) this.workspaceKey = incomingWorkspaceKey;
      this.apiToken = meta.api_token || this.apiToken;
      this.priorities = meta.priority_order && meta.priority_order.length
        ? meta.priority_order
        : Array.from(new Set(this.tasks.map(t => t.priority).filter(Boolean)));
      this.types = Object.keys(this.taskTypePolicy).length
        ? Object.keys(this.taskTypePolicy)
        : Array.from(new Set(this.tasks.map(t => t.type || t.kind).filter(Boolean)));
      this.projects = Array.from(new Set(this.tasks.map(t => t.project).filter(Boolean))).sort();
      // A linked project may differ in case; use the workspace spelling. One with no
      // cards yet still shows in the filter (see filterChoices).
      this.filters.project.forEach((linked, index) => {
        if (this.projects.includes(linked)) return;
        const spelling = this.projects.find(name => name.toLowerCase() === linked.toLowerCase());
        if (spelling) this.filters.project.splice(index, 1, spelling);
      });
    },

    applyPayload(payload, fromQuery = false) {
      const matchesQuery = !payload.query || JSON.stringify(payload.query) === JSON.stringify(this.collectionQuery());
      if (!fromQuery) {
        this.querySequence += 1;
        this.queryLoading = false;
      }
      if (payload.collections && matchesQuery) this.collections = payload.collections;
      if (!fromQuery && !matchesQuery) this.scheduleQuery(0);
      const incomingItems = payload.tasks || [];
      const targets = new Map(incomingItems.map(item => [item.id, item]));
      const referencesChanged = this.selected && (this.tasks.length !== incomingItems.length || this.tasks.some(item => {
        const target = targets.get(item.id);
        return !target || item.ref !== target.ref || item.title !== target.title || item.kind !== target.kind;
      }));
      this.tasks = incomingItems;
      this.statuses = payload.statuses || this.statuses;
      if (Object.prototype.hasOwnProperty.call(payload, "drafts")) this.drafts = payload.drafts || [];
      if (Object.prototype.hasOwnProperty.call(payload, "sync_warning")) {
        this.syncWarning = payload.sync_warning || "";
      }
      this.refreshDerivedLists(payload.meta || {});
      if (this.selectedId) {
        const nextSelection = this.tasks.find(t => t.id === this.selectedId) || null;
        if (nextSelection && this.propertyEditor.baselineHash &&
            String(nextSelection.content_hash || "") !== this.propertyEditor.baselineHash) {
          if (this.propertyEditor.changed()) {
            this.propertyEditor.conflict = true;
          } else {
            this.propertyEditor.begin(nextSelection, this.taskDefaults);
          }
        }
        const previous = this.selected;
        const sameVersion = previous && nextSelection && previous.id === nextSelection.id &&
          previous.content_hash === nextSelection.content_hash && !referencesChanged;
        this.selected = sameVersion ? { ...previous, ...nextSelection } : nextSelection;
        if (nextSelection) this.moveDestination = nextSelection.status || "";
        if (!this.selected) this.clearSelection();
        else if (typeof this.selected.body !== "string") {
          if (!sameVersion || !this.detailLoading) return this.loadSelectedDetails();
        }
      }
      if (this.selected) this.renderSelectedPreview();
    },

    collectionQuery() {
      return { ...this.filters, show_hidden: this.showHidden, needs_review: this.needsReviewOnly };
    },

    scheduleQuery(delay = 180, preserveError = false) {
      if (!this.ready) return;
      clearTimeout(this.queryTimer);
      // Invalidate in-flight results immediately, before the debounce expires.
      this.querySequence += 1;
      this.queryLoading = true;
      if (!preserveError) this.queryError = "";
      this.queryTimer = setTimeout(() => this.refreshItems().catch(error => {
        this.queryError = error.message || "Could not update results.";
      }), delay);
    },

    async refreshItemsAutomatically() {
      // Focus and visibility events often arrive together. Keep automatic refreshes
      // out of pending searches and allow a short grace period after fresh results.
      if (document.hidden || !this.ready || this.queryLoading || this.itemRequestsInFlight ||
          (this.lastItemsRefreshAt && Date.now() - this.lastItemsRefreshAt < 5000)) return;
      try {
        await this.refreshItems();
      } catch (_) {}
    },

    async refreshItems() {
      clearTimeout(this.queryTimer);
      const sequence = ++this.querySequence;
      const query = this.collectionQuery();
      this.queryLoading = true;
      this.itemRequestsInFlight += 1;
      try {
        const params = new URLSearchParams();
        for (const [name, value] of Object.entries(query)) appendParams(params, name, value);
        const response = await fetch("/api/items?" + params, { cache: "no-store" });
        const payload = await response.json();
        if (sequence !== this.querySequence) return;
        if (!response.ok) throw new Error(payload.error || "Could not load the workspace.");
        await this.applyPayload(payload, true);
        if (sequence !== this.querySequence) return;
        this.lastItemsRefreshAt = Date.now();
        this.queryError = "";
      } catch (error) {
        if (sequence === this.querySequence) throw error;
      } finally {
        this.itemRequestsInFlight -= 1;
        if (sequence === this.querySequence) this.queryLoading = false;
      }
    },

    notify(text, error = false) {
      if (this.toastTimer) clearTimeout(this.toastTimer);
      this.toastTimer = null;
      this.toast = { text: String(text), error: !!error, id: Date.now() };
      // Errors stay until dismissed so they can be read; confirmations fade.
      if (!error) this.toastTimer = setTimeout(() => this.dismissToast(), 3200);
    },

    dismissToast() {
      if (this.toastTimer) clearTimeout(this.toastTimer);
      this.toastTimer = null;
      this.toast = null;
      if (this.captureComposer && !this.captureComposer.open) {
        this.captureComposer.message = "";
        this.captureComposer.messageError = false;
      }
    },

    async apiPost(path, body) {
      this.busy = true;
      this.actionMessage = "";
      this.actionError = false;
      try {
        const response = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Microtask-Token": this.apiToken },
          body: JSON.stringify({ ...(body || {}), query: this.collectionQuery() })
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Action failed");
        }
        await this.applyPayload(payload);
        if (!Object.prototype.hasOwnProperty.call(payload, "sync_warning")) {
          this.syncWarning = "";
        }
        this.actionMessage = payload.message || "Updated";
        this.actionError = false;
        return payload;
      } catch (error) {
        if (path === "/api/time-start" || path === "/api/time-stop" || path === "/api/time-cancel") {
          try {
            await this.refreshItems();
          } catch (_) {}
        }
        this.actionMessage = error && error.message ? error.message : "Action failed";
        this.actionError = true;
        return null;
      } finally {
        this.busy = false;
      }
    },

    moveSelected() {
      if (!this.canMoveSelected() || !this.moveDestination || this.moveDestination === this.selected.status) return;
      this.apiPost("/api/move", { ref: this.selected.ref, status: this.moveDestination });
    },

    commentSelected() {
      const message = this.commentText.trim();
      if (!this.selected || !message) return;
      this.apiPost("/api/comment", { ref: this.selected.ref, message }).then(payload => {
        if (payload) this.commentText = "";
      });
    },

    doneSelected() {
      if (!this.canDoneSelected()) return;
      this.apiPost("/api/done", { ref: this.selected.ref });
    },

    archiveSelected() {
      if (!this.selected) return;
      this.apiPost("/api/archive", { ref: this.selected.ref, archived: !this.selected.archived });
    },

    hideSelected() {
      if (!this.selected) return;
      this.apiPost("/api/hidden", { ref: this.selected.ref, hidden: !this.selected.hidden });
    },

    startTimer(task) {
      if (!this.canStartTimer(task)) return;
      this.apiPost("/api/time-start", { ref: task.ref });
    },

    stopTimer(task) {
      if (!this.canStopTimer(task)) return;
      this.apiPost("/api/time-stop", { ref: task.ref });
    },

    async stopTimerWithDuration(task) {
      const duration = String(this.timerDuration || "").trim();
      if (!this.canStopTimer(task) || !duration) return false;
      const payload = await this.apiPost("/api/time-stop", { ref: task.ref, duration });
      if (payload) this.timerDuration = "";
      return Boolean(payload);
    },

    // The timer menu opens leftward from its trigger. When the timer row wraps
    // on a narrow screen, the trigger sits near the left edge, so open rightward.
    placeTimerMenu(details) {
      const menu = details && details.querySelector(".mt-card-menu-items");
      if (!menu) return;
      menu.classList.remove("mt-menu-start");
      if (menu.getBoundingClientRect().left < 8) menu.classList.add("mt-menu-start");
    },

    async cancelTimer(task) {
      if (!this.canStopTimer(task)) return false;
      const confirmed = typeof window === "undefined" || typeof window.confirm !== "function"
        || window.confirm("Discard this timer? The running session will not be recorded.");
      if (!confirmed) return false;
      return Boolean(await this.apiPost("/api/time-cancel", { ref: task.ref }));
    },

    // ---------------------------------------------------------------------------
    // Markdown Preview, Reference Hover & Popovers
    // ---------------------------------------------------------------------------

    renderSelectedPreview() {
      if (!this.selected || typeof this.selected.body !== "string" || typeof document.createElement !== "function") return;
      const preview = document.createElement("div");
      renderPreview(this.selected, preview);
      enableTaskCheckboxes(preview);
      this.previewHtml = preview.innerHTML;
      const properties = document.createElement("div");
      renderPropertyPanels(this.selected, properties);
      this.propertiesHtml = properties.innerHTML;
    },

    async toggleTaskCheckbox(event) {
      const box = event.target.closest && event.target.closest("input.mt-task-checkbox");
      if (!box || !event.currentTarget.contains(box)) return;
      // The browser toggles the box on click; keep it as rendered until the server confirms.
      event.preventDefault();
      if (!this.selected || this.busy || this.draftBusy || this.contentEditor.draft) return;
      const index = Number(box.getAttribute("data-mt-task-index"));
      const count = event.currentTarget.querySelectorAll("input.mt-task-checkbox").length;
      return this.saveBodyKeepingProperties(() => this.apiPost("/api/checkbox", {
        ref: this.selected.ref,
        expected_hash: this.selected.content_hash,
        index,
        count,
        // The checked attribute is the rendered state; box.checked is already flipped by the click.
        // The server refuses if its marker is not in the opposite state.
        checked: !box.hasAttribute("checked"),
      }));
    },

    openReference(event) {
      const link = event.target.closest('a[href^="#mt-card-"]');
      if (!link || !event.currentTarget.contains(link)) return;
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey ||
          event.shiftKey || event.altKey || link.target === "_blank") return;
      event.preventDefault();
      this.clearHoverCard();
      let id;
      try { id = decodeURIComponent(link.getAttribute("href").slice("#mt-card-".length)); }
      catch (error) { return; }
      const target = this.tasks.find(task => task.id === id);
      if (target) this.select(target);
    },

    onReferenceHover(event) {
      const link = event.target.closest('a[href^="#mt-card-"]');
      if (!link) return;
      if (this.hoverTarget === link && this.hoverCard) return;

      this.cancelHoverTimer();
      this.hoverTimer = setTimeout(() => {
        let id;
        try { id = decodeURIComponent(link.getAttribute("href").slice("#mt-card-".length)); }
        catch (_) { return; }
        const card = this.tasks.find(task => task.id === id) ||
          (this.selected && (this.selected.references || []).find(r => r.target && r.target.id === id)?.target);
        if (!card) return;

        this.hoverTarget = link;
        this.hoverPos = this.computeHoverPos(link);
        this.hoverCard = card;
      }, 150);
    },

    onReferenceLeave(event) {
      const link = event.target.closest('a[href^="#mt-card-"]');
      if (!link) return;
      if (event.relatedTarget && link.contains(event.relatedTarget)) return;

      const popover = typeof document !== "undefined" && typeof document.querySelector === "function"
        ? document.querySelector(".mt-card-popover") : null;
      if (popover && event.relatedTarget && popover.contains(event.relatedTarget)) return;

      if (event.relatedTarget && event.relatedTarget.closest && event.relatedTarget.closest('a[href^="#mt-card-"]')) {
        return;
      }

      this.scheduleClearHoverCard();
    },

    keepHoverCard() {
      this.cancelHoverTimer();
    },

    scheduleClearHoverCard() {
      this.cancelHoverTimer();
      this.hoverTimer = setTimeout(() => {
        this.clearHoverCard();
      }, 120);
    },

    cancelHoverTimer() {
      if (this.hoverTimer) {
        clearTimeout(this.hoverTimer);
        this.hoverTimer = null;
      }
    },

    clearHoverCard() {
      this.cancelHoverTimer();
      this.hoverCard = null;
      this.hoverTarget = null;
    },

    computeHoverPos(link) {
      const rect = link.getBoundingClientRect();
      const popoverWidth = 328;
      const winWidth = typeof window !== "undefined" && window.innerWidth ? window.innerWidth : 1024;
      const winHeight = typeof window !== "undefined" && window.innerHeight ? window.innerHeight : 768;

      let left = rect.left;
      if (left + popoverWidth > winWidth - 12) {
        left = Math.max(12, winWidth - popoverWidth - 12);
      }
      if (left < 12) left = 12;

      const spaceBelow = winHeight - rect.bottom;
      const placeAbove = spaceBelow < 190 && rect.top > spaceBelow;

      return {
        top: placeAbove ? Math.round(rect.top - 6) : Math.round(rect.bottom + 6),
        left: Math.round(left),
        placeAbove,
      };
    },

    // ---------------------------------------------------------------------------
    // Card Selection & Browser History Navigation
    // ---------------------------------------------------------------------------

    changeSelectedStatus() {
      if (this.moveDestination === "done") this.doneSelected();
      else this.moveSelected();
    },

    canLeaveCard() {
      if (this.busy || this.draftBusy) return false;
      if (this.selected && this.propertyEditor.changed()) {
        if (typeof window !== "undefined" && typeof window.confirm === "function" &&
            !window.confirm("Discard unsaved property changes? Content edits are kept as a draft.")) return false;
        this.propertyEditor.begin(this.selected, this.taskDefaults);
      }
      return this.contentEditor.close(this.contentContext()) !== false;
    },

    select(task, event) {
      if (this.selected && this.selected.id !== task.id && !this.canLeaveCard()) return;
      if (!this.selected) {
        this.detailReturnFocus = event && event.currentTarget ? event.currentTarget : document.activeElement;
      }
      if (this.selectedId !== task.id) {
        history.pushState({ ...history.state, mtCard: {
          workspace: this.workspaceKey, id: task.id, previous: this.selectedId,
        } }, "", referenceHref(task));
      }
      return this.readCardLocation();
    },

    readCardLocation() {
      if (!this.ready) return;
      let id = null;
      if (location.hash.startsWith("#mt-card-")) {
        try { id = decodeURIComponent(location.hash.slice("#mt-card-".length)); }
        catch (_) {}
      }
      const target = this.tasks.find(task => task.id === id);
      if (this.selected && this.selectedId !== (target && target.id) && !this.canLeaveCard()) {
        history.replaceState({ ...history.state, mtCard: {
          workspace: this.workspaceKey, id: this.selectedId, previous: this.previousCardId,
        } }, "", referenceHref(this.selected));
        return;
      }
      let navigation = history.state && history.state.mtCard;
      if (!navigation || navigation.workspace !== this.workspaceKey || navigation.id !== id) {
        // Direct links and manually edited fragments start a fresh navigation trail.
        navigation = { workspace: this.workspaceKey, id, previous: null };
        history.replaceState({ ...history.state, mtCard: navigation }, "");
      }
      this.previousCardId = target ? navigation.previous : null;
      if (target && this.selectedId !== target.id) return this.showCard(target);
      else if (!target && this.selected) this.clearSelection(false);
    },

    canGoBackReference() {
      return Boolean(this.previousCardId && this.tasks.some(task => task.id === this.previousCardId));
    },

    backReference() {
      if (this.canGoBackReference() && this.canLeaveCard()) history.back();
    },

    showCard(task) {
      this.contentEditor.close(this.contentContext());
      this.clearHoverCard();
      if (!this.detailReturnFocus) this.detailReturnFocus = document.activeElement;
      this.selectedId = task.id;
      this.selected = task;
      this.moveDestination = task.status || "";
      this.propertyEditor.begin(task, this.taskDefaults);
      this.commentText = "";
      this.actionMessage = "";
      this.actionError = false;
      this.previewHtml = "";
      this.propertiesHtml = "";
      this.detailError = "";
      this.detailLoading = false;
      this.detailSequence += 1;
      this.$nextTick(() => {
        if (typeof window !== "undefined" && typeof window.scrollTo === "function") window.scrollTo(0, 0);
        const dialog = this.$refs.detailDialog;
        const navigationButton = dialog && dialog.querySelector(this.canGoBackReference() ? ".mt-detail-back" : ".mt-close");
        if (navigationButton) navigationButton.focus();
      });
      if (typeof task.body === "string") this.renderSelectedPreview();
      else return this.loadSelectedDetails();
    },

    async loadSelectedDetails(preserveError = false) {
      if (!this.selectedId) return;
      const identity = this.selectedId;
      const sequence = ++this.detailSequence;
      this.detailLoading = true;
      if (!preserveError) this.detailError = "";
      this.previewHtml = "";
      this.propertiesHtml = "";
      try {
        const response = await fetch("/api/item?id=" + encodeURIComponent(identity), { cache: "no-store" });
        const payload = await response.json();
        if (sequence !== this.detailSequence || this.selectedId !== identity) return;
        if (!response.ok) throw new Error(payload.error || "Could not load card content.");
        const item = payload.item;
        if (!item || item.id !== identity || typeof item.body !== "string") {
          throw new Error("The server returned incomplete card content. Retry loading.");
        }
        const propertyChanges = this.propertyEditor.changed();
        if (this.propertyEditor.baselineHash !== item.content_hash && propertyChanges) {
          this.propertyEditor.conflict = true;
        }
        if (!propertyChanges) this.propertyEditor.begin(item, this.taskDefaults);
        else if (!this.propertyEditor.hydrated) this.propertyEditor.hydrate(item, this.taskDefaults);
        this.selected = item;
        this.moveDestination = item.status || "";
        this.detailError = "";
        this.renderSelectedPreview();
        return item;
      } catch (error) {
        if (sequence === this.detailSequence && this.selectedId === identity) {
          this.detailError = error.message || "Could not load card content.";
        }
      } finally {
        if (sequence === this.detailSequence) this.detailLoading = false;
      }
    },

    clearSelection(updateUrl = true) {
      if (!this.canLeaveCard()) return;
      this.contentEditor.close(this.contentContext());
      this.clearHoverCard();
      if (updateUrl) {
        history.replaceState({ ...history.state, mtCard: {
          workspace: this.workspaceKey, id: null, previous: null,
        } }, "", location.pathname + location.search);
      }
      const returnFocus = this.detailReturnFocus;
      this.detailSequence += 1;
      this.detailLoading = false;
      this.detailError = "";
      this.selectedId = null;
      this.selected = null;
      this.previousCardId = null;
      this.detailReturnFocus = null;
      this.previewHtml = "";
      this.moveDestination = "";
      this.propertyEditor.form = null;
      this.propertyEditor.baseline = "";
      this.propertyEditor.baselineHash = "";
      this.propertyEditor.conflict = false;
      this.commentText = "";
      this.actionMessage = "";
      this.actionError = false;
      this.$nextTick(() => {
        if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === "function") returnFocus.focus();
      });
    }
  };
}
