// Selected-card property editor state. The board owns selection and applies
// every successful API payload; this component only builds and validates edits.
// Unsaved edits are also kept in one localStorage key per card
// (mt-unsaved-props:<ws>:<card id>), so a reload after a conflict does not lose them.
function propertyUnsavedKey(workspaceKey, cardId) {
  return workspaceKey && cardId ? "mt-unsaved-props:" + workspaceKey + ":" + cardId : "";
}

// Returns { fields, baseHash } or null. fields holds only the properties that were edited.
function readPropertyUnsaved(workspaceKey, cardId) {
  const key = propertyUnsavedKey(workspaceKey, cardId);
  if (!key || typeof localStorage === "undefined") return null;
  try {
    const value = JSON.parse(localStorage.getItem(key));
    if (value && typeof value === "object" && value.fields && typeof value.fields === "object" &&
        Object.keys(value.fields).length) {
      return { fields: value.fields, baseHash: typeof value.baseHash === "string" ? value.baseHash : "" };
    }
  } catch (_) {}
  return null;
}

function writePropertyUnsaved(workspaceKey, cardId, fields, baseHash) {
  const key = propertyUnsavedKey(workspaceKey, cardId);
  if (!key || typeof localStorage === "undefined") return;
  try { localStorage.setItem(key, JSON.stringify({ fields, baseHash: baseHash || "" })); } catch (_) {}
}

function clearPropertyUnsaved(workspaceKey, cardId) {
  const key = propertyUnsavedKey(workspaceKey, cardId);
  if (!key || typeof localStorage === "undefined") return;
  try { localStorage.removeItem(key); } catch (_) {}
}

function createPropertyEditor() {
  return {
    form: null,
    baseline: "",
    baselineHash: "",
    conflict: false,
    errors: {},
    version: 0,
    saving: false,
    error: "",
    hydrated: false,
    workspaceKey: "",
    // A backup from an earlier visit that the person may restore, and the older card hash a restored one was written against.
    offer: null,
    staleBase: "",

    values(task, defaults) {
      const core = normalizeMetadataValue(task, { task_defaults: defaults });
      return {
        title: task && task.title || "",
        ...core,
        next_action: task && task.next_action || "",
        processing_instructions: task && task.processing_instructions || "",
      };
    },

    comparable(value) {
      const source = value || {};
      const core = metadataCoreObject(source);
      delete core.status;
      return JSON.stringify({
        title: source.title || "",
        ...core,
        next_action: source.next_action || "",
        processing_instructions: source.processing_instructions || "",
      });
    },

    begin(task, defaults) {
      this.form = this.values(task, defaults);
      this.baseline = this.comparable(this.form);
      this.baselineHash = String(task && task.content_hash || "");
      this.hydrated = Boolean(task && typeof task.body === "string");
      this.conflict = false;
      this.staleBase = "";
      this.errors = {};
      this.error = "";
      this.version += 1;
    },

    // The properties whose values differ from the loaded card.
    changedFields() {
      let baseline = {};
      let current = {};
      try { baseline = JSON.parse(this.baseline || "{}"); } catch (_) {}
      try { current = JSON.parse(this.comparable(this.form)); } catch (_) {}
      const fields = {};
      for (const field of Object.keys(current)) {
        if (JSON.stringify(current[field]) !== JSON.stringify(baseline[field])) fields[field] = this.form[field];
      }
      return fields;
    },

    // Keeps the edits in the browser while the form is dirty. An offer that is still
    // waiting stays until the person restores or dismisses it, or starts editing.
    persist(workspaceKey, cardId) {
      if (!this.form || !cardId) return;
      this.workspaceKey = workspaceKey || this.workspaceKey;
      if (this.changed()) {
        this.offer = null;
        writePropertyUnsaved(this.workspaceKey, cardId, this.changedFields(), this.staleBase || this.baselineHash);
      } else if (!this.offer) {
        clearPropertyUnsaved(this.workspaceKey, cardId);
      }
    },

    offerBackup(task) {
      this.offer = null;
      const backup = task && this.form ? readPropertyUnsaved(this.workspaceKey, task.id) : null;
      if (!backup) return;
      const same = Object.keys(backup.fields).every(
        field => JSON.stringify(backup.fields[field]) === JSON.stringify(this.form[field]));
      if (same) clearPropertyUnsaved(this.workspaceKey, task.id);
      else this.offer = backup;
    },

    // Puts the backed-up edits back on the form. If the card changed since they were written,
    // saving stays blocked and the form says so, as after a conflict.
    restoreBackup() {
      const backup = this.offer;
      if (!backup || !this.form) return;
      this.offer = null;
      this.form = { ...this.form, ...backup.fields };
      if (backup.baseHash !== this.baselineHash) {
        this.conflict = true;
        this.staleBase = backup.baseHash || "stale";
      }
      this.version += 1;
    },

    dismissBackup(cardId) {
      this.offer = null;
      clearPropertyUnsaved(this.workspaceKey, cardId);
    },

    // Throws the person's edits away, on purpose: Reset, or leaving the card after confirming.
    discard(task, defaults) {
      this.offer = null;
      if (task) clearPropertyUnsaved(this.workspaceKey, task.id);
      this.begin(task, defaults);
    },

    hydrate(task, defaults) {
      const oldForm = this.form || {};
      let oldBaseline = {};
      try { oldBaseline = JSON.parse(this.baseline || "{}"); } catch (_) {}
      let prevComparable = {};
      try { prevComparable = JSON.parse(this.comparable(oldForm)); } catch (_) {}
      const form = this.values(task, defaults);
      for (const field of Object.keys(form)) {
        if (JSON.stringify(prevComparable[field]) !== JSON.stringify(oldBaseline[field])) form[field] = oldForm[field];
      }
      this.form = form;
      this.baseline = this.comparable(this.values(task, defaults));
      this.baselineHash = String(task && task.content_hash || "");
      this.hydrated = true;
      this.errors = {};
      this.version += 1;
    },

    changed() {
      return Boolean(this.form && this.comparable(this.form) !== this.baseline);
    },

    canApply(ctx, opts = {}) {
      const form = this.form;
      if (!ctx.selected || ctx.busy || ctx.draftBusy || (!opts.allowContentOpen && ctx.contentOpen) || !form ||
          this.saving || this.conflict || this.baselineHash !== String(ctx.selected.content_hash || "") || !this.changed()) return false;
      if (!String(form.title || "").trim() || /[\r\n\0]/.test(form.title)) return false;
      if (form.due && (!form.due_kind || !Object.prototype.hasOwnProperty.call(ctx.dueKindPolicy, form.due_kind))) return false;
      if (form.priority && !Object.prototype.hasOwnProperty.call(ctx.priorityPolicy, form.priority)) return false;
      if (ctx.selected.kind === "task" && form.type && !Object.prototype.hasOwnProperty.call(ctx.typePolicy, form.type)) return false;
      return true;
    },

    async apply(ctx, opts = {}) {
      if (!this.canApply(ctx, opts)) return null;
      const form = this.form;
      const original = ctx.selected;
      const patch = {};
      const fields = original.kind === "task" ? ["priority", "type", "project", "topic"] : ["priority", "project", "topic"];
      for (const field of fields) {
        const value = (form[field] || "") || null;
        const old = (original[field] || "") || null;
        if (value !== old) patch[field] = value;
      }
      const tags = Array.isArray(form.tags) ? form.tags : [];
      if (JSON.stringify(tags) !== JSON.stringify(original.tags || [])) patch.tags = tags;
      if (form.title !== original.title) patch.title = form.title;
      if (original.kind === "task" && form.next_action !== (original.next_action || "")) {
        patch.next_action = form.next_action || null;
      }
      if (form.processing_instructions !== (original.processing_instructions || "")) {
        patch.processing_instructions = form.processing_instructions || null;
      }
      if ((form.due || null) !== (original.due || null) || (form.due_kind || null) !== (original.due_kind || null)) {
        patch.due = form.due || null;
        patch.due_kind = form.due ? form.due_kind : null;
      }
      if (!Object.keys(patch).length) return null;
      this.saving = true;
      this.error = "";
      try {
        const result = await ctx.apiPost("/api/card-metadata", {
          ref: original.ref, expected_hash: this.baselineHash, patch,
        });
        if (result) clearPropertyUnsaved(this.workspaceKey, original.id);
        if (result && ctx.getSelected()) this.begin(ctx.getSelected(), ctx.defaults);
        else if (!result) this.error = ctx.actionMessage() || "Could not save card properties.";
        return result;
      } finally {
        this.saving = false;
      }
    },

  };
}
