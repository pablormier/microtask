// Capture dialog state and workflows. The board supplies the shared draft
// store and narrow workspace/API adapters when an action runs.
function createCaptureComposer() {
  return {
    draft: null,
    open: false,
    recoveryWarning: "",
    message: "",
    messageError: false,
    saving: false,
    returnFocus: null,
    ...zenWorkspaceMethods({
      getText: editor => (editor.draft ? editor.draft.body : ""),
      setText: (editor, ctx, nextText) => {
        if (!editor.draft) return;
        editor.draft.body = nextText;
        editor.persist(ctx);
        editor.schedulePreview(ctx);
      },
      isDisabled: editor => editor.saving,
      getPlaceholder: editor => (!editor.draft ? "" :
        (editor.draft.kind === "task" ? "What do you want to accomplish?" : "Write a thought, observation, or meeting note…")),
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

    newTaskOptions(ctx) {
      // A board filtered to one project or topic files new cards there too.
      const scope = ctx.scopeDefaults || {};
      return normalizeMetadataValue(
        { project: scope.project || "", topic: scope.topic || "" },
        { task_statuses: ctx.taskStatuses, task_defaults: ctx.taskDefaults },
      );
    },

    setKind(ctx, kind) {
      if (!this.draft || this.saving) return;
      this.draft.kind = kind === "task" ? "task" : "note";
      if (!this.draft.taskOptions) this.draft.taskOptions = this.newTaskOptions(ctx);
      this.persist(ctx);
    },

    title() {
      return this.draft && this.draft.kind === "task" ? "New task" : "New note";
    },

    saveLabel() {
      if (this.saving) return "Saving…";
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
      if (this.draft.workspaceKey && this.draft.workspaceKey !== ctx.workspaceKey) {
        this.message = "This draft belongs to another workspace. Return to that workspace to resume it.";
        this.messageError = true;
        return;
      }
      this.openComposer(ctx, event);
    },

    beginNew(ctx, event, kind = "note") {
      if (this.saving) return;
      // Save what is open before replacing it; a draft in conflict stays in the Drafts list.
      this.persist(ctx);
      const now = new Date();
      const captureId = this.makeId();
      const created = this.localIsoSeconds(now);
      this.draft = {
        workspaceKey: ctx.workspaceKey,
        kind: kind === "task" ? "task" : "note",
        state: "clean",
        id: captureId,
        capture_id: captureId,
        created,
        title: "",
        body: "",
        instructions: "",
        taskOptions: this.newTaskOptions(ctx),
        _saved: null,
      };
      this.recoveryWarning = "";
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
      this.message = "";
      this.messageError = false;
      if (ctx.clearStatus) ctx.clearStatus();
      this.openComposer(ctx, event);
    },

    persist(ctx) {
      const draft = this.draft;
      if (!draft) return false;
      if (ctx.store.isEmpty(draft)) {
        ctx.store.forgetEmpty(draft);
        return false;
      }
      if (draft.state === "conflict" || draft.state === "closed") return false;
      if (!draft.taskOptions) draft.taskOptions = this.newTaskOptions(ctx);
      ctx.store.upsert(draft);
      ctx.scheduleDraftSave(draft);
      return true;
    },

    hasContent(ctx) {
      if (!this.draft) return false;
      return Boolean(!ctx.store.isEmpty(this.draft));
    },

    hasUnpersisted(ctx) {
      if (!this.draft) return false;
      return this.draft.state === "dirty" || this.draft.state === "saving" || this.draft.state === "conflict";
    },

    close(ctx) {
      if (!this.open) return true;
      if (this.draft && ctx.store.isEmpty(this.draft)) {
        ctx.store.forgetEmpty(this.draft);
        this.draft = null;
      } else if (this.draft) {
        this.persist(ctx);
      }
      this.destroyZenWorkspace();
      this.open = false;
      const returnFocus = this.returnFocus;
      ctx.nextTick(() => {
        if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === "function") returnFocus.focus();
      });
      return true;
    },

    async discard(ctx) {
      const draft = this.draft;
      if (!draft || this.saving) return;
      if (!String(draft.body || "").trim() && !String(draft.title || "").trim() &&
          !String(draft.instructions || "").trim() &&
          !draft.content_hash) {
        this.close(ctx);
        this.message = "";
        this.messageError = false;
        return;
      }
      if (ctx.confirm && !ctx.confirm("Discard this draft? This removes it from the workspace.")) return;
      if (!draft.id) return;
      await ctx.discardWorkspaceDraft(draft, true);
    },

    meetingTemplateText() {
      return "### Discussion\n\n### Decisions\n\n### Actions";
    },

    applyMeetingTemplate(ctx) {
      if (!this.draft || this.saving) return;
      const headings = this.meetingTemplateText();
      if (this.draft.body.includes(headings)) {
        this.message = "The meeting template is already in this note.";
        this.messageError = false;
        return;
      }
      if (!this.draft.body) this.draft.body = headings;
      else {
        const separator = this.draft.body.endsWith("\n\n") ? "" : (this.draft.body.endsWith("\n") ? "\n" : "\n\n");
        this.draft.body += separator + headings;
      }
      this.persist(ctx);
      this.schedulePreview();
      this.message = "Meeting headings added. Existing text was kept.";
      this.messageError = false;
      ctx.focusInput();
    },

    canSave(ctx) {
      const draft = this.draft;
      if (!draft || (draft.workspaceKey && draft.workspaceKey !== ctx.workspaceKey) || ctx.busy || ctx.attachmentUploading) return false;
      if (draft.state === "conflict" || draft.state === "closed") return false;
      const options = draft.taskOptions || {};
      if (options.due && !options.due_kind) return false;
      const priorities = ctx.taskPriorityPolicy || {};
      if (options.priority && Object.keys(priorities).length &&
          !Object.prototype.hasOwnProperty.call(priorities, options.priority)) return false;
      if (draft.kind === "task") {
        if (!String(draft.body || "").trim() && !String(draft.title || "").trim()) return false;
        if (options.status && !ctx.taskStatuses.includes(options.status)) return false;
        if (options.type && !Object.prototype.hasOwnProperty.call(ctx.taskTypePolicy, options.type)) return false;
        return true;
      }
      return Boolean(draft.body.trim() && !this.isMeetingTemplateOnly());
    },

    isMeetingTemplateOnly() {
      if (!this.draft) return false;
      const nonemptyLines = text => text.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
      return JSON.stringify(nonemptyLines(this.draft.body)) === JSON.stringify(nonemptyLines(this.meetingTemplateText()));
    },

    async save(ctx) {
      const draft = this.draft;
      if (!this.canSave(ctx) || this.saving || ctx.busy) return null;
      this.persist(ctx);
      this.saving = true;
      this.message = "";
      this.messageError = false;
      try {
        if (!draft.id) {
          this.message = "This draft has no current workspace identity. Copy its text and create a new draft.";
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
