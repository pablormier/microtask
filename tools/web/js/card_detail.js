// Card detail selection, history navigation, preview rendering, hover popovers, and dialog focus trapping.
function createDetailController() {
  return {
    trapComposerFocus(event) {
      this.trapDialogFocus(event, this.$refs.composerDialog);
    },

    trapTaskDraftFocus(event) {
      this.trapDialogFocus(event, this.$refs.taskDraftDialog);
    },

    trapDetailFocus(event) {
      this.trapDialogFocus(event, this.$refs.detailDialog);
    },

    trapDialogFocus(event, dialog) {
      if (!dialog || !dialog.querySelectorAll) return;
      const focusable = Array.from(dialog.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])'
      )).filter(element => element.getClientRects().length
        && getComputedStyle(element).visibility !== "hidden"
        && !element.closest("[inert]")
        && !element.closest('[aria-hidden="true"]'));
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    },



    renderSelectedPreview() {
      if (!this.selected || typeof this.selected.body !== "string" || typeof document.createElement !== "function") return;
      const preview = document.createElement("div");
      renderPreview(this.selected, preview);
      enableTaskCheckboxes(preview);
      this.previewHtml = preview.innerHTML;
      const properties = document.createElement("div");
      renderPropertyPanels(this.selected, properties);
      this.propertiesHtml = properties.innerHTML;
    },

    async toggleTaskCheckbox(event) {
      const box = event.target.closest && event.target.closest("input.mt-task-checkbox");
      if (!box || !event.currentTarget.contains(box)) return;
      // The browser toggles the box on click; keep it as rendered until the server confirms.
      event.preventDefault();
      if (!this.selected || this.busy || this.draftBusy || this.contentEditor.draft) return;
      const index = Number(box.getAttribute("data-mt-task-index"));
      const count = event.currentTarget.querySelectorAll("input.mt-task-checkbox").length;
      return this.saveBodyKeepingProperties(() => this.apiPost("/api/checkbox", {
        ref: this.selected.ref,
        expected_hash: this.selected.content_hash,
        index,
        count,
        // The checked attribute is the rendered state; box.checked is already flipped by the click.
        // The server refuses if its marker is not in the opposite state.
        checked: !box.hasAttribute("checked"),
      }));
    },

    openReference(event) {
      const link = event.target.closest('a[href^="#mt-card-"]');
      if (!link || !event.currentTarget.contains(link)) return;
      if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey ||
          event.shiftKey || event.altKey || link.target === "_blank") return;
      event.preventDefault();
      this.clearHoverCard();
      let id;
      try { id = decodeURIComponent(link.getAttribute("href").slice("#mt-card-".length)); }
      catch (error) { return; }
      // A referenced card may be outside the filtered list; its summary comes with the open card.
      const target = this.tasks.find(task => task.id === id) ||
        (this.selected && (this.selected.references || []).find(r => r.target && r.target.id === id)?.target);
      if (target) this.select(target);
    },

    onReferenceHover(event) {
      const link = event.target.closest('a[href^="#mt-card-"]');
      if (!link) return;
      if (this.hoverTarget === link && this.hoverCard) return;

      this.cancelHoverTimer();
      this.hoverTimer = setTimeout(() => {
        let id;
        try { id = decodeURIComponent(link.getAttribute("href").slice("#mt-card-".length)); }
        catch (_) { return; }
        const card = this.tasks.find(task => task.id === id) ||
          (this.selected && (this.selected.references || []).find(r => r.target && r.target.id === id)?.target);
        if (!card) return;

        this.hoverTarget = link;
        this.hoverPos = this.computeHoverPos(link);
        this.hoverCard = card;
      }, 150);
    },

    onReferenceLeave(event) {
      const link = event.target.closest('a[href^="#mt-card-"]');
      if (!link) return;
      if (event.relatedTarget && link.contains(event.relatedTarget)) return;

      const popover = typeof document !== "undefined" && typeof document.querySelector === "function"
        ? document.querySelector(".mt-card-popover") : null;
      if (popover && event.relatedTarget && popover.contains(event.relatedTarget)) return;

      if (event.relatedTarget && event.relatedTarget.closest && event.relatedTarget.closest('a[href^="#mt-card-"]')) {
        return;
      }

      this.scheduleClearHoverCard();
    },

    keepHoverCard() {
      this.cancelHoverTimer();
    },

    scheduleClearHoverCard() {
      this.cancelHoverTimer();
      this.hoverTimer = setTimeout(() => {
        this.clearHoverCard();
      }, 120);
    },

    cancelHoverTimer() {
      if (this.hoverTimer) {
        clearTimeout(this.hoverTimer);
        this.hoverTimer = null;
      }
    },

    clearHoverCard() {
      this.cancelHoverTimer();
      this.hoverCard = null;
      this.hoverTarget = null;
    },

    computeHoverPos(link) {
      const rect = link.getBoundingClientRect();
      const popoverWidth = 328;
      const winWidth = typeof window !== "undefined" && window.innerWidth ? window.innerWidth : 1024;
      const winHeight = typeof window !== "undefined" && window.innerHeight ? window.innerHeight : 768;

      let left = rect.left;
      if (left + popoverWidth > winWidth - 12) {
        left = Math.max(12, winWidth - popoverWidth - 12);
      }
      if (left < 12) left = 12;

      const spaceBelow = winHeight - rect.bottom;
      const placeAbove = spaceBelow < 190 && rect.top > spaceBelow;

      return {
        top: placeAbove ? Math.round(rect.top - 6) : Math.round(rect.bottom + 6),
        left: Math.round(left),
        placeAbove,
      };
    },

    // ---------------------------------------------------------------------------
    // Card Selection & Browser History Navigation
    // ---------------------------------------------------------------------------

    changeSelectedStatus() {
      if (this.moveDestination === "done") this.doneSelected();
      else this.moveSelected();
    },

    canLeaveCard() {
      if (this.busy || this.draftBusy) return false;
      if (this.selected && this.propertyEditor.changed()) {
        if (typeof window !== "undefined" && typeof window.confirm === "function" &&
            !window.confirm("Discard unsaved property changes? Content edits are kept as a draft.")) return false;
        this.propertyEditor.begin(this.selected, this.taskDefaults);
      }
      return this.contentEditor.close(this.contentContext()) !== false;
    },

    select(task, event) {
      if (this.selected && this.selected.id !== task.id && !this.canLeaveCard()) return;
      if (!this.selected) {
        this.detailReturnFocus = event && event.currentTarget ? event.currentTarget : document.activeElement;
      }
      if (!this.tasks.some(item => item.id === task.id)) this.tasks = [...this.tasks, task];
      if (this.selectedId !== task.id) {
        history.pushState({ ...history.state, mtCard: {
          workspace: this.workspaceKey, id: task.id, previous: this.selectedId,
        } }, "", referenceHref(task));
      }
      return this.readCardLocation();
    },

    readCardLocation() {
      if (!this.ready) return;
      const id = this.cardIdInLocation();
      const target = this.tasks.find(task => task.id === id);
      if (id && !target) this.requestCard(id);
      if (this.selected && this.selectedId !== (target && target.id) && !this.canLeaveCard()) {
        history.replaceState({ ...history.state, mtCard: {
          workspace: this.workspaceKey, id: this.selectedId, previous: this.previousCardId,
        } }, "", referenceHref(this.selected));
        return;
      }
      let navigation = history.state && history.state.mtCard;
      if (!navigation || navigation.workspace !== this.workspaceKey || navigation.id !== id) {
        // Direct links and manually edited fragments start a fresh navigation trail.
        navigation = { workspace: this.workspaceKey, id, previous: null };
        history.replaceState({ ...history.state, mtCard: navigation }, "");
      }
      this.previousCardId = target ? navigation.previous : null;
      if (target && this.selectedId !== target.id) return this.showCard(target);
      else if (!target && this.selected) this.clearSelection(false);
    },

    // A linked card outside the filtered list: ask for it, then read the location again if it exists.
    requestCard(id) {
      if (this.requestedCardId === id) return;
      this.requestedCardId = id;
      this.refreshItems().then(() => {
        if (this.cardIdInLocation() === id && this.tasks.some(task => task.id === id)) this.readCardLocation();
      }).catch(() => {}).finally(() => { if (this.requestedCardId === id) this.requestedCardId = null; });
    },

    canGoBackReference() {
      return Boolean(this.previousCardId && this.tasks.some(task => task.id === this.previousCardId));
    },

    backReference() {
      if (this.canGoBackReference() && this.canLeaveCard()) history.back();
    },

    showCard(task) {
      this.contentEditor.close(this.contentContext());
      this.clearHoverCard();
      if (!this.detailReturnFocus) this.detailReturnFocus = document.activeElement;
      this.selectedId = task.id;
      this.selected = task;
      this.moveDestination = task.status || "";
      this.propertyEditor.begin(task, this.taskDefaults);
      this.commentText = "";
      this.actionMessage = "";
      this.actionError = false;
      this.previewHtml = "";
      this.propertiesHtml = "";
      this.detailError = "";
      this.detailLoading = false;
      this.detailSequence += 1;
      this.$nextTick(() => {
        if (typeof window !== "undefined" && typeof window.scrollTo === "function") window.scrollTo(0, 0);
        const dialog = this.$refs.detailDialog;
        const navigationButton = dialog && dialog.querySelector(this.canGoBackReference() ? ".mt-detail-back" : ".mt-close");
        if (navigationButton) navigationButton.focus();
      });
      if (typeof task.body === "string") this.renderSelectedPreview();
      else return this.loadSelectedDetails();
    },

    async loadSelectedDetails(preserveError = false) {
      if (!this.selectedId) return;
      const identity = this.selectedId;
      const sequence = ++this.detailSequence;
      this.detailLoading = true;
      if (!preserveError) this.detailError = "";
      this.previewHtml = "";
      this.propertiesHtml = "";
      try {
        const response = await fetch("/api/item?id=" + encodeURIComponent(identity), { cache: "no-store" });
        const payload = await response.json();
        if (sequence !== this.detailSequence || this.selectedId !== identity) return;
        if (!response.ok) throw new Error(payload.error || "Could not load card content.");
        const item = payload.item;
        if (!item || item.id !== identity || typeof item.body !== "string") {
          throw new Error("The server returned incomplete card content. Retry loading.");
        }
        const propertyChanges = this.propertyEditor.changed();
        if (this.propertyEditor.baselineHash !== item.content_hash && propertyChanges) {
          this.propertyEditor.conflict = true;
        }
        if (!propertyChanges) this.propertyEditor.begin(item, this.taskDefaults);
        else if (!this.propertyEditor.hydrated) this.propertyEditor.hydrate(item, this.taskDefaults);
        this.selected = item;
        this.moveDestination = item.status || "";
        this.detailError = "";
        this.renderSelectedPreview();
        return item;
      } catch (error) {
        if (sequence === this.detailSequence && this.selectedId === identity) {
          this.detailError = error.message || "Could not load card content.";
        }
      } finally {
        if (sequence === this.detailSequence) this.detailLoading = false;
      }
    },

    clearSelection(updateUrl = true) {
      if (!this.canLeaveCard()) return;
      this.contentEditor.close(this.contentContext());
      this.clearHoverCard();
      if (updateUrl) {
        history.replaceState({ ...history.state, mtCard: {
          workspace: this.workspaceKey, id: null, previous: null,
        } }, "", location.pathname + location.search);
      }
      const returnFocus = this.detailReturnFocus;
      this.detailSequence += 1;
      this.detailLoading = false;
      this.detailError = "";
      this.selectedId = null;
      this.selected = null;
      this.previousCardId = null;
      this.detailReturnFocus = null;
      this.previewHtml = "";
      this.moveDestination = "";
      this.propertyEditor.form = null;
      this.propertyEditor.baseline = "";
      this.propertyEditor.baselineHash = "";
      this.propertyEditor.conflict = false;
      this.commentText = "";
      this.actionMessage = "";
      this.actionError = false;
      this.$nextTick(() => {
        if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === "function") returnFocus.focus();
      });
    }
  };
}
