// Capture dialog state and workflows. The board supplies the shared draft
// store and narrow workspace/API adapters when an action runs.
function createCaptureComposer() {
  return {
    draft: null,
    open: false,
    persisted: false,
    recoveryWarning: "",
    message: "",
    messageError: false,
    saving: false,
    returnFocus: null,
    zen: false,
    zenView: "write",
    zenMetaOpen: false,
    previewHtml: "",
    _previewTimer: null,
    surface: null,

    ensureSurface(ctx, hostEl) {
      if (typeof createEditorSurface !== "function") return null;
      if (!this.surface) {
        this.surface = createEditorSurface({
          getValue: () => (this.draft ? this.draft.text : ""),
          onInput: text => {
            if (!this.draft) return;
            this.draft.text = text;
            this.persist(ctx);
            this.schedulePreview();
          },
          onSave: () => this.save(ctx),
          onEscape: () => this.handleEscape(ctx),
          getPlaceholder: () => (!this.draft ? "" : (this.draft.mode === "edit" ? "" :
            (this.draft.kind === "task" ? "What do you want to accomplish?" : "Write a thought, observation, or meeting note…"))),
          isDisabled: () => this.saving,
        });
      }
      if (hostEl && this.zen && this.surface.hasCustomEngine() && !this.surface.instance) {
        this.surface.mount(hostEl);
      }
      return this.surface;
    },

    syncSurface(ctx, hostEl) {
      const surface = this.ensureSurface(ctx, hostEl);
      if (!surface) return;
      if (this.zen && surface.hasCustomEngine()) {
        if (hostEl && (!surface.instance || surface.hostEl !== hostEl)) {
          surface.mount(hostEl);
        }
        surface.setDisabled(this.saving);
      } else if (surface.instance) {
        surface.destroy();
      }
    },

    hasCustomEngine() {
      return Boolean(this.surface && this.surface.hasCustomEngine());
    },

    isCustomMounted() {
      return Boolean(this.surface && this.surface.instance);
    },

    refreshPreview() {
      if (typeof renderEditorPreview === "function") {
        this.previewHtml = renderEditorPreview({}, this.draft ? this.draft.text : "");
      } else if (typeof renderMarkdown === "function" && typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
        this.previewHtml = renderMarkdown({}, this.draft ? (this.draft.text || "") : "");
      } else {
        this.previewHtml = "";
      }
    },

    schedulePreview() {
      if (!this.zen || (this.zenView !== "split" && this.zenView !== "preview")) return;
      if (this._previewTimer) clearTimeout(this._previewTimer);
      this._previewTimer = setTimeout(() => {
        this._previewTimer = null;
        this.refreshPreview();
      }, 150);
    },

    toggleZen(ctx, force) {
      if (!this.open || !this.draft) return;
      const next = typeof force === "boolean" ? force : !this.zen;
      this.zen = next;
      this.zenMetaOpen = false;
      if (next && (this.zenView === "split" || this.zenView === "preview")) {
        this.refreshPreview();
      }
      if (ctx && typeof ctx.nextTick === "function") {
        ctx.nextTick(() => {
          if (!next || this.zenView !== "preview") {
            if (!(this.surface && this.surface.focus())) {
              if (typeof ctx.focusInput === "function") ctx.focusInput();
            }
          }
        });
      }
    },

    setZenView(ctx, view) {
      if (view !== "write" && view !== "split" && view !== "preview") return;
      this.zenView = view;
      if (view === "split" || view === "preview") {
        if (this._previewTimer) {
          clearTimeout(this._previewTimer);
          this._previewTimer = null;
        }
        this.refreshPreview();
      }
      if (view !== "preview" && ctx && typeof ctx.nextTick === "function") {
        ctx.nextTick(() => {
          if (!(this.surface && this.surface.focus())) {
            if (typeof ctx.focusInput === "function") ctx.focusInput();
          }
        });
      }
    },

    handleEscape(ctx) {
      if (this.zenMetaOpen) {
        this.zenMetaOpen = false;
        return;
      }
      if (this.zen) {
        this.toggleZen(ctx, false);
        return;
      }
      this.close(ctx);
    },

    zenMetaSummary() {
      if (!this.draft) return "";
      const parts = [];
      const opts = this.draft.taskOptions || {};
      if (opts.project) parts.push(opts.project);
      if (opts.topic) parts.push(opts.topic);
      if (Array.isArray(opts.tags) && opts.tags.length) {
        parts.push(opts.tags.length === 1 ? "1 tag" : opts.tags.length + " tags");
      }
      if (opts.due) parts.push("due " + opts.due);
      if (this.draft.instructions && this.draft.instructions.trim()) parts.push("instructions");
      return parts.join(" · ");
    },

    makeId() {
      if (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function") {
        return globalThis.crypto.randomUUID();
      }
      return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, character => {
        const random = Math.floor(Math.random() * 16);
        return (character === "x" ? random : (random & 3) | 8).toString(16);
      });
    },

    localIsoSeconds(date) {
      const pad = value => String(value).padStart(2, "0");
      return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()) +
        "T" + pad(date.getHours()) + ":" + pad(date.getMinutes()) + ":" + pad(date.getSeconds());
    },

    dateLabel() {
      if (!this.draft || !this.draft.createdAt) return "just now";
      const date = new Date(this.draft.createdAt);
      if (Number.isNaN(date.getTime())) return this.draft.createdAt;
      return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
    },

    newTaskOptions(ctx) {
      // A board filtered to one project or topic files new cards there too.
      const scope = ctx.scopeDefaults || {};
      return normalizeMetadataValue(
        { project: scope.project || "", topic: scope.topic || "" },
        { task_statuses: ctx.taskStatuses, task_defaults: ctx.taskDefaults },
      );
    },

    setKind(ctx, kind) {
      if (!this.draft || this.draft.mode !== "new" || this.saving) return;
      this.draft.kind = kind === "task" ? "task" : "note";
      if (!this.draft.taskOptions) this.draft.taskOptions = this.newTaskOptions(ctx);
      this.persist(ctx);
    },

    title() {
      if (this.draft && this.draft.mode === "edit") return this.draft.kind === "task" ? "Edit task" : "Edit note";
      return this.draft && this.draft.kind === "task" ? "New task" : "New note";
    },

    saveLabel() {
      if (this.saving) return "Saving…";
      if (this.draft && this.draft.mode === "edit") return "Save changes";
      return this.draft && this.draft.kind === "task" ? "Create task" : "Create note";
    },

    openComposer(ctx, event) {
      if (event && event.currentTarget) this.returnFocus = event.currentTarget;
      this.open = true;
      this.message = "";
      this.messageError = false;
      ctx.focusInput();
    },

    resume(ctx, event) {
      if (!this.draft || this.saving) return;
      if (this.draft.workspaceKey !== ctx.workspaceKey) {
        this.message = "This local draft belongs to another workspace. Return to that workspace to resume it.";
        this.messageError = true;
        return;
      }
      this.openComposer(ctx, event);
    },

    preserveBeforeReplace(ctx) {
      if (!this.hasUnpersisted(ctx) || this.persist(ctx)) return true;
      this.openComposer(ctx);
      this.message = "This draft could not be saved. Keep it here until storage is available, or explicitly discard it.";
      this.messageError = true;
      return false;
    },

    beginNew(ctx, event, kind = "note") {
      if (this.saving) return;
      if (!ctx.workspaceKey) {
        this.recoveryWarning = "This workspace has no recovery key, so note capture is unavailable.";
        return;
      }
      if (!this.preserveBeforeReplace(ctx)) return;
      const now = new Date();
      const captureId = this.makeId();
      this.draft = {
        version: 1, workspaceKey: ctx.workspaceKey,
        kind: kind === "task" ? "task" : "note", mode: "new",
        captureId, capture_id: captureId, draftId: captureId, revision: 0,
        createdAt: this.localIsoSeconds(now), created: this.localIsoSeconds(now),
        title: "", text: "", instructions: "", taskOptions: this.newTaskOptions(ctx), ref: "",
        originalText: "",
        _savedBody: null, _savedTitle: null, _savedKind: kind === "task" ? "task" : "note",
        _savedMetadata: "",
      };
      this.draft._savedMetadata = ctx.metadataSnapshot(this.draft);
      this.persisted = false;
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
      if (this.surface) this.surface.setValue("");
      this.message = "";
      this.messageError = false;
      this.openComposer(ctx, event);
    },

    persist(ctx) {
      const draft = this.draft;
      if (!draft) return false;
      if (ctx.store.isEmpty(draft)) {
        ctx.store.forgetEmpty(draft);
        this.persisted = false;
        return false;
      }
      if (!draft.workspaceKey) {
        this.persisted = false;
        this.recoveryWarning = "This draft has no workspace key and cannot be saved for recovery.";
        return false;
      }
      try {
        if (!ctx.store.saveLocal(draft)) throw new Error("Browser storage unavailable");
        this.persisted = true;
        this.recoveryWarning = "";
        const id = draft.draftId || draft.captureId;
        const taskOptions = draft.taskOptions || this.newTaskOptions(ctx);
        const localRecord = {
          id, kind: draft.kind || "note", mode: draft.mode,
          title: String(draft.title || ""), body: String(draft.text || ""),
          instructions: String(draft.instructions || ""), task_options: taskOptions,
          ...ctx.captureDraftFields(draft), target: draft.target || "", ref: draft.ref || "",
          base_hash: draft.base_hash || "", base_updated: draft.base_updated || "",
          content_hash: draft.content_hash || "", capture_id: draft.capture_id || draft.captureId || "",
          created: draft.created || draft.createdAt || "", revision: Number(draft.revision || 0),
          updated: draft.updated || "", _serverSaved: Boolean(draft._serverSaved || Number(draft.revision || 0) > 0),
          _orphanedLocal: Boolean(draft._orphanedLocal), _savedBody: draft._savedBody,
          _savedTitle: draft._savedTitle, _savedKind: draft._savedKind || draft.kind || "note",
          _savedMetadata: draft._savedMetadata, _localRecovery: true,
        };
        const drafts = ctx.getDrafts();
        const existing = drafts.findIndex(item => item.id === id);
        if (existing >= 0) drafts.splice(existing, 1, { ...drafts[existing], ...localRecord });
        else drafts.push(localRecord);
        if (draft.draftId && !draft._orphanedLocal) ctx.scheduleDraftSave(draft);
        return true;
      } catch (_) {
        this.persisted = false;
        this.recoveryWarning = "Browser storage is unavailable. This draft cannot be recovered if the page closes.";
        return false;
      }
    },

    hasUnpersisted(ctx) {
      if (!this.draft || this.persisted) return false;
      if (this.draft.mode === "new") return !ctx.store.isEmpty(this.draft);
      return this.draft.text !== this.draft.originalText;
    },

    close(ctx) {
      if (!this.open) return;
      if (ctx.store.isEmpty(this.draft)) {
        this.persist(ctx);
        this.draft = null;
        this.persisted = false;
      } else if (this.draft && this.draft.mode === "new") {
        this.persist(ctx);
      }
      if (this._previewTimer) {
        clearTimeout(this._previewTimer);
        this._previewTimer = null;
      }
      if (this.surface) this.surface.destroy();
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
      this.open = false;
      const returnFocus = this.returnFocus;
      ctx.nextTick(() => {
        if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === "function") returnFocus.focus();
      });
    },

    async discard(ctx) {
      const draft = this.draft;
      if (!draft || this.saving) return;
      if (draft.mode === "new" && !String(draft.text || "").trim() && !String(draft.title || "").trim() &&
          !String(draft.instructions || "").trim() &&
          Number(draft.revision || 0) === 0) {
        this.close(ctx);
        this.message = "";
        this.messageError = false;
        return;
      }
      if (ctx.confirm && !ctx.confirm("Discard this draft? This removes it from the workspace.")) return;
      if (draft._orphanedLocal) {
        const sourceId = draft.sourceId || draft.draftId;
        ctx.dismissLocalWorkspaceDraft({ ...draft, id: sourceId, sourceId, storageId: sourceId, _orphanedLocal: true });
        return;
      }
      if (!draft.draftId) return;
      const workspaceDraft = { ...draft, id: draft.draftId, kind: draft.kind || "note", body: draft.text };
      await ctx.discardWorkspaceDraft(workspaceDraft, true);
    },

    meetingTemplateText() {
      return "### Discussion\n\n### Decisions\n\n### Actions";
    },

    applyMeetingTemplate(ctx) {
      if (!this.draft || this.draft.mode !== "new" || this.saving) return;
      const headings = this.meetingTemplateText();
      if (this.draft.text.includes(headings)) {
        this.message = "The meeting template is already in this note.";
        this.messageError = false;
        return;
      }
      if (!this.draft.text) this.draft.text = headings;
      else {
        const separator = this.draft.text.endsWith("\n\n") ? "" : (this.draft.text.endsWith("\n") ? "\n" : "\n\n");
        this.draft.text += separator + headings;
      }
      this.persist(ctx);
      if (this.surface) this.surface.setValue(this.draft.text);
      this.schedulePreview();
      this.message = "Meeting headings added. Existing text was kept.";
      this.messageError = false;
      ctx.focusInput();
    },

    canSave(ctx) {
      const draft = this.draft;
      if (!draft || draft._orphanedLocal || draft.saved ||
          draft.workspaceKey !== ctx.workspaceKey || ctx.busy) return false;
      if (draft.mode === "edit") return Boolean(draft.text.trim());
      // Shared metadata applies to both kinds; status and type only to tasks.
      const options = draft.taskOptions || {};
      if (options.due && !options.due_kind) return false;
      // Policy arrives with the first payload; the server validates either way.
      const priorities = ctx.taskPriorityPolicy || {};
      if (options.priority && Object.keys(priorities).length &&
          !Object.prototype.hasOwnProperty.call(priorities, options.priority)) return false;
      if (draft.kind === "task") {
        if (!String(draft.text || "").trim() && !String(draft.title || "").trim()) return false;
        if (options.status && !ctx.taskStatuses.includes(options.status)) return false;
        if (options.type && !Object.prototype.hasOwnProperty.call(ctx.taskTypePolicy, options.type)) return false;
        return true;
      }
      return Boolean(draft.text.trim() && !this.isMeetingTemplateOnly());
    },

    isMeetingTemplateOnly() {
      if (!this.draft || this.draft.mode !== "new") return false;
      const nonemptyLines = text => text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      return JSON.stringify(nonemptyLines(this.draft.text)) === JSON.stringify(nonemptyLines(this.meetingTemplateText()));
    },

    async save(ctx) {
      const draft = this.draft;
      if (draft && draft._orphanedLocal) return null;
      if (!this.canSave(ctx) || this.saving || ctx.busy) return null;
      this.persist(ctx);
      this.saving = true;
      this.message = "";
      this.messageError = false;
      try {
        if (!draft.draftId) {
          this.message = "This draft has no current workspace identity. Export its text and create a new draft.";
          this.messageError = true;
          return null;
        }
        return await ctx.finishWorkspaceDraft(draft);
      } finally {
        this.saving = false;
      }
    },
  };
}
