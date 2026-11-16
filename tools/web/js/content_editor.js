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
    ...zenWorkspaceMethods({
      getText: editor => editor.text,
      setText: (editor, ctx, nextText) => {
        editor.text = nextText;
        editor.persist(ctx);
        editor.schedulePreview(ctx);
      },
      isDisabled: (editor, ctx) => Boolean((ctx && ctx.draftBusy) || editor.saving),
      getPlaceholder: () => "Write Markdown or LaTeX…",
      getPreviewCard: (editor, ctx) => (ctx ? ctx.selected : {}),
      canEnterZen: editor => Boolean(editor.draft),
      mentionIdPrefix: "mt-detail-mention-",
      onBeforeToggleZen: (editor, next) => {
        if (next) {
          if (editor.tab === "preview") editor.zenView = "preview";
          else if (editor.zenView === "preview") editor.zenView = "write";
        } else {
          editor.tab = editor.zenView === "preview" ? "preview" : "write";
        }
      },
      onSave: (editor, ctx) => editor.save(ctx),
      onEscape: (editor, ctx) => editor.handleEscape(ctx),
    }),

    handleEscape(ctx) {
      if (this.mention && this.mention.open) {
        this.mention.close();
        return true;
      }
      if (this.zenMetaOpen) {
        this.zenMetaOpen = false;
        return true;
      }
      if (this.zen) {
        this.toggleZen(ctx, false);
        return true;
      }
      return false;
    },

    zenMetaSummary(form) {
      if (!form) return "";
      return formatZenMetaSummary(form, form.processing_instructions);
    },

    beginZen(ctx, saved) {
      this.begin(ctx, saved);
      if (this.draft) this.toggleZen(ctx, true);
    },

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
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
      this.error = "";
      if (this.surface) this.surface.setValue(source);
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
      this.destroyZenWorkspace();
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
      if (!draft || draft._orphanedLocal || this.saving || ctx.draftBusy || ctx.busy ||
          ctx.needsReconciliation(draft)) return false;
      const bodyDirty = this.text !== this.original;
      const propsDirty = Boolean(this.zen && ctx.hasPropertyChanges && ctx.hasPropertyChanges());
      if (!bodyDirty && !propsDirty) return false;
      if (propsDirty && ctx.canSaveProperties && !ctx.canSaveProperties()) return false;
      return true;
    },

    async save(ctx) {
      if (!this.canSave(ctx)) return null;
      const bodyDirty = this.text !== this.original;
      const propsDirty = Boolean(this.zen && ctx.hasPropertyChanges && ctx.hasPropertyChanges());
      if (bodyDirty) this.persist(ctx);
      this.saving = true;
      this.error = "";
      try {
        let result = null;
        if (bodyDirty) {
          result = await ctx.finishWorkspaceDraft(this.draft);
          if (!result) return null;
        }
        if (propsDirty && ctx.saveProperties) {
          const propResult = await ctx.saveProperties();
          if (!propResult) {
            this.error = (ctx.propertyError && ctx.propertyError()) || "Could not save card properties.";
            return null;
          }
          result = propResult;
          if (!bodyDirty) this.close(ctx);
        }
        return result;
      } finally {
        this.saving = false;
      }
    },
  };
}
