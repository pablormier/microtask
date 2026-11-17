// Spotlight-style quick search palette (<dialog> modal + hybrid local/server search).
function createQuickSearchController() {
  let searchTimer = null;
  let announceTimer = null;
  let searchSequence = 0;
  let supportsDisplayTransition;

  function canTransitionDisplay() {
    if (supportsDisplayTransition !== undefined) return supportsDisplayTransition;
    if (typeof window === "undefined" || !window.CSS || typeof window.CSS.supports !== "function"
        || !window.CSS.supports("transition-behavior", "allow-discrete") || !document.body) {
      return false;
    }
    const probe = document.createElement("div");
    probe.style.cssText = "transition: display 1s allow-discrete !important; display: block;";
    document.body.appendChild(probe);
    getComputedStyle(probe).display;
    probe.style.display = "none";
    supportsDisplayTransition = getComputedStyle(probe).display === "block";
    probe.remove();
    return supportsDisplayTransition;
  }

  function supportsTopLayerExit() {
    return typeof window !== "undefined" && Boolean(window.CSS && window.CSS.supports
      && window.CSS.supports("overlay", "auto") && canTransitionDisplay());
  }

  async function closeTopLayerDialog(dialog) {
    if (!dialog || !dialog.open) return;
    if (!supportsTopLayerExit() && typeof dialog.getAnimations === "function") {
      dialog.setAttribute("data-closing", "");
      const animations = dialog.getAnimations({ subtree: true });
      if (animations.length > 0) {
        await Promise.race([
          Promise.allSettled(animations.map(a => a.finished)),
          new Promise(resolve => setTimeout(resolve, 400)),
        ]);
      }
      if (!dialog.hasAttribute("data-closing")) return;
      dialog.removeAttribute("data-closing");
    }
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function installClosedByFallback(dialog, onDismiss) {
    if (!dialog || dialog._mtClosedByInstalled) return;
    dialog._mtClosedByInstalled = true;
    if (typeof HTMLDialogElement !== "undefined" && "closedBy" in HTMLDialogElement.prototype) return;
    const outside = (event) => {
      const rect = dialog.getBoundingClientRect();
      return event.clientX < rect.left || event.clientX > rect.right
        || event.clientY < rect.top || event.clientY > rect.bottom;
    };
    let pressedOutside = false;
    dialog.addEventListener("pointerdown", (event) => { pressedOutside = outside(event); });
    dialog.addEventListener("pointerup", (event) => {
      if (pressedOutside && outside(event)) onDismiss();
      pressedOutside = false;
    });
  }

  function scoreLocalCard(card, terms, rawNeedle) {
    if (!terms.length) return 1;
    if (rawNeedle.startsWith("@")) {
      const prefix = rawNeedle.slice(1).toLowerCase();
      return String(card.ref || "").toLowerCase().startsWith("@" + prefix) ? 4 : 0;
    }
    const title = String(card.title || "").toLowerCase();
    const names = [card.id, card.ref, ...(card.tags || []), card.project || "", card.topic || ""].join(" ").toLowerCase();
    const excerpt = String(card.excerpt || "").toLowerCase();
    let total = 0;
    for (const term of terms) {
      if (title.includes(term)) total += 3;
      else if (names.includes(term)) total += 2;
      else if (excerpt.includes(term)) total += 1;
      else return 0;
    }
    return total;
  }

  return {
    quickSearch: {
      open: false,
      query: "",
      results: [],
      active: 0,
      loading: false,
      announcement: "",
    },

    quickSearchShortcutLabel() {
      const platform = typeof navigator !== "undefined"
        ? (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || ""
        : "";
      return /mac/i.test(platform) ? "⌘K" : "Ctrl+K";
    },

    canOpenQuickSearch() {
      return Boolean(this.ready && !this.captureComposer.open
        && !(this.contentEditor && this.contentEditor.open && this.contentEditor.zen));
    },

    quickSearchActiveDescendant() {
      const item = this.quickSearch.results[this.quickSearch.active];
      return item && this.quickSearch.open ? "mt-qs-opt-" + item.id : null;
    },

    localQuickSearch(queryText) {
      const needle = String(queryText || "").trim();
      if (!needle) return [];
      const terms = needle.toLowerCase().split(/\s+/).filter(Boolean);
      const scored = [];
      for (const card of this.tasks || []) {
        const score = scoreLocalCard(card, terms, needle.toLowerCase());
        if (score > 0) scored.push({ card, score });
      }
      scored.sort((a, b) => b.score - a.score
        || String(b.card.updated || b.card.created || "").localeCompare(String(a.card.updated || a.card.created || "")));
      return scored.slice(0, 12).map(entry => entry.card);
    },

    openQuickSearch() {
      if (!this.canOpenQuickSearch()) return;
      const dialog = this.$refs && this.$refs.quickSearchDialog;
      if (!dialog) return;
      installClosedByFallback(dialog, () => this.closeQuickSearch());
      dialog.removeAttribute("data-closing");
      this.quickSearch.query = "";
      this.quickSearch.active = 0;
      this.quickSearch.results = [];
      this.quickSearch.announcement = "";
      this.quickSearch.loading = false;
      this.quickSearch.open = true;
      if (!dialog.open) {
        if (typeof dialog.showModal === "function") dialog.showModal();
        else dialog.setAttribute("open", "");
      }
      const focusInput = () => {
        const input = this.$refs && this.$refs.quickSearchInput;
        if (input) {
          input.focus();
          input.select();
        }
      };
      if (typeof this.$nextTick === "function") this.$nextTick(focusInput);
      else focusInput();
    },

    clearQuickSearchInput() {
      clearTimeout(searchTimer);
      clearTimeout(announceTimer);
      searchSequence += 1;
      this.quickSearch.query = "";
      this.quickSearch.results = [];
      this.quickSearch.active = 0;
      this.quickSearch.loading = false;
      this.quickSearch.announcement = "";
      const input = this.$refs && this.$refs.quickSearchInput;
      if (input) input.focus();
    },

    closeQuickSearch(immediate = false) {
      const dialog = this.$refs && this.$refs.quickSearchDialog;
      clearTimeout(searchTimer);
      clearTimeout(announceTimer);
      searchSequence += 1;
      this.quickSearch.loading = false;
      if (!dialog || !dialog.open) {
        this.quickSearch.open = false;
        return;
      }
      if (immediate) {
        dialog.removeAttribute("data-closing");
        if (typeof dialog.close === "function") dialog.close();
        else dialog.removeAttribute("open");
        return;
      }
      closeTopLayerDialog(dialog);
    },

    onQuickSearchCancel(event) {
      if (event) event.preventDefault();
      this.closeQuickSearch();
    },

    onQuickSearchClose() {
      clearTimeout(searchTimer);
      clearTimeout(announceTimer);
      searchSequence += 1;
      this.quickSearch.open = false;
      this.quickSearch.loading = false;
      this.quickSearch.announcement = "";
    },

    onQuickSearchInput() {
      const query = String(this.quickSearch.query || "");
      clearTimeout(searchTimer);
      clearTimeout(announceTimer);
      if (!query.trim()) {
        searchSequence += 1;
        this.quickSearch.results = [];
        this.quickSearch.active = 0;
        this.quickSearch.loading = false;
        this.quickSearch.announcement = "";
        return;
      }
      this.quickSearch.results = this.localQuickSearch(query);
      this.quickSearch.active = 0;
      const seq = ++searchSequence;
      this.quickSearch.loading = true;
      searchTimer = setTimeout(() => this.fetchQuickSearch(query, seq), 120);
    },

    async fetchQuickSearch(query, seq = ++searchSequence) {
      this.quickSearch.loading = true;
      try {
        const params = new URLSearchParams({ q: String(query || "").trim(), limit: "12" });
        const response = await fetch("/api/search?" + params, { cache: "no-store" });
        if (seq !== searchSequence || !this.quickSearch.open) return;
        if (!response.ok) return;
        const payload = await response.json();
        if (seq !== searchSequence || !this.quickSearch.open) return;
        this.quickSearch.results = Array.isArray(payload.items) ? payload.items : [];
        this.quickSearch.active = 0;
        clearTimeout(announceTimer);
        announceTimer = setTimeout(() => {
          if (!this.quickSearch.open) return;
          const count = this.quickSearch.results.length;
          this.quickSearch.announcement = count === 0
            ? "No matching cards"
            : count + (count === 1 ? " match" : " matches");
        }, 250);
      } catch (_) {
        // Keep local matches if the server request fails.
      } finally {
        if (seq === searchSequence) this.quickSearch.loading = false;
      }
    },

    moveQuickSearch(step) {
      const count = this.quickSearch.results.length;
      if (!count) return;
      this.quickSearch.active = (this.quickSearch.active + step + count) % count;
      const scrollActive = () => {
        const list = this.$refs && this.$refs.quickSearchList;
        const item = this.quickSearch.results[this.quickSearch.active];
        if (!list || !item) return;
        const row = document.getElementById("mt-qs-opt-" + item.id);
        if (row && typeof row.scrollIntoView === "function") row.scrollIntoView({ block: "nearest" });
      };
      if (typeof this.$nextTick === "function") this.$nextTick(scrollActive);
      else scrollActive();
    },

    chooseQuickSearch() {
      const item = this.quickSearch.results[this.quickSearch.active];
      if (!item) return;
      this.selectQuickSearch(item);
    },

    selectQuickSearch(item, event) {
      if (!item) return;
      this.closeQuickSearch(true);
      this.select(item, event);
    },

    handleQuickSearchShortcut(event) {
      if (!event || event.defaultPrevented) return;
      const key = String(event.key || "").toLowerCase();
      const isCmdK = (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && key === "k";
      const target = event.target;
      const inEditable = Boolean(target && (
        target.isContentEditable
        || /^(INPUT|TEXTAREA|SELECT)$/i.test(target.tagName || "")
      ));
      const isSlash = !inEditable && !event.metaKey && !event.ctrlKey && !event.altKey && event.key === "/";
      if (!isCmdK && !isSlash) return;
      if (!this.canOpenQuickSearch()) return;
      event.preventDefault();
      if (this.quickSearch.open) this.closeQuickSearch();
      else this.openQuickSearch();
    },
  };
}
