// One body-only editor for tasks and notes. Properties remain separate controls;
// the saved draft still uses the shared task/note draft schema.
function createContentEditor() {
  return {
    draft: null,
    text: "",
    original: "",
    tab: "write",
    zen: false,
    zenView: "write",
    previewHtml: "",
    _previewTimer: null,
    surface: null,
    mention: typeof createMentionAutocomplete === "function" ? createMentionAutocomplete() : null,

    updateMention(ctx, textarea) {
      if (!this.mention) return;
      const cards = ctx && typeof ctx.getCards === "function" ? ctx.getCards() : [];
      this.mention.update(textarea, cards);
    },

    handleMentionKeydown(ctx, event, textarea) {
      if (!this.mention) return false;
      const handled = this.mention.handleKeydown(event, textarea, nextText => {
        this.text = nextText;
        this.persist(ctx);
        this.schedulePreview(ctx);
      });
      if (handled && this.mention.open && ctx && typeof ctx.nextTick === "function") {
        ctx.nextTick(() => {
          const activeEl = typeof document !== "undefined" && document.getElementById
            ? document.getElementById("mt-detail-mention-" + this.mention.activeIndex)
            : null;
          if (activeEl && typeof activeEl.scrollIntoView === "function") {
            activeEl.scrollIntoView({ block: "nearest" });
          }
        });
      }
      return handled;
    },

    handleMentionKeyup(ctx, event, textarea) {
      if (!this.mention || !this.mention.open || !event) return;
      if (event.isComposing || event.keyCode === 229) return;
      if (["ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"].includes(event.key)) {
        this.updateMention(ctx, textarea);
      }
    },

    pickMention(ctx, item, textarea) {
      if (!this.mention) return;
      this.mention.apply(textarea, item, nextText => {
        this.text = nextText;
        this.persist(ctx);
        this.schedulePreview(ctx);
      });
    },

    closeMention() {
      if (this.mention) this.mention.close();
    },

    repositionMention(textarea) {
      if (this.mention) this.mention.reposition(textarea);
    },
    localSaved: true,
    saving: false,
    error: "",

    ensureSurface(ctx, hostEl) {
      if (typeof createEditorSurface !== "function") return null;
      if (!this.surface) {
        this.surface = createEditorSurface({
          getValue: () => this.text,
          onInput: text => {
            this.text = text;
            this.persist(ctx);
            this.schedulePreview(ctx);
          },
          onSave: () => this.save(ctx),
          onEscape: () => this.handleEscape(ctx),
          getPlaceholder: () => "Write Markdown or LaTeX…",
          isDisabled: () => Boolean(ctx.draftBusy || this.saving),
        });
      }
      if (hostEl && this.zen && this.surface.hasCustomEngine() && !this.surface.instance) {
        this.surface.mount(hostEl);
      }
      return this.surface;
    },

    syncSurface(ctx, hostEl) {
      const surface = this.ensureSurface(ctx, hostEl);
      if (!surface) return;
      if (this.zen && surface.hasCustomEngine()) {
        if (hostEl && (!surface.instance || surface.hostEl !== hostEl)) {
          surface.mount(hostEl);
        }
        surface.setDisabled(Boolean((ctx && ctx.draftBusy) || this.saving));
      } else if (surface.instance) {
        surface.destroy();
      }
    },

    hasCustomEngine() {
      return Boolean(this.surface && this.surface.hasCustomEngine());
    },

    isCustomMounted() {
      return Boolean(this.surface && this.surface.instance);
    },

    refreshPreview(ctx) {
      if (typeof renderEditorPreview === "function") {
        this.previewHtml = renderEditorPreview(ctx ? ctx.selected : {}, this.text);
      } else {
        this.previewHtml = this.preview(ctx);
      }
    },

    schedulePreview(ctx) {
      if (!this.zen || (this.zenView !== "split" && this.zenView !== "preview")) return;
      if (this._previewTimer) clearTimeout(this._previewTimer);
      this._previewTimer = setTimeout(() => {
        this._previewTimer = null;
        this.refreshPreview(ctx);
      }, 150);
    },

    toggleZen(ctx, force) {
      if (!this.draft) return;
      const next = typeof force === "boolean" ? force : !this.zen;
      this.zen = next;
      this.closeMention();
      if (next) {
        if (this.tab === "preview") this.zenView = "preview";
        else if (this.zenView === "preview") this.zenView = "write";
        if (this.zenView === "split" || this.zenView === "preview") {
          this.refreshPreview(ctx);
        }
      } else {
        this.tab = this.zenView === "preview" ? "preview" : "write";
      }
      if (ctx && typeof ctx.nextTick === "function") {
        ctx.nextTick(() => {
          if (!next || this.zenView !== "preview") {
            if (!(this.surface && this.surface.focus())) {
              if (typeof ctx.focusInput === "function") ctx.focusInput();
            }
          }
        });
      } else if ((!next || this.zenView !== "preview") && ctx && typeof ctx.focusInput === "function") {
        ctx.focusInput();
      }
    },

    setZenView(ctx, view) {
      if (view !== "write" && view !== "split" && view !== "preview") return;
      this.zenView = view;
      if (view === "preview") this.closeMention();
      if (view === "split" || view === "preview") {
        if (this._previewTimer) {
          clearTimeout(this._previewTimer);
          this._previewTimer = null;
        }
        this.refreshPreview(ctx);
      }
      if (view !== "preview") {
        const focusEditor = () => {
          if (!(this.surface && this.surface.focus())) {
            if (ctx && typeof ctx.focusInput === "function") ctx.focusInput();
          }
        };
        if (ctx && typeof ctx.nextTick === "function") ctx.nextTick(focusEditor);
        else focusEditor();
      }
    },

    handleEscape(ctx) {
      if (this.mention && this.mention.open) {
        this.mention.close();
        return true;
      }
      if (this.zen) {
        this.toggleZen(ctx, false);
        return true;
      }
      return false;
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
      if (this._previewTimer) {
        clearTimeout(this._previewTimer);
        this._previewTimer = null;
      }
      if (this.surface) this.surface.destroy();
      if (this.mention) this.mention.close();
      this.draft = null;
      this.text = "";
      this.zen = false;
      this.zenView = "write";
      this.previewHtml = "";
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
