const MT_CONFIG = JSON.parse(document.getElementById("mt-config").textContent);



function taskBoard() {
  // Closure-scoped caches stay outside Alpine's reactive proxy.
  let tasksByIdSource = null;
  let tasksByIdMap = new Map();
  let focusTaskCache = null;
  let focusAlertsCache = null;
  let followupTasksCache = null;
  let viewGroupsCache = null;

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
    doingTotal: 0,
    referenceRevision: "",
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
    ...createFilterController(),
    ...createDraftCoordinator(),
    ...createDetailController(),
    filtersOpen: false,
    ...createPicker(),
    showHidden: false,
    priorities: [],
    types: [],
    projects: [],
    filters: defaultFilters(),
    // Query parameters from the page address that no control shows, such as created_since from `mt web`.
    extraQuery: {},
    needsReviewOnly: false,
    detailLoading: false,
    detailError: "",
    detailSequence: 0,
    selectedId: null,
    selected: null,
    previousCardId: null,
    requestedCardId: null,
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
    draftWorkspaceStatus: "",
    draftBusy: false,
    syncWarning: "",
    restartNotice: "",
    detailReturnFocus: null,
    hoverCard: null,
    hoverPos: { top: 0, left: 0, placeAbove: false },
    hoverTimer: null,
    hoverTarget: null,
    now: Date.now(),
    nowSeconds: null,
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
      setInterval(() => {
        if (typeof document !== "undefined" && document.hidden) return;
        this.now = Date.now();
      }, 60000);
      setInterval(() => {
        if (typeof document !== "undefined" && document.hidden) return;
        if (this.runningTimers && Object.keys(this.runningTimers).length > 0) {
          this.nowSeconds = Date.now();
        } else if (this.nowSeconds !== null) {
          this.nowSeconds = null;
        }
      }, 1000);
      window.addEventListener("load", () => {
        if (this.selected) this.renderSelectedPreview();
      }, { once: true });
      if (typeof this.$watch === "function") {
        for (const field of ["filters", "extraQuery", "showHidden", "needsReviewOnly"]) {
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
      const refreshVisibleBoard = () => {
        if (!document.hidden) {
          const ts = Date.now();
          this.now = ts;
          if (this.runningTimers && Object.keys(this.runningTimers).length > 0) {
            this.nowSeconds = ts;
          }
        }
        return this.refreshItemsAutomatically();
      };
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
        if (!this.ready) {
          this.restoreLocalWorkspaceDrafts();
        }
        await Promise.all([this.refreshItems(), this.loadWorkspaceDrafts()]);
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

    tasksById() {
      if (tasksByIdSource !== this.tasks) {
        tasksByIdSource = this.tasks;
        tasksByIdMap = new Map(this.tasks.map(item => [item.id, item]));
      }
      return tasksByIdMap;
    },

    collectionItems(name) {
      const byId = this.tasksById();
      return (this.collections[name] || []).map(id => byId.get(id)).filter(Boolean);
    },

    isCardsView() { return this.mode === "tasks" || this.mode === "notes" || this.mode === "archive"; },
    // Every tab except Drafts shows filtered cards, so it also shows the filter controls.
    filtersApply() { return this.mode !== "drafts"; },
    openTasks() { return this.collectionItems("open"); },

    // Counted by the server: `tasks` holds only the cards the current filters show.
    doingCount() { return this.doingTotal; },

    // ---------------------------------------------------------------------------
    // Due Dates, Deadlines & Countdown Calculations
    // ---------------------------------------------------------------------------

    dueState(task) {
      return cardDueState(task, Date.now());
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
      return formatDeadlineTimeLeft(task.due, this.now);
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
      const current = this.nowSeconds !== null ? this.nowSeconds : this.now;
      return Math.max(0, Math.floor(current / 1000) - Number(timer.start_epoch || 0));
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
      return year + " · " + formatProgressPercentLabel(this.yearProgressPercent());
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
      return formatProgressPercentLabel(this.deadlineProgressPercent(item));
    },

    deadlineProgressTitle(item) {
      return this.deadlineProgressLabel(item) + " · " + this.deadlineTimeLeft(item);
    },

    isHardKind(kind) {
      return kind === "deadline";
    },

    hasDeadlineDue(task) {
      return this.isHardKind(task && task.due_kind);
    },

    showCardDeadlineProgress(task) {
      return Boolean(this.showDeadlineProgress && task && task.due && task.status !== "done");
    },

    // ---------------------------------------------------------------------------
    // Focus Mode & Task Prioritization
    // ---------------------------------------------------------------------------

    focusTask() {
      const openIds = this.collections.open;
      const tasks = this.tasks;
      const ctx = this.focusContext;
      const cache = focusTaskCache;
      if (cache && cache.openIds === openIds && cache.tasks === tasks && cache.ctx === ctx) {
        return cache.value;
      }
      const byId = this.tasksById();
      const openSet = new Set(openIds || []);
      let value = null;
      const selectedId = ctx && ctx.selected && ctx.selected.id;
      if (selectedId && openSet.has(selectedId)) {
        value = byId.get(selectedId) || null;
      }
      if (!value && ctx && Array.isArray(ctx.candidates)) {
        for (const candidate of ctx.candidates) {
          if (openSet.has(candidate.id)) {
            const task = byId.get(candidate.id);
            if (task) {
              value = task;
              break;
            }
          }
        }
      }
      focusTaskCache = { openIds, tasks, ctx, value };
      return value;
    },

    focusReason(task) {
      if (!task) return "";
      if (task.id === this.focusContext.selected?.id) return this.focusContext.selected.reason;
      return this.focusContext.candidates.find(t => t.id === task.id)?.reason || "";
    },

    focusAlerts() {
      const openIds = this.collections.open;
      const ctx = this.focusContext;
      const selectedId = this.focusTask()?.id;
      const cache = focusAlertsCache;
      if (cache && cache.openIds === openIds && cache.ctx === ctx && cache.selectedId === selectedId) {
        return cache.value;
      }
      const visibleIds = new Set(openIds || []);
      const value = (ctx.alerts || []).filter(t => t.id !== selectedId && visibleIds.has(t.id));
      focusAlertsCache = { openIds, ctx, selectedId, value };
      return value;
    },

    followupTasks() {
      const openIds = this.collections.open;
      const tasks = this.tasks;
      const ctx = this.focusContext;
      const cache = followupTasksCache;
      if (cache && cache.openIds === openIds && cache.tasks === tasks && cache.ctx === ctx) {
        return cache.value;
      }
      const byId = this.tasksById();
      const openSet = new Set(openIds || []);
      const value = (this.focusContext.followup_ids || [])
        .filter(id => openSet.has(id))
        .map(id => byId.get(id))
        .filter(Boolean);
      followupTasksCache = { openIds, tasks, ctx, value };
      return value;
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

    // Cards of the current Tasks, Notes or Archive tab (the Archive's Kind picks its collection), grouped.
    viewGroups() {
      const mode = this.mode;
      const kind = this.viewKind();
      const group = this.settings[mode].group;
      const collectionName = this.mode === "archive" ? ARCHIVE_COLLECTIONS[this.viewKind()] : this.mode;
      const ids = this.collections[collectionName];
      const tasks = this.tasks;
      const statuses = this.statuses;
      const topicTree = this.topicTree;
      const doingTotal = this.doingTotal;
      const maxDoingTasks = this.maxDoingTasks;
      const statusLabels = this.statusLabels;
      const priorityOrder = this.priorityOrder;
      const dueKinds = this.dueKinds;
      const cache = viewGroupsCache;
      if (cache && cache.mode === mode && cache.kind === kind && cache.group === group &&
          cache.ids === ids && cache.tasks === tasks && cache.statuses === statuses &&
          cache.topicTree === topicTree && cache.doingTotal === doingTotal &&
          cache.maxDoingTasks === maxDoingTasks && cache.statusLabels === statusLabels &&
          cache.priorityOrder === priorityOrder && cache.dueKinds === dueKinds) {
        return cache.value;
      }
      const items = this.collectionItems(collectionName);
      const groups = this.groupCards(items, group, { workflow: mode !== "archive" });
      const value = groups.length ? groups : this.groupCards([], "none");
      viewGroupsCache = {
        mode, kind, group, ids, tasks, statuses, topicTree,
        doingTotal, maxDoingTasks, statusLabels, priorityOrder, dueKinds, value,
      };
      return value;
    },

    // Ordered {key, label, items, countLabel, className, empty} groups. Groups with
    // no value come last as "Ungrouped"; a card with several tags is in each tag's
    // group. With workflow, status groups are the policy statuses, empty ones too,
    // with the WIP label.
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
        return {
          key,
          label: key === "" ? "Ungrouped" : this.groupLabel(field, key, workflow),
          items: all,
          countLabel: String(all.length),
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
      return cardPriorityClass(priority);
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
      this.doingTotal = meta.doing_count || 0;
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
      this.projects = Array.from(new Set(this.workspaceProjects)).sort();
      // A linked project may differ in case; use the workspace spelling. One with no
      // cards yet still shows in the filter (see filterChoices).
      this.filters.project.forEach((linked, index) => {
        if (this.projects.includes(linked)) return;
        const spelling = this.projects.find(name => name.toLowerCase() === linked.toLowerCase());
        if (spelling) this.filters.project.splice(index, 1, spelling);
      });
    },

    // `sentQuery` is the query the request carried; a response to another query keeps the collections.
    applyPayload(payload, fromQuery = false, sentQuery = {}) {
      this.now = Date.now();
      this.nowSeconds = null;
      const matchesQuery = fromQuery || JSON.stringify(sentQuery) === JSON.stringify(this.collectionQuery());
      if (!fromQuery) {
        this.querySequence += 1;
        this.queryLoading = false;
      }
      if (payload.collections && matchesQuery) this.collections = payload.collections;
      if (!fromQuery && !matchesQuery) this.scheduleQuery(0);
      // `tasks` is only what the filters show, so the server reports changes to any card's reference.
      const revision = (payload.meta || {}).reference_revision || "";
      const referencesChanged = this.selected && revision !== this.referenceRevision;
      this.referenceRevision = revision;
      const incoming = payload.tasks || [];
      // A response requested before a card was opened cannot tell whether that card still
      // exists, so keep the card and ask again instead of closing it.
      const unasked = Array.isArray(payload.include)
        ? this.includedCardIds().filter(id => !payload.include.includes(id) && !incoming.some(item => item.id === id))
        : [];
      this.tasks = incoming.concat(this.tasks.filter(item => unasked.includes(item.id)));
      if (unasked.length) this.scheduleQuery(0);
      this.statuses = payload.statuses || this.statuses;
      if (Object.prototype.hasOwnProperty.call(payload, "drafts")) this.drafts = payload.drafts || [];
      if (Object.prototype.hasOwnProperty.call(payload, "sync_warning")) {
        this.syncWarning = payload.sync_warning || "";
      }
      if (Object.prototype.hasOwnProperty.call(payload, "restart_notice")) {
        this.restartNotice = payload.restart_notice || "";
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
        for (const id of this.includedCardIds()) params.append("include", id);
        const response = await fetch("/api/items?" + params, { cache: "no-store" });
        const payload = await response.json();
        if (sequence !== this.querySequence) return;
        if (!response.ok) throw new Error(payload.error || "Could not load the workspace.");
        await this.applyPayload(payload, true);
        if (sequence !== this.querySequence) return;
        if (payload.query_aliased) {
          // The address used older spellings; show the query as the server understood it.
          const canonical = new URLSearchParams();
          for (const [name, value] of Object.entries(payload.query || {})) appendParams(canonical, name, value);
          this.applyFilterParams(canonical);
        }
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
        const query = this.collectionQuery();
        const response = await fetch(path, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Microtask-Token": this.apiToken },
          body: JSON.stringify({ ...(body || {}), query, include: this.includedCardIds() })
        });
        const payload = await response.json();
        if (!response.ok) {
          throw new Error(payload.error || "Action failed");
        }
        await this.applyPayload(payload, false, query);
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

  };
}
