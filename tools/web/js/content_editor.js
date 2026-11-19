// Direct body editor for existing cards. Uses content_hash for conflict detection
// and one localStorage key (mt-unsaved:<ws>:<card id>) for crash safety.
function cardUnsavedStorageKey(workspaceKey, cardId) {
  return workspaceKey && cardId ? "mt-unsaved:" + workspaceKey + ":" + cardId : "";
}

// A backup older than this feature stored plain text with no base hash. This
// placeholder never matches a card, so saving such text reports a conflict.
const UNKNOWN_CARD_BASE_HASH = "0".repeat(64);
const STALE_UNSAVED_MESSAGE = "This text was written against an older version of the card. " +
  "Reload the card to save it over the current version.";

// Returns { text, baseHash } or null. baseHash is the card hash the text was based on.
function readCardUnsaved(workspaceKey, cardId) {
  const key = cardUnsavedStorageKey(workspaceKey, cardId);
  if (!key || typeof localStorage === "undefined") return null;
  let raw;
  try { raw = localStorage.getItem(key); } catch (_) { return null; }
  if (raw === null || raw === undefined) return null;
  try {
    const value = JSON.parse(raw);
    if (value && typeof value === "object" && typeof value.text === "string") {
      return { text: value.text, baseHash: typeof value.baseHash === "string" ? value.baseHash : "" };
    }
  } catch (_) {}
  return { text: String(raw), baseHash: "" };
}

function writeCardUnsaved(workspaceKey, cardId, text, baseHash) {
  const key = cardUnsavedStorageKey(workspaceKey, cardId);
  if (!key || typeof localStorage === "undefined") return false;
  try {
    localStorage.setItem(key, JSON.stringify({ text, baseHash: baseHash || "" }));
    return true;
  } catch (_) { return false; }
}

// The hash to save restored text against: its own base, so a card that changed
// since the backup was written reports a conflict instead of being overwritten.
function restoredCardBase(baseHash, currentHash) {
  if (baseHash && baseHash === currentHash) return { hash: currentHash, stale: false };
  return { hash: baseHash || UNKNOWN_CARD_BASE_HASH, stale: true };
}

function clearCardUnsavedText(workspaceKey, cardId) {
  const key = cardUnsavedStorageKey(workspaceKey, cardId);
  if (!key || typeof localStorage === "undefined") return;
  try { localStorage.removeItem(key); } catch (_) {}
}

function createContentEditor() {
  return {
    open: false,
    cardId: "",
    ref: "",
    workspaceKey: "",
    contentHash: "",
    text: "",
    original: "",
    unsavedOffer: "",
    unsavedBase: "",
    tab: "write",
    saving: false,
    conflict: false,
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
      canEnterZen: editor => Boolean(editor.open),
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

    beginZen(ctx) {
      this.begin(ctx);
      if (this.open) this.toggleZen(ctx, true);
    },

    begin(ctx) {
      const card = ctx.selected;
      if (!card || ctx.busy || ctx.draftBusy || typeof card.body !== "string") return;
      this.open = true;
      this.cardId = card.id || "";
      this.ref = card.ref || "";
      this.workspaceKey = ctx.workspaceKey || "";
      this.contentHash = card.content_hash || "";
      this.text = card.body;
      this.original = card.body;
      const backup = readCardUnsaved(this.workspaceKey, this.cardId);
      if (backup && backup.text !== card.body) {
        this.unsavedOffer = backup.text;
        this.unsavedBase = backup.baseHash;
      } else {
        if (backup) clearCardUnsavedText(this.workspaceKey, this.cardId);
        this.unsavedOffer = "";
        this.unsavedBase = "";
      }
      this.tab = "write";
      this.zen = false;
      this.zenView = "write";
      this.zenMetaOpen = false;
      this.previewHtml = "";
      this.conflict = false;
      this.error = "";
      if (this.surface) this.surface.setValue(this.text);
      ctx.clearStatus();
      ctx.focusInput();
    },

    restoreUnsaved(ctx) {
      if (!this.unsavedOffer) return;
      const base = restoredCardBase(this.unsavedBase, this.contentHash);
      this.text = this.unsavedOffer;
      this.contentHash = base.hash;
      if (base.stale) {
        this.conflict = true;
        this.error = STALE_UNSAVED_MESSAGE;
      }
      this.persist();
      if (this.surface) this.surface.setValue(this.text);
      this.schedulePreview(ctx);
    },

    dismissUnsaved() {
      clearCardUnsavedText(this.workspaceKey, this.cardId);
      this.unsavedOffer = "";
      this.unsavedBase = "";
    },

    persist() {
      if (!this.open) return;
      this.unsavedOffer = "";
      this.unsavedBase = "";
      if (this.text === this.original) {
        clearCardUnsavedText(this.workspaceKey, this.cardId);
      } else {
        writeCardUnsaved(this.workspaceKey, this.cardId, this.text, this.contentHash);
      }
    },

    close(ctx, { force = false, keepUnsaved = false } = {}) {
      if (!this.open) return true;
      if (!force && this.text !== this.original && ctx && typeof ctx.confirm === "function" &&
          !ctx.confirm("Discard unsaved content changes?")) {
        return false;
      }
      if (!keepUnsaved && !this.conflict) {
        clearCardUnsavedText(this.workspaceKey, this.cardId);
      }
      this.destroyZenWorkspace();
      this.open = false;
      this.cardId = "";
      this.ref = "";
      this.workspaceKey = "";
      this.contentHash = "";
      this.text = "";
      this.original = "";
      this.unsavedOffer = "";
      this.unsavedBase = "";
      this.conflict = false;
      this.error = "";
      return true;
    },

    async reloadKeepingText(ctx) {
      if (!this.open || !ctx || typeof ctx.reloadSelected !== "function") return;
      const card = await ctx.reloadSelected();
      if (!card || typeof card.body !== "string") return;
      this.contentHash = card.content_hash || this.contentHash;
      this.original = card.body;
      this.conflict = false;
      this.error = "";
      // The backup now belongs to the reloaded version of the card.
      this.persist();
    },

    preview(ctx) {
      if (typeof renderMarkdown !== "function" || typeof marked === "undefined" || typeof DOMPurify === "undefined") return "";
      return renderMarkdown(ctx.selected, this.text);
    },

    canSave(ctx) {
      if (!this.open || this.saving || ctx.draftBusy || ctx.busy || ctx.attachmentUploading) return false;
      const bodyDirty = this.text !== this.original;
      const propsDirty = Boolean(this.zen && ctx.hasPropertyChanges && ctx.hasPropertyChanges());
      // Property problems (a conflict, an empty title) never block saving the body.
      if (bodyDirty) return true;
      return Boolean(propsDirty && (!ctx.canSaveProperties || ctx.canSaveProperties()));
    },

    async save(ctx) {
      if (!this.canSave(ctx)) return null;
      const bodyDirty = this.text !== this.original;
      const propsDirty = Boolean(this.zen && ctx.hasPropertyChanges && ctx.hasPropertyChanges());
      this.saving = true;
      this.error = "";
      this.conflict = false;
      try {
        let result = null;
        if (bodyDirty) {
          const response = await ctx.saveCardBody(this.ref, this.contentHash, this.text);
          if (!response || !response.ok) {
            if (response && response.conflict) {
              this.conflict = true;
              this.error = "This card changed since you opened it. Copy your text or reload.";
            } else {
              this.error = (response && response.error) || "Could not save card content.";
            }
            return null;
          }
          result = response.payload;
          clearCardUnsavedText(this.workspaceKey, this.cardId);
          // The body is on the card now. A retry after a failed property save
          // must not resend it against the hash it replaced.
          const latest = ctx.getSelected ? ctx.getSelected() : null;
          if (latest && latest.content_hash) this.contentHash = latest.content_hash;
          this.original = this.text;
          // Leave properties that cannot be saved (checked after the body save,
          // which may rebase them) in the panel, where Reset and the warning show.
          if (!propsDirty || (ctx.canSaveProperties && !ctx.canSaveProperties())) {
            this.close(ctx, { force: true });
            return result;
          }
        }
        if (propsDirty && ctx.saveProperties) {
          const propResult = await ctx.saveProperties();
          if (!propResult) {
            this.error = (ctx.propertyError && ctx.propertyError()) || "Could not save card properties.";
            return null;
          }
          result = propResult;
          this.close(ctx, { force: true });
        }
        return result;
      } finally {
        this.saving = false;
      }
    },
  };
}
