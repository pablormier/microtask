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

async function postJson(path, body, token) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Microtask-Token": token || "" },
    body: JSON.stringify(body || {}),
  });
  const payload = await readJsonResponse(response);
  return {
    ok: response.ok,
    status: response.status,
    conflict: Boolean(payload.conflict || response.status === 409),
    missing: response.status === 404,
    error: payload.error || (response.ok ? "" : `Request failed (${response.status})`),
    payload,
  };
}

function createBoardApi() {
  return {
    applyPayload(payload, fromQuery = false, sentQuery = {}) {
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

    commentSelected() {
      const message = this.commentText.trim();
      if (!this.selected || !message || this.attachmentUploading) return;
      this.apiPost("/api/comment", { ref: this.selected.ref, message }).then(payload => {
        if (payload) {
          this.commentText = "";
          this.notify("Comment added");
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
      return null;
    },

    async uploadAttachmentsForTarget(target, files, textarea) {
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
          scope: target === "composer" ? "workspace" : "",
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

    archiveSelected() {
      if (!this.selected || this.busy) return;
      this.apiPost("/api/archive", { ref: this.selected.ref, value: !this.selected.archived });
    },

    hideSelected() {
      if (!this.selected || this.busy) return;
      this.apiPost("/api/hidden", { ref: this.selected.ref, value: !this.selected.hidden });
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
