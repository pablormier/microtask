// Selected-card property editor state. The board owns selection and applies
// every successful API payload; this component only builds and validates edits.
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
      this.errors = {};
      this.error = "";
      this.version += 1;
    },

    hydrate(task, defaults) {
      const oldForm = this.form || {};
      let oldBaseline = {};
      try { oldBaseline = JSON.parse(this.baseline || "{}"); } catch (_) {}
      const form = this.values(task, defaults);
      for (const field of Object.keys(form)) {
        if (JSON.stringify(oldForm[field]) !== JSON.stringify(oldBaseline[field])) form[field] = oldForm[field];
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

    canApply(ctx) {
      const form = this.form;
      if (!ctx.selected || ctx.busy || ctx.draftBusy || ctx.contentOpen || !form ||
          this.saving || this.conflict || this.baselineHash !== String(ctx.selected.content_hash || "") || !this.changed()) return false;
      if (!String(form.title || "").trim() || /[\r\n\0]/.test(form.title)) return false;
      if (form.due && (!form.due_kind || !Object.prototype.hasOwnProperty.call(ctx.dueKindPolicy, form.due_kind))) return false;
      if (form.priority && !Object.prototype.hasOwnProperty.call(ctx.priorityPolicy, form.priority)) return false;
      if (ctx.selected.kind === "task" && form.type && !Object.prototype.hasOwnProperty.call(ctx.typePolicy, form.type)) return false;
      return true;
    },

    async apply(ctx) {
      if (!this.canApply(ctx)) return null;
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
        if (result && ctx.getSelected()) this.begin(ctx.getSelected(), ctx.defaults);
        else if (!result) this.error = ctx.actionMessage() || "Could not save card properties.";
        return result;
      } finally {
        this.saving = false;
      }
    },

  };
}
