const MT_CONFIG = JSON.parse(document.getElementById("mt-config").textContent);
const MEMBER_CHANGED_FLAG = "mt-member-changed";
const MEMBER_RELOADED_AT = "mt-member-reloaded-at";
const MEMBER_RELOAD_WINDOW_MS = 15000;



function taskBoard() {
  // Closure-scoped caches stay outside Alpine's reactive proxy.
  let visibleTopicTreeSource = null;
  let visibleTopicExpandedSource = null;
  let visibleTopicNodesCache = [];

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
    readyAction: null,
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
    topicTrees: {},
    topicTreeState: { tasks: false, notes: true, archive: false },
    expandedTopics: {},
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
    memberChanging: false,
    restoredStashId: "",
    demo: false,
    showYearProgress: true,
    showDeadlineProgress: true,
    deadlineProgressStart: "created",
    mode: "tasks",
    layout: "vertical",
    settings: normalizeViewSettings({}),
    textSize: "medium",
    theme: "system",
    columnIndex: 0,
    ...createFilterController(),
    ...createDraftCoordinator(),
    ...createDetailController(),
    ...createQuickSearchController(),
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
    textPropertyHtml: {},
    textPropertyEditing: "",
    moveDestination: "",
    contentEditor: createContentEditor(),
    commentText: "",
    attachmentDragTarget: "",
    _attachmentUploadCount: 0,
    attachmentUploading: false,
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
    hoverDisplayCard: null,
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
      let savedTheme = "system";
      try { savedTheme = localStorage.getItem("mt-theme-v1") || "system"; } catch (_) {}
      this.setTheme(savedTheme);
      try {
        window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.applyTheme());
      } catch (_) {}
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
      this.expandTopicAncestors(this.filters.topic);
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
        this.$watch("filters.topic", (paths) => this.expandTopicAncestors(paths));
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
      window.addEventListener("keydown", event => {
        if (!this.memberChanging) this.handleQuickSearchShortcut(event);
      });
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
      if (typeof setMemberChangeHandler === "function") setMemberChangeHandler(() => this.leaveForOtherMember());
      return this.loadWorkspace();
    },

    async loadWorkspace() {
      if (this.workspaceLoading) return;
      this.workspaceLoading = true;
      this.loading = true;
      this.loadError = "";
      try {
        await this.refreshItems();
        await this.loadWorkspaceDrafts();
        this.ready = true;
        this.showMemberChangedNotice();
        this.restoreCaptureStash();
        this.$nextTick(() => {
          this.readCardLocation();
          const action = this.readyAction;
          this.readyAction = null;
          if (action) action();
        });
      } catch (error) {
        this.loadError = error.message || "Could not load the workspace.";
      } finally {
        this.loading = false;
        this.workspaceLoading = false;
      }
    },

    updatePageTitle() {
      if (typeof document === "undefined") return;
      if (this.memberChanging) {
        document.title = "MicroTask";
      } else if (this.selected && this.selected.title) {
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
      let savedTree = {};
      try { savedTree = JSON.parse(localStorage.getItem("mt-topic-tree-v1") || "{}"); } catch (_) {}
      this.topicTreeState = {
        tasks: typeof savedTree.tasks === "boolean" ? savedTree.tasks : false,
        notes: typeof savedTree.notes === "boolean" ? savedTree.notes : true,
        archive: typeof savedTree.archive === "boolean" ? savedTree.archive : false
      };
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

    // On phones the board shows one column at a time; the pager follows the column nearest the left edge.
    trackColumn(el) {
      const columns = el ? el.querySelectorAll(":scope > .mt-column") : [];
      const left = el ? el.getBoundingClientRect().left : 0;
      let nearest = 0;
      let best = Infinity;
      columns.forEach((column, index) => {
        const distance = Math.abs(column.getBoundingClientRect().left - left);
        if (distance < best) { best = distance; nearest = index; }
      });
      if (nearest === this.columnIndex) return;
      this.columnIndex = nearest;
      const item = document.querySelectorAll(".mt-column-pager-item")[nearest];
      if (item && item.scrollIntoView) item.scrollIntoView({ block: "nearest", inline: "nearest" });
    },

    scrollToColumn(index) {
      const groups = this.$refs && this.$refs.groups;
      const column = groups && groups.querySelectorAll(":scope > .mt-column")[index];
      if (!column) return;
      let smooth = true;
      try { smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (_) {}
      const offset = column.getBoundingClientRect().left - groups.getBoundingClientRect().left;
      groups.scrollTo({ left: groups.scrollLeft + offset - 12, behavior: smooth ? "smooth" : "auto" });
      this.columnIndex = index;
    },

    // `theme` is the viewer's choice; data-theme on <html> is what it resolves to now.
    setTheme(choice) {
      this.theme = ["system", "light", "dark"].includes(choice) ? choice : "system";
      try { localStorage.setItem("mt-theme-v1", this.theme); } catch (_) {}
      this.applyTheme();
    },

    applyTheme() {
      let dark = this.theme === "dark";
      if (this.theme === "system") {
        try { dark = window.matchMedia("(prefers-color-scheme: dark)").matches; } catch (_) {}
      }
      document.documentElement.dataset.theme = dark ? "dark" : "light";
    },

    setTextSize(size) {
      this.textSize = ["small", "medium", "large"].includes(size) ? size : "medium";
      document.documentElement.dataset.textSize = this.textSize;
      try { localStorage.setItem("mt-text-size-v2", this.textSize); } catch (_) {}
    },

    topicTreeOpen() {
      return Boolean(this.topicTreeState[this.mode]);
    },

    toggleTopicTree() {
      if (!(this.mode in this.topicTreeState)) return;
      this.topicTreeState = { ...this.topicTreeState, [this.mode]: !this.topicTreeState[this.mode] };
      try { localStorage.setItem("mt-topic-tree-v1", JSON.stringify(this.topicTreeState)); } catch (_) {}
    },

    toggleTopicExpanded(path) {
      const key = String(path || "").toLowerCase();
      if (!key) return;
      this.expandedTopics = { ...this.expandedTopics, [key]: !this.expandedTopics[key] };
    },

    expandTopicAncestors(paths) {
      const list = Array.isArray(paths) ? paths : [];
      if (!list.length) return;
      const next = { ...this.expandedTopics };
      let changed = false;
      for (const path of list) {
        const parts = topicSegments(path);
        for (let i = 1; i < parts.length; i += 1) {
          const ancestor = parts.slice(0, i).join("/").toLowerCase();
          if (!next[ancestor]) {
            next[ancestor] = true;
            changed = true;
          }
        }
      }
      if (changed) this.expandedTopics = next;
    },

    visibleTopicNodes() {
      // The panel counts the cards of the current view; pickers keep the workspace-wide tree.
      const tree = this.topicTrees[this.mode] || this.topicTree;
      if (visibleTopicTreeSource !== tree || visibleTopicExpandedSource !== this.expandedTopics) {
        visibleTopicTreeSource = tree;
        visibleTopicExpandedSource = this.expandedTopics;
        visibleTopicNodesCache = visibleTopicTreeNodes(tree, this.expandedTopics);
      }
      return visibleTopicNodesCache;
    },

    selectTopicNode(path, event) {
      if (event && (event.metaKey || event.ctrlKey)) {
        this.toggleFilter("topic", path);
        return;
      }
      if (this.filters.topic.length === 1 && this.filters.topic[0].toLowerCase() === String(path || "").toLowerCase()) {
        this.clearFilter("topic");
      } else {
        this.filters.topic = [path];
      }
    },

    ...createBoardViews(),
    ...createBoardApi(),

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

    whenReady(action) {
      if (this.ready) action();
      else this.readyAction = action;
    },

    beginNewCapture(event) {
      if (!this.ready) return;
      this.captureComposer.beginNew(this.captureContext(), event, this.mode === "tasks" || this.mode === "focus" ? "task" : "note");
    },

    hasUnsavedChanges() {
      // Kept in browser storage already; the restart must not wait for a prompt.
      if (this.memberChanging) return false;
      if (this.selected && this.propertyEditor.changed()) return true;
      if (this.contentEditor.open && this.contentEditor.text !== this.contentEditor.original) return true;
      return this.captureComposer.hasUnpersisted(this.captureContext());
    },

    // A payload names the signed-in person through its workspace key. When that is not the person this
    // page was loaded for, nothing from it is applied: unsaved work is kept in this browser under the
    // current key and the page restarts, so that no state of the previous person is left on screen.
    payloadIsForOtherMember(meta) {
      if (this.memberChanging) return true;
      const incoming = meta && meta.workspace_key ? String(meta.workspace_key) : "";
      if (!incoming || !this.workspaceKey || incoming === this.workspaceKey) return false;
      this.leaveForOtherMember();
      return true;
    },

    leaveForOtherMember() {
      if (this.memberChanging) return;
      this.memberChanging = true;
      this.updatePageTitle();
      // Each step on its own: a failure in one must not stop the restart or the other saves.
      for (const step of [() => this.keepEditsForReload(), () => this.stashCaptureDraft(),
        () => this.getDraftStore().suspend()]) {
        try { step(); } catch (_) {}
      }
      // A second restart within a few seconds means the sign-in keeps changing or the page token never
      // matches. Reloading again would loop, so stop and leave the page blocked with a message instead.
      let recent = false;
      try {
        const last = Number(sessionStorage.getItem(MEMBER_RELOADED_AT) || 0);
        recent = last > 0 && Date.now() - last < MEMBER_RELOAD_WINDOW_MS;
        sessionStorage.setItem(MEMBER_RELOADED_AT, String(Date.now()));
      } catch (_) {}
      if (recent) return;
      try { sessionStorage.setItem(MEMBER_CHANGED_FLAG, "1"); } catch (_) {}
      this.reloadPage();
    },

    reloadPage() {
      window.location.reload();
    },

    showMemberChangedNotice() {
      let flagged = false;
      try {
        flagged = sessionStorage.getItem(MEMBER_CHANGED_FLAG) === "1";
        if (flagged) sessionStorage.removeItem(MEMBER_CHANGED_FLAG);
      } catch (_) {}
      if (flagged) this.notify("The signed-in account changed, so the page was reloaded. Unsaved edits stay in this browser with their account.", true);
    },

    refreshDerivedLists(meta) {
      if (this.payloadIsForOtherMember(meta)) return;
      const incomingWorkspaceKey = meta.workspace_key ? String(meta.workspace_key) : "";
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
      this.demo = Boolean(meta.demo);
      this.showYearProgress = meta.show_year_progress !== false;
      this.showDeadlineProgress = meta.show_deadline_progress !== false;
      this.deadlineProgressStart = meta.deadline_progress_start || "created";
      if (incomingWorkspaceKey) this.workspaceKey = incomingWorkspaceKey;
      // The write token belongs to the person the page was loaded for; the check above keeps it so.
      if (!incomingWorkspaceKey || incomingWorkspaceKey === this.workspaceKey) this.apiToken = meta.api_token || this.apiToken;
      this.priorities = meta.priority_order && meta.priority_order.length
        ? meta.priority_order
        : Array.from(new Set(this.tasks.map(t => t.priority).filter(Boolean)));
      this.types = Object.keys(this.taskTypePolicy).length
        ? Object.keys(this.taskTypePolicy)
        : Array.from(new Set(this.tasks.map(t => t.type || t.kind).filter(Boolean)));
      this.projects = Array.from(new Set(this.workspaceProjects)).sort();
      this.filters.project.forEach((linked, index) => {
        if (this.projects.includes(linked)) return;
        const spelling = this.projects.find(name => name.toLowerCase() === linked.toLowerCase());
        if (spelling) this.filters.project.splice(index, 1, spelling);
      });
    },
  };
}
