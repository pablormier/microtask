// Draft store wiring and editor context adapters.
function createDraftCoordinator() {
  return {

    getDraftStore() {
      if (!this.draftStorage) {
        this.draftStorage = createDraftStore({
          initialDrafts: [],
          context: () => ({ workspaceKey: this.workspaceKey, apiToken: this.apiToken,
            available: true }),
          captureFields: draft => this.captureDraftFields(draft),
          onStatus: status => { this.draftWorkspaceStatus = status; },
          onWarning: message => { this.captureComposer.recoveryWarning = message; },
        });
      }
      return this.draftStorage;
    },

    captureContext() {
      return {
        workspaceKey: this.workspaceKey,
        busy: this.busy,
        attachmentUploading: this.attachmentUploading,
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
        captureDraftFields: draft => this.captureDraftFields(draft),
        getDrafts: () => this.drafts,
        getCards: () => this.tasks,
        scheduleDraftSave: (draft, delay) => this.scheduleDraftSave(draft, delay),
        setMode: mode => { this.mode = mode; },
        openWorkspaceDraft: draft => this.openWorkspaceDraft(draft),
        finishWorkspaceDraft: draft => this.finishWorkspaceDraft(draft),
        saveConflictAsNewDraft: draft => this.saveConflictAsNewDraft(draft),
        clearStatus: () => { this.draftWorkspaceStatus = ""; this.captureComposer.message = ""; this.captureComposer.messageError = false; },
        nextTick: callback => this.$nextTick(callback),
        focusInput: () => this.$nextTick(() => {
          const input = (this.$refs && this.$refs.composerBody) || document.getElementById("mt-composer-body");
          if (input) input.focus();
        }),
        confirm: message => typeof window !== "undefined" && typeof window.confirm === "function"
          ? window.confirm(message) : true,
        discardWorkspaceDraft: (draft, confirmed) => this.discardWorkspaceDraft(draft, confirmed),
      };
    },

    propertyContext() {
      return {
        selected: this.selected,
        busy: this.busy,
        draftBusy: this.draftBusy,
        contentOpen: Boolean(this.contentEditor.open),
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
        workspaceKey: this.workspaceKey,
        selected: this.selected,
        busy: this.busy,
        draftBusy: this.draftBusy,
        attachmentUploading: this.attachmentUploading,
        getSelected: () => this.selected,
        getCards: () => this.tasks,
        saveCardBody: (ref, expectedHash, body) => this.saveCardBody(ref, expectedHash, body),
        // Property saves read a fresh context: the body save has just replaced the selected card.
        hasPropertyChanges: () => this.propertyEditor.changed(),
        canSaveProperties: () => this.propertyEditor.canApply(this.propertyContext(), { allowContentOpen: true }),
        saveProperties: () => this.propertyEditor.apply(this.propertyContext(), { allowContentOpen: true }),
        propertyError: () => this.actionMessage,
        reloadSelected: async () => {
          await this.loadSelectedDetails(true);
          return this.selected;
        },
        confirm: message => typeof window !== "undefined" && typeof window.confirm === "function"
          ? window.confirm(message) : true,
        clearStatus: () => { this.draftWorkspaceStatus = ""; this.captureComposer.message = ""; this.captureComposer.messageError = false; },
        nextTick: callback => this.$nextTick(callback),
        focusInput: () => this.$nextTick(() => this.$refs.detailContentInput?.focus()),
      };
    },

    async saveCardBody(ref, expectedHash, body) {
      this.busy = true;
      this.actionMessage = "";
      this.actionError = false;
      try {
        const query = this.collectionQuery();
        const response = await fetch("/api/card-edit", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Microtask-Token": this.apiToken },
          body: JSON.stringify({ ref, expected_hash: expectedHash, body, query, include: this.includedCardIds() }),
        });
        const payload = await response.json();
        if (!response.ok) {
          return {
            ok: false,
            conflict: Boolean(payload.conflict || response.status === 409),
            error: payload.error || "Could not save card content",
          };
        }
        await this.saveBodyKeepingProperties(async () => {
          await this.applyPayload(payload, false, query);
          return payload;
        });
        if (!Object.prototype.hasOwnProperty.call(payload, "sync_warning")) {
          this.syncWarning = "";
        }
        this.actionMessage = payload.message || "Content saved";
        this.actionError = false;
        return { ok: true, payload };
      } catch (error) {
        return { ok: false, conflict: false, error: (error && error.message) || "Could not save card content" };
      } finally {
        this.busy = false;
      }
    },

    openSelectedInZen() {
      if (!this.selected || this.busy || this.draftBusy || this.detailLoading || this.detailError || typeof this.selected.body !== "string") return;
      if (this.contentEditor.open) this.contentEditor.toggleZen(this.contentContext(), true);
      else this.contentEditor.beginZen(this.contentContext());
    },

    async loadWorkspaceDrafts() {
      try {
        await this.getDraftStore().load();
      } catch (_) {
        this.draftWorkspaceStatus = "Error";
      }
    },

    visibleDrafts() {
      const query = this.draftSearch.trim().toLowerCase();
      return this.drafts.filter(draft => {
        if (!query) return true;
        return [draft.title, draft.kind, draft.body]
          .some(value => String(value || "").toLowerCase().includes(query));
      }).sort((a, b) => String(b.updated || "").localeCompare(String(a.updated || "")));
    },

    draftDisplayTitle(draft) {
      if (!draft) return "Untitled draft";
      if (String(draft.title || "").trim()) return String(draft.title).trim();
      const firstLine = String(draft.body || "").split(/\r?\n/).find(line => line.trim());
      return firstLine ? firstLine.trim() : "Untitled draft";
    },

    cleanExcerpt(text) {
      if (!text) return "";
      // Matches extract_excerpt in tools/web_payloads.py: fence, math and rule lines carry no text,
      // and table rows read as their cells.
      let s = String(text).split("\n")
        .filter(line => !/^\s*(?:(?:`{3,}|~{3,})[^`~]*|\$\$|\\\[|\\\]|([-*_])(?:\s*\1){2,})\s*$/.test(line))
        .filter(line => !/^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-*:?\s*\|?\s*$/.test(line))
        .map(line => line.trimStart().startsWith("|")
          ? line.trim().replace(/^\|+|\|+$/g, "").split("|").map(cell => cell.trim()).filter(Boolean).join(" · ")
          : line)
        .join("\n");
      s = s.replace(/^#{1,6}\s+/gm, "");
      s = s.replace(/^\s*([-*+]|\d+\.)\s+/gm, "");
      s = s.replace(/^\s*>\s*/gm, "");
      s = s.replace(/!?\[([^\]]*)\]\((?:[^\s()]|\([^()]*\))*(?:\s+["'][^"']*["'])?\s*\)/g, "$1");
      s = s.replace(/\[([^\]]+)\]\([^\s)]*$/g, "$1");
      s = s.replace(/\[([^\]]+)$/g, "$1");
      s = s.replace(/`([^`]+)`/g, "$1");
      s = s.replace(/`/g, "");
      s = s.replace(/\$\$([\s\S]+?)\$\$|\\\(([\s\S]+?)\\\)|\\\[([\s\S]+?)\\\]/g, (_, a, b, c) => (a || b || c).trim());
      s = s.replace(/(^|[^\w$\\])\$([^\s$](?:[^$]*[^\s$])?)\$(?![\w$])/g, "$1$2");
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

    async openWorkspaceDraft(draft) {
      if (!draft) return;
      if (this.captureComposer.saving) return;
      if (this.captureComposer.draft !== draft) this.captureComposer.persist(this.captureContext());
      this.prepareDraftForEditing(draft);
      this.captureComposer.draft = draft;
      this.captureComposer.recoveryWarning = draft.state === "conflict"
        ? "This draft changed since you opened it. Copy your text or save as a new draft."
        : "";
      this.draftWorkspaceStatus = draft.state === "conflict" ? "Error" : "Saved in workspace";
      this.captureComposer.zen = false;
      this.captureComposer.zenView = "write";
      this.captureComposer.zenMetaOpen = false;
      this.captureComposer.previewHtml = "";
      if (this.captureComposer.surface) this.captureComposer.surface.setValue(draft.body);
      this.captureComposer.open = true;
      this.$nextTick(() => {
        const input = (this.$refs && this.$refs.composerBody) || document.getElementById("mt-composer-body");
        if (input) input.focus();
      });
    },

    // A draft loaded from the workspace carries metadata as top-level fields; the composer
    // edits it in taskOptions. Fill in what the composer needs, keeping any existing edits.
    prepareDraftForEditing(draft) {
      // A draft straight from the workspace has no edits yet, so its saved snapshot is
      // taken after normalizing; otherwise opening it would look like an unsaved change.
      const fromWorkspace = !draft.taskOptions && draft.state !== "dirty" && draft.state !== "conflict";
      Object.assign(draft, {
        workspaceKey: this.workspaceKey,
        kind: draft.kind || "note",
        state: draft.state || "clean",
        taskOptions: this.taskOptionsFromDraft(draft),
        created: String(draft.created || ""),
        title: String(draft.title || ""),
        body: String(draft.body || ""),
        instructions: String(draft.instructions || ""),
        content_hash: draft.content_hash || "",
      });
      if (fromWorkspace || !Object.prototype.hasOwnProperty.call(draft, "_saved")) {
        draft._saved = this.getDraftStore().snapshot(draft);
      }
      return draft;
    },

    taskOptionsFromDraft(draft) {
      const nested = draft && (draft.taskOptions || draft.task_options);
      // Editor changes live in taskOptions; top-level fields are server values that go stale.
      const hasNested = Boolean(nested) && typeof nested === "object";
      const saved = hasNested ? nested : draft || {};
      const source = {
        status: saved.status,
        priority: saved.priority,
        type: saved.type,
        project: saved.project,
        topic: saved.topic,
        tags: saved.tags,
        due: saved.due,
        due_kind: saved.due_kind,
      };
      return normalizeMetadataValue(source, {
        task_statuses: this.taskStatuses,
        task_defaults: this.taskDefaults,
      });
    },

    captureDraftFields(draft) {
      if (!draft) return {};
      return {
        ...metadataCoreObject(this.taskOptionsFromDraft(draft)),
        instructions: String(draft.instructions || ""),
      };
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

    async saveConflictAsNewDraft(draft) {
      if (!draft || this.draftBusy) return null;
      const newId = this.captureComposer.makeId();
      const created = this.captureComposer.localIsoSeconds(new Date());
      this.draftBusy = true;
      try {
        const saved = await this.getDraftStore().saveAsNew(draft, newId, created);
        this.prepareDraftForEditing(saved);
        if (this.captureComposer.draft === draft) {
          this.captureComposer.draft = saved;
        }
        this.captureComposer.recoveryWarning = "";
        this.captureComposer.message = "Saved as a new draft";
        this.captureComposer.messageError = false;
        this.draftWorkspaceStatus = "Saved in workspace";
        return saved;
      } catch (error) {
        this.draftWorkspaceStatus = "Error";
        this.captureComposer.recoveryWarning = (error && error.message) || "Could not save as a new draft.";
        return null;
      } finally {
        this.draftBusy = false;
      }
    },

    async finishWorkspaceDraft(draft) {
      if (!draft || draft.state === "conflict" || this.draftBusy) return null;
      this.draftBusy = true;
      try {
        const result = await this.getDraftStore().finish(draft);
        if (!result) return null;
        await this.applyPayload(result);
        if (this.captureComposer.draft === draft) {
          this.captureComposer.draft = null;
          this.captureComposer.close(this.captureContext());
        }
        this.draftWorkspaceStatus = "";
        this.captureComposer.recoveryWarning = "";
        this.captureComposer.message = result.saved_ref ? "Created " + result.saved_ref : "Capture created";
        this.captureComposer.messageError = false;
        return result;
      } catch (error) {
        this.draftWorkspaceStatus = "Error";
        this.captureComposer.message = (error && error.message) || "Could not apply draft";
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

    async discardWorkspaceDraft(draft, confirmed = false) {
      if (!draft || this.draftBusy) return;
      if (typeof window !== "undefined" && typeof window.confirm === "function" &&
          !confirmed && !window.confirm("Discard this draft? This removes it from the workspace.")) return;
      this.draftBusy = true;
      try {
        if (!await this.getDraftStore().discard(draft)) return;
        const discardedId = draft.id;
        if (this.captureComposer.draft && this.captureComposer.draft.id === discardedId) {
          this.captureComposer.destroyZenWorkspace();
          this.captureComposer.draft = null;
          this.captureComposer.zen = false;
          this.captureComposer.previewHtml = "";
          this.captureComposer.message = "";
          this.captureComposer.messageError = false;
          this.captureComposer.recoveryWarning = "";
          this.captureComposer.open = false;
        }
        this.draftWorkspaceStatus = "";
        if (typeof this.notify === "function") this.notify("Draft discarded");
      } catch (error) {
        this.draftWorkspaceStatus = "Error";
        const message = (error && error.message) || "Could not discard draft";
        this.captureComposer.message = message;
        this.captureComposer.messageError = true;
        if (typeof this.notify === "function") this.notify(message, true);
      } finally {
        this.draftBusy = false;
      }
    },

  };
}
