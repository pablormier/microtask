// Workspace draft persistence. No editor, modal, DOM or Alpine state lives here.
// Queues stay private; each store has its own draft list and per-ID requests.
function createDraftStore({ context, findTarget, captureFields, initialDrafts = [],
  onStatus = () => {}, onWarning = () => {}, onSaved = () => {} }) {
  const timers = Object.create(null);
  const pendingSaves = Object.create(null);
  const queuedSaves = Object.create(null);
  function setStatus(value) {
    onStatus(value);
  }
  function draftBodyText(draft) {
    return String(draft && (draft.body == null ? draft.text || "" : draft.body));
  }
  function applySavedFields(target, saved, { body, title, kind, metadata }) {
    target.revision = saved.revision;
    target.updated = saved.updated;
    target.content_hash = saved.content_hash;
    target._serverSaved = true;
    target._savedBody = body;
    target._savedTitle = title;
    target._savedKind = kind;
    target._savedMetadata = metadata;
  }
  function buildSavePayload(draft, { id, expectedRevision, expectedHash, captureId, created, targetRef }) {
    const body = draftBodyText(draft);
    const title = String(draft.title || "");
    const payload = {
      id,
      expected_revision: expectedRevision,
      kind: draft.kind || "note",
      mode: draft.mode,
      title,
      body,
      ...(draft.origin ? { origin: draft.origin } : {}),
    };
    if (draft.mode === "new") {
      payload.capture_id = captureId;
      payload.created = created;
      Object.assign(payload, captureFields(draft));
    } else if (expectedRevision === 0) {
      payload.target_ref = targetRef;
      payload.base_hash = draft.base_hash || "0".repeat(64);
      payload.base_updated = draft.base_updated || "";
    }
    if (expectedRevision > 0) payload.expected_hash = expectedHash;
    return payload;
  }
  async function runDraftAction(store, endpoint, draft, { ignoreTargetConflict = false } = {}) {
    if (!draft || draft._orphanedLocal || !await store.flush(draft)) return null;
    const id = draft.id || draft.draftId;
    try {
      const result = await store.post(endpoint, {
        id,
        expected_revision: Number(draft.revision || 0),
        expected_hash: draft.content_hash,
      });
      store.drafts = store.drafts.filter(item => item.id !== id);
      store.markClosed(draft);
      return result;
    } catch (error) {
      if ((error.conflict || error.missing) && !(ignoreTargetConflict && error.targetConflict)) {
        store.markForRecovery(draft);
      }
      throw error;
    }
  }
  return {
    drafts: initialDrafts.slice(),

    async load() {
      const response = await fetch("/api/drafts", { cache: "no-store" });
      if (!response.ok) throw new Error("Could not load workspace drafts");
      const payload = await response.json();
      this.drafts = payload.drafts || [];
    },

    storageKey(workspaceKey, draftId) {
      const key = workspaceKey || context().workspaceKey;
      if (!key || !draftId) return "";
      return "mt-note-draft-v2:" + key + ":" + draftId;
    },

    async importLocal() {
      if (!context().available || typeof localStorage.length !== "number" ||
          typeof localStorage.key !== "function") return;
      const prefix = "mt-note-draft-v2:" + context().workspaceKey + ":";
      const keys = [];
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (key && key.startsWith(prefix)) keys.push(key);
      }
      for (const key of keys) {
        let local;
        try { local = JSON.parse(localStorage.getItem(key)); } catch (_) { continue; }
        const localId = local && (local.id || local.draftId) || key.slice(prefix.length);
        if (!local || !localId || !local.kind || !local.mode) continue;
        if (local.mode === "edit" && local.origin !== "card-body") continue;
        const body = draftBodyText(local);
        const current = this.drafts.find(draft => draft.id === localId);
        const title = String(local.title || "");
        const sameText = current && current.kind === (local.kind || "note") && current.mode === (local.mode || "new") &&
          current.body === body && String(current.title || "") === title &&
          String(current.origin || "") === String(local.origin || "") &&
          this.metadataSnapshot(current) === this.metadataSnapshot(local);
        if (!current && (local._serverSaved || Number(local.revision || 0) > 0 || this.wasDismissed(localId))) {
          continue;
        }
        const revision = current ? Number(current.revision) : 0;
        if (current && !sameText && (Number(local.revision || 0) !== revision || local.content_hash !== current.content_hash)) {
          setStatus("Error");
          continue;
        }
        if (sameText) {
          local.revision = revision;
          local.content_hash = current.content_hash;
          local._serverSaved = true;
          try { localStorage.setItem(key, JSON.stringify(local)); } catch (_) {}
          continue;
        }
        let targetRef;
        if (local.mode !== "new" && revision === 0) {
          const target = findTarget(local.ref, local.target);
          if (!target) continue;
          targetRef = target.ref;
        }
        const payload = buildSavePayload(local, {
          id: localId,
          expectedRevision: revision,
          expectedHash: current && current.content_hash,
          captureId: local.capture_id || local.captureId,
          created: local.created || local.createdAt,
          targetRef,
        });
        try {
          const result = await this.post("/api/draft-save", payload);
          this.upsert(result.saved_draft);
          applySavedFields(local, result.saved_draft, {
            body, title, kind: payload.kind, metadata: this.metadataSnapshot(local),
          });
          try { localStorage.setItem(key, JSON.stringify(local)); } catch (_) {}
          setStatus("Saved in workspace");
        } catch (_) {
          setStatus("Error");
        }
      }
    },

    restoreLocal() {
      if (typeof localStorage === "undefined" || typeof localStorage.length !== "number" ||
          typeof localStorage.key !== "function" || !context().workspaceKey) return;
      const prefix = "mt-note-draft-v2:" + context().workspaceKey + ":";
      this.drafts = this.drafts.filter(draft => !draft._localRecovery);
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (!key || !key.startsWith(prefix)) continue;
        let local;
        try { local = JSON.parse(localStorage.getItem(key)); } catch (_) { continue; }
        if (!local || !local.kind || !local.mode) continue;
        if (local.mode === "edit" && local.origin !== "card-body") continue;
        const id = local.id || local.draftId || key.slice(prefix.length);
        if (!id) continue;
        // A closed ID is a durable signal from another tab. Ignore its stale
        // localStorage copy even when the canonical draft still exists.
        if (this.wasDismissed(id)) {
          try { localStorage.removeItem(key); } catch (_) {}
          continue;
        }
        const body = String(local.body == null ? local.text || "" : local.body);
        const title = String(local.title || "");
        const current = this.drafts.find(draft => draft.id === id);
        if (current) {
          if ((local.kind || "note") === (current.kind || "note") && (local.mode || "new") === (current.mode || "new") &&
              body === String(current.body || "") && title === String(current.title || "") &&
              this.metadataSnapshot(local) === this.metadataSnapshot(current)) continue;
          const recoveryId = id + ":local-recovery";
          this.drafts.push({
            ...local, id: recoveryId, sourceId: id, storageId: id, body, title,
            _localRecovery: true, _orphanedLocal: true, _staleLocal: true,
          });
          continue;
        }
        const wasSaved = Boolean(local._serverSaved) || Number(local.revision || 0) > 0;
        this.drafts.push({
          ...local, id, body, title,
          revision: Number(local.revision || 0),
          _savedBody: local._savedBody,
          _savedTitle: local._savedTitle,
          sourceId: id, storageId: id,
          _localRecovery: wasSaved,
          _orphanedLocal: wasSaved,
          _staleLocal: wasSaved,
        });
      }
    },

    dismissKey(id) {
      return "mt-draft-closed-v1:" + context().workspaceKey + ":" + id;
    },

    wasDismissed(id) {
      try { return Boolean(localStorage.getItem(this.dismissKey(id))); } catch (_) { return false; }
    },

    upsert(saved) {
      if (!saved || !saved.id) return;
      const index = this.drafts.findIndex(draft => draft.id === saved.id);
      if (index >= 0) this.drafts.splice(index, 1, { ...this.drafts[index], ...saved, _localRecovery: false, _orphanedLocal: false });
      else this.drafts.push(saved);
    },

    async post(path, body) {
      const response = await fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Microtask-Token": context().apiToken },
        body: JSON.stringify(body || {}),
      });
      const payload = await response.json();
      if (!response.ok) {
        const error = new Error(payload.error || "Draft action failed");
        error.conflict = Boolean(payload.conflict || response.status === 409);
        error.missing = response.status === 404;
        error.targetConflict = /target changed since the draft|no verified target baseline|draft target is missing/i.test(error.message);
        throw error;
      }
      return payload;
    },

    metadataSnapshot(draft) {
      // Edit drafts carry changes in their body; server metadata fields are empty defaults.
      if (draft && draft.mode === "edit") return "";
      const options = draft && draft.taskOptions && typeof draft.taskOptions === "object"
        ? draft.taskOptions : draft || {};
      const hasTaskFields = METADATA_FIELDS
        .some(field => Object.prototype.hasOwnProperty.call(options, field));
      if (!hasTaskFields) return "";
      return JSON.stringify({
        ...metadataCoreObject(options),
        instructions: String(draft && draft.instructions || ""),
      });
    },

    hasChanges(draft) {
      if (!draft) return false;
      const body = String(draft.body == null ? draft.text || "" : draft.body);
      const title = String(draft.title || "");
      const kind = draft.kind || "note";
      const savedMetadata = Object.prototype.hasOwnProperty.call(draft, "_savedMetadata")
        ? draft._savedMetadata : this.metadataSnapshot(draft);
      return draft._savedBody !== body || draft._savedTitle !== title ||
        (draft._savedKind || kind) !== kind || savedMetadata !== this.metadataSnapshot(draft);
    },

    saveLocal(draft) {
      const id = draft.storageId || draft.sourceId || draft.id || draft.draftId;
      const key = this.storageKey(draft.workspaceKey || context().workspaceKey, id);
      if (!key) return;
      try {
        localStorage.setItem(key, JSON.stringify({
          ...draft, id, body: String(draft.body == null ? draft.text || "" : draft.body), _serverSaved: Boolean(draft._serverSaved || Number(draft.revision || 0) > 0),
        }));
        return true;
      } catch (_) { return false; }
    },

    markClosed(draft) {
      const id = draft && (draft.storageId || draft.sourceId || draft.id || draft.draftId);
      if (!id || !context().workspaceKey) return;
      try {
        localStorage.setItem(this.dismissKey(id), JSON.stringify({ state: "closed", id }));
        localStorage.removeItem(this.storageKey(context().workspaceKey, id));
      } catch (_) {}
    },

    removeRecovery(sourceId, selected) {
      this.drafts = this.drafts.filter(item => {
        if (item === selected) return false;
        if (!item) return true;
        const itemSourceId = item.sourceId || item.storageId || item.id;
        // An unsaved inline row can coexist with its explicit recovery copy.
        // Remove both local rows while retaining any canonical saved sibling.
        if (!item._orphanedLocal && (item._serverSaved || Number(item.revision || 0) > 0)) return true;
        return itemSourceId !== sourceId && item.id !== sourceId + ":local-recovery";
      });
    },

    markForRecovery(draft, message) {
      const id = draft && (draft.sourceId || draft.id || draft.draftId);
      if (!draft || !id) return;
      draft.sourceId = id;
      draft.storageId = id;
      draft._orphanedLocal = true;
      draft._localRecovery = true;
      draft._staleLocal = true;
      this.saveLocal(draft);
      const recoveryId = id + ":local-recovery";
      if (!this.drafts.some(item => item.id === recoveryId)) {
        this.drafts.push({ ...draft, id: recoveryId, sourceId: id, storageId: id,
          body: String(draft.body == null ? draft.text || "" : draft.body),
          _localRecovery: true, _orphanedLocal: true, _staleLocal: true });
      }
      setStatus("Error");
      onWarning(message || "The workspace draft changed or was removed. Recover your local text as a separate draft or dismiss it.");
    },

    isEmpty(draft) {
      const id = draft && (draft.id || draft.draftId);
      return Boolean(draft && draft.mode === "new" &&
        !String(draft.title || "").trim() &&
        !String(draft.body == null ? draft.text || "" : draft.body).trim() &&
        !String(draft.instructions || "").trim() &&
        Number(draft.revision || 0) === 0 && !draft._serverSaved &&
        !pendingSaves[String(id)]);
    },

    forgetEmpty(draft) {
      if (!this.isEmpty(draft)) return;
      const id = draft.id || draft.draftId || draft.captureId;
      clearTimeout(timers[String(id)]);
      delete timers[String(id)];
      try { localStorage.removeItem(this.storageKey(draft.workspaceKey, id)); } catch (_) {}
      this.drafts = this.drafts.filter(item => item.id !== id);
    },

    dismiss(draft) {
      const sourceId = draft.sourceId || draft.id;
      this.markClosed(draft);
      this.removeRecovery(sourceId, draft);
      return sourceId;
    },

    async recover(draft, newId, created) {
      const sourceId = draft.sourceId || draft.storageId || draft.id;
      let targetRef;
      if (draft.mode !== "new") {
        const target = findTarget(null, draft.target) || findTarget(draft.ref);
        if (!target) throw new Error("The original item is unavailable. Keep this local copy and reconcile it manually.");
        targetRef = target.ref;
      }
      const payload = buildSavePayload(draft, {
        id: newId,
        expectedRevision: 0,
        // Divergent text must not share an already-promoted capture's identity.
        captureId: newId,
        created: draft.created || draft.createdAt || created,
        targetRef,
      });
      const result = await this.post("/api/draft-save", payload);
      this.markClosed({ ...draft, sourceId });
      this.removeRecovery(sourceId, draft);
      this.upsert(result.saved_draft);
      setStatus("Recovered as a separate workspace draft");
      return result.saved_draft;
    },

    finish(draft) {
      return runDraftAction(this, "/api/draft-finish", draft, { ignoreTargetConflict: true });
    },

    discard(draft) {
      return runDraftAction(this, "/api/draft-discard", draft);
    },

    schedule(draft, delay = 450) {
      const id = draft && (draft.id || draft.draftId);
      if (!draft || !id || draft._orphanedLocal || !context().available ||
          (draft.workspaceKey && draft.workspaceKey !== context().workspaceKey)) return;
      if (this.isEmpty(draft)) return;
      if (!this.hasChanges(draft)) return;
      const key = String(id);
      clearTimeout(timers[key]);
      setStatus("Saving…");
      timers[key] = setTimeout(() => {
        delete timers[key];
        this.save(draft);
      }, delay);
    },

    async save(draft) {
      const draftId = draft && (draft.id || draft.draftId);
      if (!draft || !draftId || draft._orphanedLocal || !context().available ||
          (draft.workspaceKey && draft.workspaceKey !== context().workspaceKey)) return false;
      const key = String(draftId);
      const pending = pendingSaves[key];
      if (pending) {
        queuedSaves[key] = draft;
        await pending;
        const queued = queuedSaves[key];
        delete queuedSaves[key];
        if (queued && this.hasChanges(queued)) {
          return this.save(queued);
        }
        return true;
      }
      if (draft.mode === "new" && this.isEmpty(draft)) return false;
      const body = draftBodyText(draft);
      const title = String(draft.title || "");
      const expectedRevision = Number(draft.revision || 0);
      const payload = buildSavePayload(draft, {
        id: draftId,
        expectedRevision,
        expectedHash: draft.content_hash,
        captureId: draft.capture_id || draft.captureId,
        created: draft.created || draft.createdAt,
        targetRef: draft.ref || (findTarget(null, draft.target) || {}).ref,
      });
      const savedKind = payload.kind;
      const savedMetadata = this.metadataSnapshot({ ...payload, kind: savedKind });
      const request = (async () => {
        try {
          const result = await this.post("/api/draft-save", payload);
          const saved = result.saved_draft;
          applySavedFields(draft, saved, { body, title, kind: savedKind, metadata: savedMetadata });
          this.upsert(saved);
          setStatus("Saved in workspace");
          this.saveLocal(draft);
          onSaved(draft);
          return true;
        } catch (error) {
          if (error.conflict || error.missing) {
            this.markForRecovery(draft);
          } else {
            setStatus("Error");
            onWarning("Workspace save failed. Your local recovery copy is still available.");
          }
          return false;
        }
      })();
      pendingSaves[key] = request;
      const result = await request;
      if (pendingSaves[key] === request) delete pendingSaves[key];
      const queued = queuedSaves[key];
      delete queuedSaves[key];
      if (queued && this.hasChanges(queued)) {
        return this.save(queued);
      }
      return result;
    },

    async flush(draft) {
      if (!draft || draft._orphanedLocal || !context().available ||
          (draft.workspaceKey && draft.workspaceKey !== context().workspaceKey)) return false;
      const id = draft && (draft.id || draft.draftId);
      const key = id ? String(id) : "";
      if (key) {
        clearTimeout(timers[key]);
        delete timers[key];
        if (pendingSaves[key]) await pendingSaves[key];
        clearTimeout(timers[key]);
        delete timers[key];
      }
      const body = String(draft.body == null ? draft.text || "" : draft.body);
      const title = String(draft.title || "");
      if (this.isEmpty(draft)) return true;
      if (this.hasChanges({ ...draft, body, title })) return this.save(draft);
      return true;
    },
  };
}
