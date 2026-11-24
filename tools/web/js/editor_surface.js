// Shared Zen workspace, preview, and @mention state/methods
// mixed into both captureComposer and contentEditor.

function renderEditorPreview(card, text) {
  if (typeof renderMarkdown !== "function" || typeof marked === "undefined" || typeof DOMPurify === "undefined") {
    return "";
  }
  return renderMarkdown(card || {}, String(text || ""));
}

function formatZenMetaSummary(opts, instructions) {
  const meta = opts || {};
  const parts = [];
  if (meta.project) parts.push(meta.project);
  if (meta.topic) parts.push(meta.topic);
  if (Array.isArray(meta.tags) && meta.tags.length) {
    parts.push(meta.tags.length === 1 ? "1 tag" : meta.tags.length + " tags");
  }
  if (meta.due) parts.push("due " + meta.due);
  if (instructions && String(instructions).trim()) parts.push("instructions");
  return parts.join(" · ");
}

function mentionMethods(config = {}) {
  const setText = config.setText;
  const mentionIdPrefix = config.mentionIdPrefix || "mt-mention-";

  return {
    mention: typeof createMentionAutocomplete === "function" ? createMentionAutocomplete() : null,

    updateMention(ctx, textarea) {
      if (!this.mention) return;
      const cards = ctx && typeof ctx.getCards === "function" ? ctx.getCards() : [];
      this.mention.update(textarea, cards);
    },

    handleMentionKeydown(ctx, event, textarea) {
      if (!this.mention) return false;
      const handled = this.mention.handleKeydown(event, textarea, nextText => {
        setText(this, ctx, nextText);
      });
      if (handled && this.mention.open && ctx && typeof ctx.nextTick === "function") {
        ctx.nextTick(() => {
          const activeEl = typeof document !== "undefined" && document.getElementById
            ? document.getElementById(mentionIdPrefix + this.mention.activeIndex)
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
        setText(this, ctx, nextText);
      });
    },

    closeMention() {
      if (this.mention) this.mention.close();
    },

    repositionMention(textarea) {
      if (this.mention) this.mention.reposition(textarea);
    },
  };
}

function zenWorkspaceMethods(config = {}) {
  const getText = config.getText;
  const getPreviewCard = config.getPreviewCard || (() => ({}));
  const canEnterZen = config.canEnterZen || (() => true);
  const onBeforeToggleZen = config.onBeforeToggleZen;

  const focusEditor = (ctx) => {
    if (ctx && typeof ctx.focusInput === "function") ctx.focusInput();
  };

  return {
    zen: false,
    zenView: "write",
    zenMetaOpen: false,
    previewHtml: "",
    _previewTimer: null,
    ...mentionMethods(config),

    refreshPreview(ctx) {
      const text = getText(this);
      const card = getPreviewCard(this, ctx);
      if (typeof renderEditorPreview === "function") {
        this.previewHtml = renderEditorPreview(card, text);
      } else if (typeof renderMarkdown === "function" && typeof marked !== "undefined" && typeof DOMPurify !== "undefined") {
        this.previewHtml = renderMarkdown(card || {}, text || "");
      } else {
        this.previewHtml = "";
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
      if (!canEnterZen(this)) return;
      const next = typeof force === "boolean" ? force : !this.zen;
      this.zen = next;
      this.zenMetaOpen = false;
      this.closeMention();
      if (onBeforeToggleZen) onBeforeToggleZen(this, next);
      if (next && (this.zenView === "split" || this.zenView === "preview")) {
        this.refreshPreview(ctx);
      }
      if (ctx && typeof ctx.nextTick === "function") {
        ctx.nextTick(() => {
          if (!next || this.zenView !== "preview") focusEditor(ctx);
        });
      } else if ((!next || this.zenView !== "preview")) {
        focusEditor(ctx);
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
        if (ctx && typeof ctx.nextTick === "function") {
          ctx.nextTick(() => focusEditor(ctx));
        } else {
          focusEditor(ctx);
        }
      }
    },

    destroyZenWorkspace() {
      if (this._previewTimer) {
        clearTimeout(this._previewTimer);
        this._previewTimer = null;
      }
      this.closeMention();
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
    },
  };
}
