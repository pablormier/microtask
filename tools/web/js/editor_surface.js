// Minimal adapter between microtask's draft state and the underlying text editor.
// Uses the native <textarea> by default; if window.MTZenEditorEngine is registered,
// mounts the custom engine inside the Zen editor host container.
function createEditorSurface(options = {}) {
  const getValue = typeof options.getValue === "function" ? options.getValue : () => "";
  const onInput = typeof options.onInput === "function" ? options.onInput : () => {};
  const onSave = typeof options.onSave === "function" ? options.onSave : () => {};
  const onEscape = typeof options.onEscape === "function" ? options.onEscape : () => {};
  const getPlaceholder = typeof options.getPlaceholder === "function" ? options.getPlaceholder : () => "";
  const isDisabled = typeof options.isDisabled === "function" ? options.isDisabled : () => false;

  return {
    instance: null,
    hostEl: null,

    hasCustomEngine() {
      return Boolean(
        typeof window !== "undefined" &&
        window.MTZenEditorEngine &&
        typeof window.MTZenEditorEngine.mount === "function"
      );
    },

    mount(hostEl) {
      this.destroy();
      this.hostEl = hostEl || null;
      if (!hostEl || !this.hasCustomEngine()) return false;
      this.instance = window.MTZenEditorEngine.mount(hostEl, {
        value: String(getValue() || ""),
        placeholder: String(getPlaceholder() || ""),
        disabled: Boolean(isDisabled()),
        onChange: text => onInput(String(text || "")),
        onSave: () => onSave(),
        onEscape: () => onEscape(),
      });
      return Boolean(this.instance);
    },

    setValue(text) {
      if (this.instance && typeof this.instance.setValue === "function") {
        this.instance.setValue(String(text || ""));
      }
    },

    setDisabled(disabled) {
      if (this.instance && typeof this.instance.setDisabled === "function") {
        this.instance.setDisabled(Boolean(disabled));
      }
    },

    focus() {
      if (this.instance && typeof this.instance.focus === "function") {
        this.instance.focus();
        return true;
      }
      if (this.hostEl && typeof this.hostEl.querySelector === "function") {
        const textarea = this.hostEl.querySelector("textarea");
        if (textarea) {
          textarea.focus();
          return true;
        }
      }
      return false;
    },

    destroy() {
      if (this.instance && typeof this.instance.destroy === "function") {
        this.instance.destroy();
      }
      this.instance = null;
      this.hostEl = null;
    },
  };
}

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

// Shared Zen workspace, preview, custom editor surface, and @mention state/methods
// mixed into both captureComposer and contentEditor.
function zenWorkspaceMethods(config = {}) {
  const getText = config.getText;
  const setText = config.setText;
  const isDisabled = config.isDisabled || (() => false);
  const getPlaceholder = config.getPlaceholder || (() => "");
  const getPreviewCard = config.getPreviewCard || (() => ({}));
  const canEnterZen = config.canEnterZen || (() => true);
  const mentionIdPrefix = config.mentionIdPrefix || "mt-mention-";
  const onBeforeToggleZen = config.onBeforeToggleZen;
  const onSave = config.onSave;
  const onEscape = config.onEscape;

  const focusEditor = (editor, ctx) => {
    if (!(editor.surface && editor.surface.focus())) {
      if (ctx && typeof ctx.focusInput === "function") ctx.focusInput();
    }
  };

  return {
    zen: false,
    zenView: "write",
    zenMetaOpen: false,
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

    ensureSurface(ctx, hostEl) {
      if (typeof createEditorSurface !== "function") return null;
      if (!this.surface) {
        this.surface = createEditorSurface({
          getValue: () => getText(this),
          onInput: text => setText(this, ctx, text),
          onSave: () => onSave && onSave(this, ctx),
          onEscape: () => onEscape && onEscape(this, ctx),
          getPlaceholder: () => getPlaceholder(this),
          isDisabled: () => Boolean(isDisabled(this, ctx)),
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
        surface.setDisabled(Boolean(isDisabled(this, ctx)));
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
          if (!next || this.zenView !== "preview") focusEditor(this, ctx);
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
        if (ctx && typeof ctx.nextTick === "function") {
          ctx.nextTick(() => focusEditor(this, ctx));
        } else {
          focusEditor(this, ctx);
        }
      }
    },

    destroyZenWorkspace() {
      if (this._previewTimer) {
        clearTimeout(this._previewTimer);
        this._previewTimer = null;
      }
      if (this.surface) this.surface.destroy();
      this.closeMention();
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
    },
  };
}
