// Shared HTTP JSON helper and board API/action methods.
async function readJsonResponse(response) {
  if (typeof response.text === "function") {
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch (_) {
      return {};
    }
  }
  if (typeof response.json === "function") {
    try {
      return await response.json();
    } catch (_) {
      return {};
    }
  }
  return {};
}

// The board registers what happens when the server reports that the signed-in account is no longer the
// one this page was loaded for (a 409 with member_changed). Requests only report it; the board decides.
let memberChangeHandler = null;
function setMemberChangeHandler(handler) {
  memberChangeHandler = typeof handler === "function" ? handler : null;
}
function reportMemberChanged() {
  if (!memberChangeHandler) return;
  try { memberChangeHandler(); } catch (_) {}
}

async function postJson(path, body, token) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Microtask-Token": token || "" },
    body: JSON.stringify(body || {}),
  });
  const payload = await readJsonResponse(response);
  // The write was refused before anything changed, so it is neither a conflict nor a failure to retry.
  const memberChanged = Boolean(payload.member_changed);
  if (memberChanged) reportMemberChanged();
  return {
    ok: response.ok,
    status: response.status,
    memberChanged,
    conflict: !memberChanged && Boolean(payload.conflict || response.status === 409),
    missing: response.status === 404,
    error: payload.error || (response.ok ? "" : `Request failed (${response.status})`),
    payload,
  };
}

function createBoardApi() {
  return {
    applyPayload(payload, fromQuery = false, sentQuery = {}) {
      // A payload for another signed-in person is never applied: the page restarts instead.
      if (this.payloadIsForOtherMember(payload.meta)) return;
      this.now = Date.now();
      this.nowSeconds = null;
      const matchesQuery = fromQuery || JSON.stringify(sentQuery) === JSON.stringify(this.collectionQuery());
      if (!fromQuery) {
        this.querySequence += 1;
        this.queryLoading = false;
      }
      if (payload.collections && matchesQuery) this.collections = payload.collections;
      // The panel's counts depend on the filters, so take them only from a response to the current ones.
      if (payload.meta && matchesQuery) this.topicTrees = payload.meta.topic_trees || {};
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
      if (Object.prototype.hasOwnProperty.call(payload, "sync_status") && payload.sync_status) {
        this.syncing = Boolean(payload.sync_status.syncing);
        this.lastSyncedAt = payload.sync_status.last_synced_at || "";
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
      if (this.memberChanging) return;
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
        const payload = await readJsonResponse(response);
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
      if (this.memberChanging) return null;
      this._busyCount = (this._busyCount || 0) + 1;
      this.busy = true;
      this.actionMessage = "";
      this.actionError = false;
      try {
        const query = this.collectionQuery();
        const result = await postJson(path, { ...(body || {}), query, include: this.includedCardIds() }, this.apiToken);
        if (!result.ok) {
          throw new Error(result.error || "Action failed");
        }
        const payload = result.payload;
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
        this._busyCount = Math.max(0, (this._busyCount || 1) - 1);
        this.busy = this._busyCount > 0;
      }
    },

    markReviewed(card) {
      if (!card || !card.needs_review || this.busy) return;
      this.apiPost("/api/review", {
        ref: card.ref,
        expected_hash: String(card.content_hash || ""),
      });
    },

    moveSelected() {
      if (!this.canMoveSelected() || !this.moveDestination || this.moveDestination === this.selected.status) return;
      this.apiPost("/api/move", { ref: this.selected.ref, status: this.moveDestination });
    },

    canOrderCard(card) {
      return Boolean(card && !card.archived && !(card.kind === "task" && card.status === "done"));
    },

    cardGroupNeighbors(card) {
      if (!this.canOrderCard(card) || typeof this.viewGroups !== "function") return { prev: null, next: null };
      for (const group of this.viewGroups()) {
        const idx = group.items.findIndex(item => item.id === card.id);
        if (idx === -1) continue;
        const peers = group.items.filter(item => item.kind === card.kind && (card.kind !== "task" || item.status === card.status));
        const peerIdx = peers.findIndex(item => item.id === card.id);
        return {
          prev: peerIdx > 0 ? peers[peerIdx - 1] : null,
          next: peerIdx >= 0 && peerIdx < peers.length - 1 ? peers[peerIdx + 1] : null,
        };
      }
      return { prev: null, next: null };
    },

    orderCard(card, opts = {}) {
      if (!this.canOrderCard(card) || this.busy) return Promise.resolve(null);
      if (this.settings[this.mode] && this.settings[this.mode].sort && this.settings[this.mode].sort !== "default") {
        this.settings[this.mode].sort = "default";
      }
      return this.apiPost("/api/order", { ref: card.ref, ...opts });
    },

    moveCardStep(card, delta) {
      const { prev, next } = this.cardGroupNeighbors(card);
      if (delta < 0 && prev) return this.orderCard(card, { before: prev.ref });
      if (delta > 0 && next) return this.orderCard(card, { after: next.ref });
      return Promise.resolve(null);
    },

    groupHasManualOrder(group) {
      const sort = this.settings[this.mode]?.sort || "default";
      const inPanelView = (this.mode === "tasks" && this.settings.tasks.group === "status")
        || (this.mode === "notes" && this.settings.notes.group === "none");
      if (!inPanelView || sort !== "default" || !group || !Array.isArray(group.items)) return false;
      return group.items.some(item => Boolean(item.order) && this.canOrderCard(item));
    },

    resetGroupOrder(group) {
      if (!group || !Array.isArray(group.items)) return Promise.resolve(null);
      const target = group.items.find(item => Boolean(item.order) && this.canOrderCard(item));
      if (!target) return Promise.resolve(null);
      return this.orderCard(target, { position: "clear_panel" });
    },

    commentSelected() {
      const message = this.commentText.trim();
      if (!this.selected || !message || this.attachmentUploading) return;
      this.saveBodyKeepingProperties(() => this.apiPost("/api/comment", { ref: this.selected.ref, message })).then(payload => {
        if (payload) {
          this.commentText = "";
          if (this.commentMention) this.commentMention.closeMention();
          this.notify("Comment added");
        }
      });
    },

    saveEditComment() {
      const message = this.commentEditText.trim();
      if (!this.selected || this.commentEditingIndex < 0 || !message || this.attachmentUploading || this.busy) return;
      const index = this.commentEditingIndex;
      const original = Array.isArray(this.selected.comments) ? this.selected.comments[index] : null;
      if (original && typeof original.text === "string" && original.text.trim() === message) {
        this.cancelEditComment();
        return;
      }
      this.saveBodyKeepingProperties(() => this.apiPost("/api/comment-edit", {
        ref: this.selected.ref,
        index,
        message,
        expected_hash: String(this.selected.content_hash || ""),
      })).then(payload => {
        if (payload) {
          this.cancelEditComment();
          this.notify("Comment updated");
        }
      });
    },

    deleteComment(entry) {
      if (!this.selected || !entry || this.busy) return;
      if (typeof window !== "undefined" && typeof window.confirm === "function" &&
          !window.confirm("Delete this comment?")) return;
      this.saveBodyKeepingProperties(() => this.apiPost("/api/comment-delete", {
        ref: this.selected.ref,
        index: entry.index,
        expected_hash: String(this.selected.content_hash || ""),
      })).then(payload => {
        if (payload) {
          if (this.commentEditingIndex === entry.index) {
            this.cancelEditComment();
          } else if (this.commentEditingIndex > entry.index) {
            this.commentEditingIndex -= 1;
          }
          this.notify("Comment deleted");
        }
      });
    },

    _attachmentTargetAccessors(target) {
      if (target === "content") {
        return {
          getText: () => this.contentEditor.text,
          setText: next => {
            this.contentEditor.text = next;
            this.contentEditor.persist(this.contentContext());
            this.contentEditor.schedulePreview(this.contentContext());
          },
        };
      }
      if (target === "composer") {
        return {
          getText: () => (this.captureComposer.draft ? this.captureComposer.draft.body : ""),
          setText: next => {
            if (!this.captureComposer.draft) return;
            this.captureComposer.draft.body = next;
            this.captureComposer.persist(this.captureContext());
            this.captureComposer.schedulePreview(this.captureContext());
          },
        };
      }
      if (target === "comment") {
        return {
          getText: () => this.commentText,
          setText: next => { this.commentText = next; },
        };
      }
      if (target === "commentEdit") {
        return {
          getText: () => this.commentEditText,
          setText: next => { this.commentEditText = next; },
        };
      }
      return null;
    },

    async uploadAttachmentsForTarget(target, files, textarea) {
      if (this.memberChanging) return;
      const accessors = this._attachmentTargetAccessors(target);
      if (!accessors || !files || !files.length) return;
      this._attachmentUploadCount = (this._attachmentUploadCount || 0) + 1;
      this.attachmentUploading = true;
      try {
        await uploadAndInsertAttachments({
          files,
          textarea,
          getText: accessors.getText,
          setText: accessors.setText,
          apiToken: this.apiToken,
          notify: (msg, err) => this.notify(msg, err),
          nextTick: fn => this.$nextTick(fn),
        });
      } finally {
        this._attachmentUploadCount = Math.max(0, (this._attachmentUploadCount || 1) - 1);
        this.attachmentUploading = this._attachmentUploadCount > 0;
      }
    },

    handleAttachmentPaste(target, event, textarea) {
      const files = extractClipboardFiles(event);
      if (!files.length) return;
      event.preventDefault();
      this.uploadAttachmentsForTarget(target, files, textarea);
    },

    handleAttachmentDragOver(target, event) {
      const types = event && event.dataTransfer && event.dataTransfer.types;
      if (types && Array.from(types).includes("Files")) {
        event.preventDefault();
        this.attachmentDragTarget = target;
      }
    },

    handleAttachmentDragLeave(target) {
      if (this.attachmentDragTarget === target) {
        this.attachmentDragTarget = "";
      }
    },

    handleAttachmentDrop(target, event, textarea) {
      this.attachmentDragTarget = "";
      const files = extractDropFiles(event);
      if (!files.length) return;
      event.preventDefault();
      this.uploadAttachmentsForTarget(target, files, textarea);
    },

    handleAttachmentPick(target, event, textarea) {
      const input = event && event.target;
      const files = input && input.files ? Array.from(input.files) : [];
      if (input) input.value = "";
      if (!files.length) return;
      this.uploadAttachmentsForTarget(target, files, textarea);
    },

    doneSelected() {
      if (!this.canDoneSelected()) return;
      this.apiPost("/api/done", { ref: this.selected.ref });
    },

    showConvertCard(card) {
      return Boolean(card && (card.kind === "note" || (card.kind === "task" && card.status !== "done")));
    },

    convertDisabledReason(card) {
      if (!card) return "";
      if (this.busy || this.draftBusy || this.detailLoading) {
        return "Finish the current action before converting";
      }
      if (this.contentEditor && this.contentEditor.open) {
        return "Close the content editor before converting";
      }
      if (this.propertyEditor && typeof this.propertyEditor.changed === "function" && this.propertyEditor.changed()) {
        return "Save or reset property changes before converting";
      }
      if (card.kind === "task") {
        if (card.status === "done") return "Done tasks cannot be converted to notes";
        if (card.claimed_by) return "Release the claim before converting to a note";
        if (this.timerRunningFor(card) || this.hasTrackedTime(card)) {
          return "Tasks with tracked time or a running timer cannot be converted to a note";
        }
      }
      return "";
    },

    canConvertCard(card) {
      return Boolean(this.showConvertCard(card) && !this.convertDisabledReason(card));
    },

    async convertSelected() {
      if (!this.canConvertCard(this.selected)) return null;
      const destination = this.selected.kind === "note" ? "task" : "note";
      const payload = await this.apiPost("/api/convert", {
        ref: this.selected.ref,
        destination,
        expected_hash: this.selected.content_hash || "",
      });
      if (payload && this.selected && this.propertyEditor && typeof this.propertyEditor.begin === "function") {
        this.propertyEditor.begin(this.selected, this.taskDefaults);
      }
      return payload;
    },

    archiveSelected() {
      if (!this.selected || this.busy) return;
      this.apiPost("/api/archive", { ref: this.selected.ref, archived: !this.selected.archived });
    },

    hideSelected() {
      if (!this.selected || this.busy) return;
      this.apiPost("/api/hidden", { ref: this.selected.ref, hidden: !this.selected.hidden });
    },

    startTimer(task) {
      if (!this.canStartTimer(task)) return;
      this.apiPost("/api/time-start", { ref: task.ref, parallel: true });
    },

    async stopTimer(task, duration = "") {
      if (!this.canStopTimer(task)) return false;
      const payload = await this.apiPost("/api/time-stop", { ref: task.ref, duration });
      if (payload) this.timerDuration = "";
      return Boolean(payload);
    },

    // The timer menu's form: stop with the typed duration instead of the running time.
    stopTimerWithDuration(task) {
      const duration = String(this.timerDuration || "").trim();
      if (!duration) return Promise.resolve(false);
      return this.stopTimer(task, duration);
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
