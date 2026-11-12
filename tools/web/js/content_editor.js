// One body-only editor for tasks and notes. Properties remain separate controls;
// the saved draft still uses the shared task/note draft schema.
function createContentEditor() {
  return {
    draft: null,
    text: "",
    original: "",
    tab: "write",
    localSaved: true,
    saving: false,
    error: "",

    begin(ctx, saved) {
      const card = ctx.selected;
      if (!card || ctx.busy || ctx.draftBusy || typeof card.body !== "string") return;
      const existing = saved || ctx.getDrafts().find(draft => draft.mode === "edit" &&
        draft.origin === "card-body" && draft.target === card.path && !draft._orphanedLocal);
      const source = String(existing ? existing.body || "" : card.body);
      this.draft = existing ? { ...existing } : {
        id: ctx.makeId(), kind: card.kind, mode: "edit", target: card.path,
        ref: card.ref, base_hash: card.content_hash, base_updated: card.updated,
        origin: "card-body", revision: 0, title: card.title, body: source,
        _savedBody: source, _savedTitle: card.title, _savedKind: card.kind, _savedMetadata: "",
      };
      this.localSaved = true;
      this.text = source;
      // Compare an edit draft with the canonical card body, not with the
      // draft being reopened. This keeps recovered divergent text saveable.
      this.original = card.body;
      this.tab = "write";
      this.error = "";
      ctx.clearStatus();
      ctx.focusInput();
    },

    persist(ctx) {
      const draft = this.draft;
      if (!draft) return;
      draft.body = this.text;
      if (draft.body === this.original && !draft.revision) return;
      this.localSaved = Boolean(ctx.store.saveLocal(draft));
      ctx.store.upsert({ ...draft, _localRecovery: true });
      if (!draft._orphanedLocal) ctx.scheduleDraftSave(draft);
    },

    close(ctx) {
      if (!this.draft) return;
      this.persist(ctx);
      if (!this.localSaved && this.draft.body !== this.draft._savedBody) {
        this.error = "The draft could not be saved locally. Keep the editor open until the workspace save succeeds.";
        return false;
      }
      this.draft = null;
      this.text = "";
      this.error = "";
    },

    preview(ctx) {
      if (typeof renderMarkdown !== "function" || typeof marked === "undefined" || typeof DOMPurify === "undefined") return "";
      return renderMarkdown(ctx.selected, this.text);
    },

    canSave(ctx) {
      const draft = this.draft;
      return Boolean(draft && !draft._orphanedLocal && !this.saving && !ctx.draftBusy && !ctx.busy &&
        !ctx.needsReconciliation(draft) && draft.body !== this.original);
    },

    async save(ctx) {
      if (!this.canSave(ctx)) return null;
      this.persist(ctx);
      this.saving = true;
      this.error = "";
      try {
        return await ctx.finishWorkspaceDraft(this.draft);
      } finally {
        this.saving = false;
      }
    },
  };
}
