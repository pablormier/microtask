// Board view models, grouping, formatting, timer/deadline/focus helpers.
function createBoardViews() {
  let tasksByIdSource = null;
  let tasksByIdMap = new Map();
  let focusTaskCache = null;
  let focusAlertsCache = null;
  let followupTasksCache = null;
  let viewGroupsCache = null;

  return {
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
    filtersApply() { return this.mode !== "drafts"; },
    openTasks() { return this.collectionItems("open"); },
    doingCount() { return this.doingTotal; },

    dueState(task) { return cardDueState(task, Date.now()); },
    dueDate(value) { return parseCardDueDate(value); },
    dueDayDiff(value) { return cardDueDayDifference(value, new Date()); },

    dueClass(task) {
      const state = this.dueState(task);
      return {
        "is-overdue": state === "overdue",
        "is-due-today": state === "today",
        "is-due-tomorrow": state === "next7" && this.dueDayDiff(this.dueDate(task.due)) === 1,
      };
    },

    deadlineItems() { return this.collectionItems("deadlines"); },

    deadlineRows() {
      const items = this.deadlineItems();
      const field = this.settings.deadlines.group;
      if (field === "none") return items.map(item => ({ item, key: item.id }));
      return this.groupCards(items, field).flatMap(group =>
        [{ group: group.label, key: "group:" + group.key }, ...group.items.map(item => ({ item, key: item.id }))]);
    },

    spanUntil(target) { return formatTimeUntil(target, this.now); },
    timeLeft(task) { return this.spanUntil(this.dueDate(task.due)); },
    deadlineTimeLeft(task) { return formatDeadlineTimeLeft(task.due, this.now); },
    dateLabel(value) { return formatCardDate(value); },
    deadlineDateLabel(task) { return this.dateLabel(task.due); },

    savedTimeSeconds(task) { return Math.max(0, Number((task && task.time_seconds) || 0)); },
    runningTimeSeconds(task) { return this.timerElapsedSeconds(task && this.runningTimers[task.id]); },

    timerElapsedSeconds(timer) {
      if (!timer || timer.saved) return 0;
      const current = this.nowSeconds !== null ? this.nowSeconds : this.now;
      return Math.max(0, Math.floor(current / 1000) - Number(timer.start_epoch || 0));
    },

    runningTimerList() {
      return Object.entries(this.runningTimers || {})
        .map(([id, timer]) => ({ ...timer, id }))
        .sort((a, b) => Number(a.start_epoch || 0) - Number(b.start_epoch || 0));
    },

    timerStartLabel(timer) {
      const start = String((timer && timer.start) || "");
      const now = new Date(this.now);
      const today = [now.getFullYear(), String(now.getMonth() + 1).padStart(2, "0"), String(now.getDate()).padStart(2, "0")].join("-");
      return start.slice(0, 10) === today ? start.slice(11, 16) : start.slice(0, 10) + " " + start.slice(11, 16);
    },

    timerMetaLabel(timer) {
      const elapsed = timer.saved ? "session saved" : this.formatTrackedTime(this.timerElapsedSeconds(timer));
      return [timer.ref, "started " + this.timerStartLabel(timer), timer.by, elapsed].filter(Boolean).join(" · ");
    },

    timerBadgeLabel(task) {
      const timer = task && this.runningTimers[task.id];
      if (!timer) return "";
      return timer.saved ? "Timer saved" : "Timer " + this.formatTrackedTime(this.runningTimeSeconds(task));
    },

    timerBadgeTitle(task) {
      const timer = task && this.runningTimers[task.id];
      return timer ? "Timer started " + this.timerStartLabel(timer) + (timer.by ? " by " + timer.by : "") : "";
    },

    hasTrackedTime(task) { return this.savedTimeSeconds(task) > 0; },
    savedTimeLabel(task) { return "Total " + this.formatTrackedTime(this.savedTimeSeconds(task)); },

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

    formatTrackedTime(seconds) { return formatTrackedDuration(seconds); },
    timerRunningFor(task) { return Boolean(task && this.runningTimers[task.id]); },

    showStartTimer(task) {
      return Boolean(task && task.kind === "task" && task.status !== "done" && !task.archived && !task.hidden
        && !this.timerRunningFor(task));
    },

    showStopTimer(task) { return Boolean(this.timerRunningFor(task)); },
    canStartTimer(task) { return Boolean(this.showStartTimer(task) && !this.busy); },
    canStopTimer(task) { return Boolean(this.showStopTimer(task) && !this.busy); },

    yearEnd() {
      const now = new Date(this.now);
      return new Date(now.getFullYear() + 1, 0, 1);
    },

    yearTimeLeft() { return this.spanUntil(this.yearEnd()); },

    yearProgressPercent() {
      const now = new Date(this.now);
      const start = new Date(now.getFullYear(), 0, 1);
      const end = new Date(now.getFullYear() + 1, 0, 1);
      return this.elapsedPercent(start, end);
    },

    elapsedPercent(start, end) { return elapsedPercentage(start, end, this.now); },

    yearProgressLabel() {
      const year = new Date(this.now).getFullYear();
      return year + " · " + formatProgressPercentLabel(this.yearProgressPercent());
    },

    deadlineDueEnd(item) { return parseCardDueDate(item && item.due); },

    deadlineProgressPercent(item) {
      let start = item && item.created;
      if (this.deadlineProgressStart === "now") start = this.nowAtLoad;
      if (this.deadlineProgressStart === "today") {
        const d = new Date(this.now);
        start = new Date(d.getFullYear(), d.getMonth(), d.getDate());
      }
      return this.elapsedPercent(start, this.deadlineDueEnd(item));
    },

    deadlineProgressLabel(item) { return formatProgressPercentLabel(this.deadlineProgressPercent(item)); },
    deadlineProgressTitle(item) { return this.deadlineProgressLabel(item) + " · " + this.deadlineTimeLeft(item); },
    isHardKind(kind) { return kind === "deadline"; },
    hasDeadlineDue(task) { return this.isHardKind(task && task.due_kind); },

    showCardDeadlineProgress(task) {
      return Boolean(this.showDeadlineProgress && task && task.due && task.status !== "done" && this.dueState(task) !== "overdue");
    },

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

    needsRestartStep(task) { return Boolean(task && task.needs_restart); },

    // Waiting tasks are listed under Waiting on others, so they are left out here.
    focusModeTasks() {
      const current = this.focusTask();
      const waiting = new Set(this.followupTasks().map(t => t.id));
      return this.openTasks()
        .filter(t => (!current || t.id !== current.id) && !waiting.has(t.id)
          && (this.focusContext.alerts.some(alert => alert.id === t.id) || this.needsRestartStep(t)));
    },

    focusAlertLabel(task) {
      return this.focusContext.alerts.find(alert => alert.id === task.id)?.action_label || "";
    },

    groupOptions(kind) { return cardGroupOptions(kind); },

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

    groupCards(items, field, { workflow = false } = {}) {
      if (field === "none") {
        return [{ key: "", label: "", items, countLabel: "", className: {}, empty: "No cards." }];
      }
      const columns = workflow && field === "status";
      const buckets = new Map(columns ? this.statuses.map(status => [status, []]) : []);
      for (const item of items) {
        const values = field === "tag" ? item.tags || [] : [item[field]];
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

    statusBadgeClass(status) { return status === "done" ? "mt-badge-success" : "mt-badge-accent"; },

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

    hasPriority(priority) { return Boolean(priority && priority !== "none"); },
    priorityLabel(priority) { return priority === "none" ? "Unassigned" : priority; },
    priorityClass(priority) { return cardPriorityClass(priority); },

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

    openStatuses() { return this.statuses.filter(status => status !== "done"); },

    canMoveSelected() {
      return Boolean(this.selected && this.selected.kind === "task" && this.selected.status !== "done");
    },

    canDoneSelected() { return this.canMoveSelected(); },
  };
}
