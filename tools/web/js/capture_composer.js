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
    _editOriginal: null,
    ...zenWorkspaceMethods({
      getText: editor => (editor.draft ? editor.draft.text : ""),
      setText: (editor, ctx, nextText) => {
        if (!editor.draft) return;
        editor.draft.text = nextText;
        editor.persist(ctx);
        editor.schedulePreview(ctx);
      },
      isDisabled: editor => editor.saving,
      getPlaceholder: editor => (!editor.draft ? "" : (editor.draft.mode === "edit" ? "" :
        (editor.draft.kind === "task" ? "What do you want to accomplish?" : "Write a thought, observation, or meeting note…"))),
      getPreviewCard: () => ({}),
      canEnterZen: editor => Boolean(editor.open && editor.draft),
      mentionIdPrefix: "mt-composer-mention-",
      onSave: (editor, ctx) => editor.save(ctx),
      onEscape: (editor, ctx) => editor.handleEscape(ctx),
    }),

    handleEscape(ctx) {
      if (this.mention && this.mention.open) {
        this.mention.close();
        return;
      }
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
      return formatZenMetaSummary(this.draft.taskOptions, this.draft.instructions);
    },

    isCardEdit() {
      return Boolean(this.draft && this.draft.mode === "edit" && this.draft.origin === "card-body");
    },

    editPropertyPatch() {
      if (!this.isCardEdit() || !this._editOriginal) return {};
      const draft = this.draft;
      const orig = this._editOriginal;
      const opts = draft.taskOptions || {};
      const patch = {};
      const fields = orig.kind === "task" ? ["priority", "type", "project", "topic"] : ["priority", "project", "topic"];
      for (const field of fields) {
        const value = (opts[field] || "") || null;
        const old = (orig[field] || "") || null;
        if (value !== old) patch[field] = value;
      }
      const tags = Array.isArray(opts.tags) ? opts.tags : [];
      if (JSON.stringify(tags) !== JSON.stringify(orig.tags || [])) patch.tags = tags;
      if ((draft.title || "") !== (orig.title || "")) patch.title = draft.title;
      if (orig.kind === "task" && (draft.next_action || "") !== (orig.next_action || "")) {
        patch.next_action = draft.next_action || null;
      }
      if ((draft.instructions || "") !== (orig.processing_instructions || "")) {
        patch.processing_instructions = draft.instructions || null;
      }
      if ((opts.due || null) !== (orig.due || null) || (opts.due_kind || null) !== (orig.due_kind || null)) {
        patch.due = opts.due || null;
        patch.due_kind = opts.due ? opts.due_kind : null;
      }
      return patch;
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
        id: captureId, captureId, capture_id: captureId, draftId: captureId, revision: 0,
        createdAt: this.localIsoSeconds(now), created: this.localIsoSeconds(now),
        title: "", text: "", instructions: "", taskOptions: this.newTaskOptions(ctx), ref: "",
        originalText: "",
        _savedBody: null, _savedTitle: null, _savedKind: kind === "task" ? "task" : "note",
        _savedMetadata: "",
      };
      this.draft._savedMetadata = ctx.metadataSnapshot(this.draft);
      this._editOriginal = null;
      this.persisted = false;
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
      if (this.surface) this.surface.setValue("");
      this.message = "";
      this.messageError = false;
      if (ctx.clearStatus) ctx.clearStatus();
      this.openComposer(ctx, event);
    },

    beginEdit(ctx, card, opts = {}) {
      if (!card || this.saving || typeof card.body !== "string") return;
      if (!ctx.workspaceKey) {
        this.recoveryWarning = "This workspace has no recovery key, so editing is unavailable.";
        return;
      }
      if (!this.preserveBeforeReplace(ctx)) return;
      const existing = opts.saved || (ctx.getDrafts
        ? ctx.getDrafts().find(d => d.mode === "edit" && d.origin === "card-body" && d.target === card.path && !d._orphanedLocal)
        : null);
      const source = opts.currentText !== undefined
        ? String(opts.currentText)
        : String(existing ? existing.body || "" : card.body);
      const draftId = existing ? existing.id : this.makeId();
      const taskOptions = normalizeMetadataValue(card, { task_defaults: ctx.taskDefaults });
      if (existing) {
        Object.assign(existing, {
          version: 1,
          workspaceKey: ctx.workspaceKey,
          kind: card.kind,
          mode: "edit",
          origin: "card-body",
          target: card.path,
          ref: card.ref,
          base_hash: existing.base_hash || card.content_hash,
          base_updated: existing.base_updated || card.updated,
          id: draftId,
          draftId,
          title: card.title || "",
          text: source,
          body: source,
          originalText: card.body,
          next_action: card.next_action || "",
          instructions: card.processing_instructions || "",
          taskOptions,
        });
        this.draft = existing;
      } else {
        this.draft = {
          version: 1,
          workspaceKey: ctx.workspaceKey,
          kind: card.kind,
          mode: "edit",
          origin: "card-body",
          target: card.path,
          ref: card.ref,
          base_hash: card.content_hash,
          base_updated: card.updated,
          id: draftId,
          draftId,
          revision: 0,
          title: card.title || "",
          text: source,
          body: source,
          originalText: card.body,
          next_action: card.next_action || "",
          instructions: card.processing_instructions || "",
          taskOptions,
          _savedBody: source,
          _savedTitle: card.title || "",
          _savedKind: card.kind,
          _savedMetadata: "",
        };
      }
      this._editOriginal = {
        ref: card.ref,
        kind: card.kind,
        content_hash: card.content_hash,
        title: card.title || "",
        next_action: card.next_action || "",
        processing_instructions: card.processing_instructions || "",
        ...taskOptions,
      };
      this.persisted = Boolean(existing);
      this.zen = opts.zen !== undefined ? Boolean(opts.zen) : true;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
      if (this.surface) this.surface.setValue(source);
      this.message = "";
      this.messageError = false;
      if (ctx.clearStatus) ctx.clearStatus();
      this.openComposer(ctx);
    },

    persist(ctx) {
      const draft = this.draft;
      if (!draft) return false;
      if (this.isCardEdit()) {
        draft.body = draft.text;
        if (draft.body === draft.originalText && !Number(draft.revision || 0)) {
          this.persisted = false;
          return false;
        }
        try {
          const localSaved = Boolean(ctx.store.saveLocal(draft));
          if (!localSaved) throw new Error("Browser storage unavailable");
          this.persisted = true;
          this.recoveryWarning = "";
          if (typeof ctx.store.upsert === "function") {
            ctx.store.upsert(draft);
          } else {
            const drafts = ctx.getDrafts();
            const existing = drafts.findIndex(item => item.id === draft.id);
            if (existing >= 0) Object.assign(drafts[existing], draft);
            else drafts.push(draft);
          }
          if (!draft._orphanedLocal) ctx.scheduleDraftSave(draft);
          return true;
        } catch (_) {
          this.persisted = false;
          this.recoveryWarning = "Browser storage is unavailable. This draft cannot be recovered if the page closes.";
          return false;
        }
      }
      draft.body = String(draft.text || "");
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
        const id = draft.id || draft.draftId || draft.captureId;
        const taskOptions = draft.taskOptions || this.newTaskOptions(ctx);
        Object.assign(draft, {
          id,
          draftId: id,
          kind: draft.kind || "note",
          title: String(draft.title || ""),
          body: String(draft.text || ""),
          instructions: String(draft.instructions || ""),
          taskOptions,
          target: draft.target || "",
          ref: draft.ref || "",
          base_hash: draft.base_hash || "",
          base_updated: draft.base_updated || "",
          content_hash: draft.content_hash || "",
          capture_id: draft.capture_id || draft.captureId || "",
          created: draft.created || draft.createdAt || "",
          revision: Number(draft.revision || 0),
          updated: draft.updated || "",
          _serverSaved: Boolean(draft._serverSaved || Number(draft.revision || 0) > 0),
          _orphanedLocal: Boolean(draft._orphanedLocal),
          _savedKind: draft._savedKind || draft.kind || "note",
        });
        if (typeof ctx.store.upsert === "function") {
          ctx.store.upsert(draft);
        } else {
          const drafts = ctx.getDrafts();
          const existing = drafts.findIndex(item => item.id === id);
          if (existing >= 0) Object.assign(drafts[existing], draft);
          else drafts.push(draft);
        }
        if (draft.draftId && !draft._orphanedLocal) ctx.scheduleDraftSave(draft);
        return true;
      } catch (_) {
        this.persisted = false;
        this.recoveryWarning = "Browser storage is unavailable. This draft cannot be recovered if the page closes.";
        return false;
      }
    },

    // Discard is offered only when the draft holds something; an empty draft just closes.
    hasContent(ctx) {
      if (!this.draft) return false;
      if (this.isCardEdit()) {
        return Boolean(Number(this.draft.revision || 0) > 0 || this.draft._serverSaved || this.draft.text !== this.draft.originalText);
      }
      return Boolean(!ctx.store.isEmpty(this.draft));
    },

    hasUnpersisted(ctx) {
      if (!this.draft || this.persisted) return false;
      if (this.draft.mode === "new") return !ctx.store.isEmpty(this.draft);
      return this.draft.text !== this.draft.originalText;
    },

    close(ctx) {
      if (!this.open) return;
      if (this.isCardEdit()) {
        this.persist(ctx);
        this.draft = null;
        this._editOriginal = null;
        this.persisted = false;
      } else if (this.draft && ctx.store.isEmpty(this.draft)) {
        this.persist(ctx);
        this.draft = null;
        this.persisted = false;
      } else if (this.draft && this.draft.mode === "new") {
        this.persist(ctx);
      }
      this.destroyZenWorkspace();
      this.open = false;
      const returnFocus = this.returnFocus;
      ctx.nextTick(() => {
        if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === "function") returnFocus.focus();
      });
    },

    async discard(ctx) {
      const draft = this.draft;
      if (!draft || this.saving) return;
      if (this.isCardEdit() && !this.hasContent(ctx)) {
        this.close(ctx);
        this.message = "";
        this.messageError = false;
        return;
      }
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
      // Discard the live object so a save that finishes during the flush
      // updates the revision this request sends.
      draft.kind = draft.kind || "note";
      draft.body = draft.text;
      await ctx.discardWorkspaceDraft(draft, true);
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
      if (this.isCardEdit()) {
        const bodyDirty = draft.text !== draft.originalText;
        const propsDirty = Object.keys(this.editPropertyPatch()).length > 0;
        if (!bodyDirty && !propsDirty) return false;
        if (!String(draft.title || "").trim() || /[\r\n\0]/.test(draft.title)) return false;
        const options = draft.taskOptions || {};
        if (options.due && !options.due_kind) return false;
        const priorities = ctx.taskPriorityPolicy || {};
        if (options.priority && Object.keys(priorities).length &&
            !Object.prototype.hasOwnProperty.call(priorities, options.priority)) return false;
        if (draft.kind === "task" && options.type && Object.keys(ctx.taskTypePolicy || {}).length &&
            !Object.prototype.hasOwnProperty.call(ctx.taskTypePolicy, options.type)) return false;
        return true;
      }
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
      if (this.isCardEdit()) {
        const bodyDirty = draft.text !== draft.originalText;
        const patch = this.editPropertyPatch();
        const propsDirty = Object.keys(patch).length > 0;
        if (bodyDirty) this.persist(ctx);
        this.saving = true;
        this.message = "";
        this.messageError = false;
        try {
          let result = null;
          if (bodyDirty) {
            draft.body = draft.text;
            result = await ctx.finishWorkspaceDraft(draft);
            if (!result) return null;
            const latest = ctx.getSelected ? ctx.getSelected() : null;
            const nextId = this.makeId();
            Object.assign(draft, {
              id: nextId,
              draftId: nextId,
              revision: 0,
              content_hash: "",
              _serverSaved: false,
              base_hash: (latest && latest.content_hash) || draft.base_hash,
              base_updated: (latest && latest.updated) || draft.base_updated,
              originalText: draft.text,
              _savedBody: draft.text,
            });
            if (this._editOriginal && latest && latest.content_hash) {
              this._editOriginal.content_hash = latest.content_hash;
            }
          }
          if (propsDirty && ctx.saveCardProperties && this._editOriginal) {
            const latest = ctx.getSelected ? ctx.getSelected() : null;
            const expectedHash = (latest && latest.ref === this._editOriginal.ref && latest.content_hash) || this._editOriginal.content_hash;
            const propResult = await ctx.saveCardProperties(this._editOriginal.ref, expectedHash, patch);
            if (!propResult) {
              this.message = (ctx.actionMessage && ctx.actionMessage()) || "Could not save card properties.";
              this.messageError = true;
              return null;
            }
            result = propResult;
          }
          this.draft = null;
          this._editOriginal = null;
          this.persisted = false;
          this.close(ctx);
          this.message = "Saved";
          this.messageError = false;
          return result;
        } finally {
          this.saving = false;
        }
      }
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
