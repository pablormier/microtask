// Workspace draft persistence for new-card drafts. No editor, modal, DOM or Alpine state lives here.
// The store keeps one object per draft ID. A draft carries id, capture_id, created, kind,
// title, body, instructions, taskOptions, content_hash, state and _saved (the snapshot
// last written to the workspace, or null before the first save).
function createDraftStore({ context, captureFields, initialDrafts = [],
  onStatus = () => {}, onWarning = () => {}, onSaved = () => {} }) {
  const timers = Object.create(null);
  const pendingSaves = Object.create(null);
  const resave = Object.create(null);
  // Set when the page is about to restart for another signed-in person: nothing more is sent.
  let suspended = false;
  const CONFLICT_MESSAGE = "This draft changed since you opened it. Copy your text or save as a new draft.";

  function setStatus(value) {
    onStatus(value);
  }
  function inWorkspace(draft) {
    return context().available && !(draft.workspaceKey && draft.workspaceKey !== context().workspaceKey);
  }
  function editable(draft) {
    return Boolean(draft && draft.id && draft.state !== "conflict" && draft.state !== "closed" && inWorkspace(draft));
  }
  function clearTimer(key) {
    clearTimeout(timers[key]);
    delete timers[key];
  }
  function buildSavePayload(draft, { id, expectedHash, captureId }) {
    const payload = {
      id,
      kind: draft.kind || "note",
      title: String(draft.title || ""),
      body: String(draft.body || ""),
      capture_id: captureId,
      created: draft.created,
      ...(draft.origin ? { origin: draft.origin } : {}),
      ...captureFields(draft),
    };
    if (expectedHash) payload.expected_hash = expectedHash;
    return payload;
  }
  function markConflict(draft) {
    if (draft) draft.state = "conflict";
    setStatus("Error");
    onWarning(CONFLICT_MESSAGE);
  }
  function removeFromList(store, id) {
    store.drafts = store.drafts.filter(item => item.id !== id);
  }

  return {
    drafts: initialDrafts.slice(),

    async load() {
      const response = await fetch("/api/drafts", { cache: "no-store" });
      if (!response.ok) throw new Error("Could not load workspace drafts");
      const payload = await response.json();
      this.drafts = (payload.drafts || []).map(draft => {
        const loaded = { ...draft, state: "clean" };
        loaded._saved = this.snapshot(loaded);
        return loaded;
      });
    },

    upsert(saved) {
      if (!saved || !saved.id) return null;
      const index = this.drafts.findIndex(draft => draft.id === saved.id);
      if (index >= 0) {
        if (this.drafts[index] !== saved) Object.assign(this.drafts[index], saved);
        return this.drafts[index];
      }
      this.drafts.push(saved);
      return saved;
    },

    // Stops every pending and future save. The text stays in the draft objects for the caller to keep.
    suspend() {
      suspended = true;
      for (const key of Object.keys(timers)) clearTimer(key);
    },

    async post(path, body) {
      if (suspended) {
        const error = new Error("The signed-in account changed. Reload the page.");
        error.memberChanged = true;
        throw error;
      }
      const result = await postJson(path, body, context().apiToken);
      if (!result.ok) {
        const error = new Error(result.error || "Draft action failed");
        error.memberChanged = result.memberChanged;
        error.conflict = result.conflict;
        error.missing = result.missing;
        throw error;
      }
      return result.payload;
    },

    // Everything a save writes, as one comparable string.
    snapshot(draft) {
      const options = draft && draft.taskOptions && typeof draft.taskOptions === "object"
        ? draft.taskOptions : draft || {};
      const hasTaskFields = METADATA_FIELDS
        .some(field => Object.prototype.hasOwnProperty.call(options, field));
      return JSON.stringify({
        kind: (draft && draft.kind) || "note",
        title: String(draft && draft.title || ""),
        body: String(draft && draft.body || ""),
        metadata: hasTaskFields ? metadataCoreObject(options) : null,
        instructions: String(draft && draft.instructions || ""),
      });
    },

    hasChanges(draft) {
      return Boolean(draft) && this.snapshot(draft) !== draft._saved;
    },

    isEmpty(draft) {
      return Boolean(draft &&
        !String(draft.title || "").trim() &&
        !String(draft.body || "").trim() &&
        !String(draft.instructions || "").trim() &&
        !draft.content_hash &&
        !pendingSaves[String(draft.id)]);
    },

    forgetEmpty(draft) {
      if (!this.isEmpty(draft)) return;
      clearTimer(String(draft.id));
      removeFromList(this, draft.id);
    },

    async finish(draft, extra = {}) {
      if (!editable(draft) || !await this.flush(draft)) return null;
      try {
        const result = await this.post("/api/draft-finish", {
          id: draft.id,
          expected_hash: draft.content_hash,
          ...(extra || {}),
        });
        draft.state = "closed";
        removeFromList(this, draft.id);
        return result;
      } catch (error) {
        if (error.conflict || error.missing) markConflict(draft);
        throw error;
      }
    },

    async discard(draft) {
      if (!draft || !draft.id || draft.state === "closed" || !inWorkspace(draft)) return null;
      const key = String(draft.id);
      clearTimer(key);
      delete resave[key];
      if (pendingSaves[key]) await pendingSaves[key];
      clearTimer(key);
      // A draft that was never saved, or lost a conflict, has nothing of ours in the workspace.
      if (!draft.content_hash || draft.state === "conflict") {
        draft.state = "closed";
        removeFromList(this, draft.id);
        return { message: "Draft discarded" };
      }
      try {
        const result = await this.post("/api/draft-discard", {
          id: draft.id,
          expected_hash: draft.content_hash,
        });
        draft.state = "closed";
        removeFromList(this, draft.id);
        return result;
      } catch (error) {
        if (error.conflict || error.missing) markConflict(draft);
        throw error;
      }
    },

    async saveAsNew(draft, newId, created) {
      const payload = buildSavePayload({ ...draft, created: draft.created || created }, {
        id: newId,
        expectedHash: null,
        captureId: newId,
      });
      const result = await this.post("/api/draft-save", payload);
      const saved = { ...result.saved_draft, state: "clean" };
      saved._saved = this.snapshot(saved);
      draft.state = "closed";
      removeFromList(this, draft.id);
      this.upsert(saved);
      setStatus("Saved in workspace");
      onWarning("");
      return saved;
    },

    schedule(draft, delay = 450) {
      if (suspended || !editable(draft) || this.isEmpty(draft) || !this.hasChanges(draft)) return;
      draft.state = "dirty";
      const key = String(draft.id);
      clearTimer(key);
      setStatus("Saving…");
      timers[key] = setTimeout(() => {
        delete timers[key];
        this.save(draft);
      }, delay);
    },

    // One request per draft at a time. A save asked for while one is in flight
    // runs once more afterwards and resolves when the draft's latest text is saved.
    async save(draft) {
      if (!editable(draft)) return false;
      const key = String(draft.id);
      if (pendingSaves[key]) {
        resave[key] = true;
        return pendingSaves[key];
      }
      const run = (async () => {
        let ok;
        do {
          delete resave[key];
          ok = await this.send(draft);
        } while (ok && resave[key] && editable(draft) && this.hasChanges(draft));
        delete resave[key];
        return ok;
      })();
      pendingSaves[key] = run;
      try {
        return await run;
      } finally {
        if (pendingSaves[key] === run) delete pendingSaves[key];
      }
    },

    async send(draft) {
      if (suspended || this.isEmpty(draft)) return false;
      draft.state = "saving";
      const payload = buildSavePayload(draft, {
        id: draft.id,
        expectedHash: draft.content_hash || null,
        captureId: draft.capture_id,
      });
      const sent = this.snapshot(draft);
      try {
        const result = await this.post("/api/draft-save", payload);
        const saved = result.saved_draft;
        draft.updated = saved.updated;
        draft.content_hash = saved.content_hash;
        draft._saved = sent;
        draft.state = this.hasChanges(draft) ? "dirty" : "clean";
        this.upsert(draft);
        setStatus("Saved in workspace");
        onSaved(draft);
        return true;
      } catch (error) {
        if (error.memberChanged) {
          // Not a conflict and not a failed save: the text is kept for the page restart.
          draft.state = "dirty";
        } else if (error.conflict || error.missing) {
          markConflict(draft);
        } else {
          draft.state = "dirty";
          setStatus("Error");
          onWarning("Workspace save failed.");
        }
        return false;
      }
    },

    async flush(draft) {
      if (!editable(draft)) return false;
      const key = String(draft.id);
      clearTimer(key);
      if (pendingSaves[key]) await pendingSaves[key];
      clearTimer(key);
      if (this.isEmpty(draft)) return true;
      if (this.hasChanges(draft)) return this.save(draft);
      return true;
    },
  };
}
