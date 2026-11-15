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
