// Draft store wiring, editor context adapters, and draft recovery orchestration.
function createDraftCoordinator() {
  return {

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
          const input = (this.$refs && this.$refs.composerBody) || document.getElementById("mt-composer-body");
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
        const input = (this.$refs && this.$refs.composerBody) || document.getElementById("mt-composer-body");
        if (input) input.focus();
      });
      if (this.draftChangedFromSaved(this.captureComposer.draft)) {
        this.scheduleDraftSave(this.captureComposer.draft, 0);
      }
    },

    taskOptionsFromDraft(draft) {
      const nested = draft && (draft.taskOptions || draft.task_options);
      const saved = nested && typeof nested === "object" ? nested : {};
      const source = {
        status: draft && draft.status || saved.status,
        priority: draft && draft.priority || saved.priority,
        type: draft && draft.type || saved.type,
        project: draft && draft.project || saved.project,
        topic: draft && draft.topic || saved.topic,
        tags: Array.isArray(draft && draft.tags) ? draft.tags : saved.tags,
        due: draft && Object.prototype.hasOwnProperty.call(draft, "due") ? draft.due : saved.due,
        due_kind: draft && Object.prototype.hasOwnProperty.call(draft, "due_kind") ? draft.due_kind : saved.due_kind,
      };
      return normalizeMetadataValue(source, {
        task_statuses: this.taskStatuses,
        task_defaults: this.taskDefaults,
      });
    },

    draftMetadataSnapshot(draft) {
      return this.getDraftStore().metadataSnapshot(draft);
    },

    draftChangedFromSaved(draft) {
      return this.getDraftStore().hasChanges(draft);
    },

    captureDraftFields(draft) {
      if (!draft || draft.mode !== "new") return {};
      return {
        ...metadataCoreObject(this.taskOptionsFromDraft(draft)),
        instructions: String(draft.instructions || ""),
      };
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
      if (this.contentEditor.draft === draft) {
        this.contentEditor.draft = null;
        this.contentEditor.text = "";
        this.contentEditor.error = "";
        this.$nextTick(() => ((this.$refs && this.$refs.contentEditButton) || this.$refs.detailDialog?.querySelector('.mt-content-edit'))?.focus());
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
          this.$nextTick(() => ((this.$refs && this.$refs.contentEditButton) || this.$refs.detailDialog?.querySelector('.mt-content-edit'))?.focus());
        } else if (draft === this.captureComposer.draft || (this.captureComposer.draft && this.captureComposer.draft.draftId === draft.id)) {
          const wasEdit = draft.mode === "edit";
          this.captureComposer.draft = null;
          this.captureComposer.persisted = false;
          this.captureComposer.close(this.captureContext());
          if (wasEdit) this.mode = draft.kind === "task" ? "tasks" : "notes";
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
        this.draftWorkspaceStatus = "Draft discarded";
      } catch (error) {
        this.draftWorkspaceStatus = "Error";
        this.captureComposer.message = error.message || "Could not discard draft";
        this.captureComposer.messageError = true;
      } finally {
        this.draftBusy = false;
      }
    },

  };
}
