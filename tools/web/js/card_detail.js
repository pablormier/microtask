// Card detail selection, history navigation, preview rendering, hover popovers, and dialog focus trapping.
function createDetailController() {
  return {
    trapComposerFocus(event) {
      this.trapDialogFocus(event, this.$refs.composerDialog);
    },

    trapDetailFocus(event) {
      this.trapDialogFocus(event, this.$refs.detailDialog);
    },

    trapDialogFocus(event, dialog) {
      if (!dialog || !dialog.querySelectorAll) return;
      const focusable = Array.from(dialog.querySelectorAll(
        ':is(a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), textarea:not([disabled]), select:not([disabled]), summary, [tabindex]):not([tabindex="-1"])'
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
      this.commentsHtml = renderCommentsHtml(this.selected);
      this.timeEntriesHtml = renderTimeEntriesHtml(this.selected);
      if (this.activityTab === "time" && !this.hasTimeActivityTab()) {
        this.activityTab = "comments";
      }
      this.textPropertyHtml = {
        next_action: renderTextProperty(this.selected, "next_action"),
        processing_instructions: renderTextProperty(this.selected, "processing_instructions"),
      };
    },

    hasTimeActivityTab() {
      const card = this.selected;
      return Boolean(card && (card.kind === "task" || (Array.isArray(card.time_entries) && card.time_entries.length)));
    },

    activityTabs() {
      const card = this.selected;
      if (!card) return [];
      const commentCount = Array.isArray(card.comments) ? card.comments.length : 0;
      const tabs = [{ id: "comments", label: "Comments", count: commentCount }];
      if (this.hasTimeActivityTab()) {
        const timeCount = Array.isArray(card.time_entries) ? card.time_entries.length : 0;
        tabs.push({ id: "time", label: "Time", count: timeCount });
      }
      return tabs;
    },

    selectActivityTab(tabId, focusTab = false) {
      if (tabId === "time" && !this.hasTimeActivityTab()) tabId = "comments";
      this.activityTab = tabId;
      if (focusTab) {
        this.$nextTick(() => {
          const dialog = this.$refs && this.$refs.detailDialog;
          const tab = dialog && dialog.querySelector("#mt-activity-tab-" + tabId);
          if (tab && typeof tab.focus === "function") tab.focus();
        });
      }
    },

    openTimeActivity() {
      if (!this.hasTimeActivityTab()) return;
      this.selectActivityTab("time");
      this.$nextTick(() => {
        const section = this.$refs && this.$refs.activitySection;
        if (section && typeof section.scrollIntoView === "function") {
          section.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
      });
    },

    handleActivityTabKeydown(event, index) {
      const tabs = this.activityTabs();
      if (tabs.length < 2) return;
      let nextIndex = -1;
      if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
      else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
      else if (event.key === "Home") nextIndex = 0;
      else if (event.key === "End") nextIndex = tabs.length - 1;
      if (nextIndex === -1) return;
      event.preventDefault();
      this.selectActivityTab(tabs[nextIndex].id, true);
    },

    // Next action and processing instructions live in one place: rendered in the content
    // column, edited there in place, and saved through the shared property editor.
    textPropertyFields() {
      const card = this.selected;
      if (!card) return [];
      const fields = [];
      if (card.kind === "task") fields.push({ key: "next_action", label: "Next action", empty: "No next action yet.", placeholder: "The next concrete step" });
      fields.push({ key: "processing_instructions", label: "Processing instructions", empty: "None.", placeholder: "How should this be organized or triaged?" });
      return fields
        .map(field => ({ ...field, value: String(card[field.key] || ""), html: (this.textPropertyHtml || {})[field.key] || "" }))
        .filter(field => field.key === "next_action" || field.value || this.textPropertyEditing === field.key);
    },

    editTextProperty(key) {
      if (!this.propertyEditor.form) return;
      this.textPropertyEditing = key;
    },

    cancelTextProperty(key) {
      if (this.propertyEditor.form && this.selected) this.propertyEditor.form[key] = this.selected[key] || "";
      this.textPropertyEditing = "";
    },

    async saveTextProperty() {
      const result = await this.propertyEditor.apply(this.propertyContext());
      if (result) this.textPropertyEditing = "";
      return result;
    },

    async toggleTaskCheckbox(event) {
      const box = event.target.closest && event.target.closest("input.mt-task-checkbox");
      if (!box || !event.currentTarget.contains(box)) return;
      // The browser toggles the box on click; keep it as rendered until the server confirms.
      event.preventDefault();
      if (!this.selected || this.busy || this.draftBusy || this.contentEditor.open) return;
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
        this.hoverDisplayCard = card;
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
      const propDirty = Boolean(this.selected && this.propertyEditor.changed());
      const bodyDirty = Boolean(this.contentEditor.open && this.contentEditor.text !== this.contentEditor.original);
      if (propDirty || bodyDirty) {
        if (typeof window !== "undefined" && typeof window.confirm === "function" &&
            !window.confirm("Discard unsaved changes?")) return false;
        if (propDirty) this.propertyEditor.discard(this.selected, this.taskDefaults);
      }
      this.contentEditor.close(this.contentContext(), { force: true });
      return true;
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

    // Focus Back when there is a previous card, else Close. Back renders only in that case and
    // can appear shortly after the card does, so check again briefly before using Close.
    focusDetailNavigation(triesLeft) {
      const dialog = this.$refs && this.$refs.detailDialog;
      if (!dialog) return;
      const back = dialog.querySelector(".mt-detail-back");
      const backShown = Boolean(back && back.getClientRects().length);
      if (this.canGoBackReference() && !backShown && triesLeft > 0) {
        setTimeout(() => this.focusDetailNavigation(triesLeft - 1), 30);
        return;
      }
      const target = this.canGoBackReference() && backShown ? back : dialog.querySelector(".mt-close");
      if (target) target.focus();
    },

    canGoBackReference() {
      return Boolean(this.previousCardId && this.tasks.some(task => task.id === this.previousCardId));
    },

    backReference() {
      if (this.canGoBackReference() && this.canLeaveCard()) history.back();
    },

    showCard(task) {
      this.contentEditor.close(this.contentContext(), { force: true });
      this.clearHoverCard();
      if (!this.detailReturnFocus) this.detailReturnFocus = document.activeElement;
      this.selectedId = task.id;
      this.selected = task;
      this.moveDestination = task.status || "";
      this.propertyEditor.workspaceKey = this.workspaceKey;
      this.propertyEditor.begin(task, this.taskDefaults);
      this.propertyEditor.offerBackup(task);
      this.commentText = "";
      if (this.commentMention) this.commentMention.closeMention();
      this.actionMessage = "";
      this.actionError = false;
      this.previewHtml = "";
      this.commentsHtml = "";
      this.timeEntriesHtml = "";
      this.activityTab = "comments";
      this.textPropertyHtml = {};
      this.textPropertyEditing = "";
      this.detailError = "";
      this.detailLoading = false;
      this.detailSequence += 1;
      this.$nextTick(() => {
        if (typeof window !== "undefined" && typeof window.scrollTo === "function") window.scrollTo(0, 0);
        this.focusDetailNavigation(10);
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
      this.commentsHtml = "";
      this.timeEntriesHtml = "";
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

    // Writes the open property form and body editor to browser storage under the current workspace key,
    // so that a page restart for another signed-in person loses nothing.
    keepEditsForReload() {
      this.propertyEditor.persist(this.workspaceKey, this.selected && this.selected.id);
      this.contentEditor.persist();
    },

    clearSelection(updateUrl = true) {
      if (!this.canLeaveCard()) return;
      this.contentEditor.close(this.contentContext(), { force: true });
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
      this.commentsHtml = "";
      this.timeEntriesHtml = "";
      this.activityTab = "comments";
      this.moveDestination = "";
      this.propertyEditor.form = null;
      this.propertyEditor.baseline = "";
      this.propertyEditor.baselineHash = "";
      this.propertyEditor.conflict = false;
      this.commentText = "";
      if (this.commentMention) this.commentMention.closeMention();
      this.actionMessage = "";
      this.actionError = false;
      this.$nextTick(() => {
        if (returnFocus && returnFocus.isConnected && typeof returnFocus.focus === "function") returnFocus.focus();
      });
    }
  };
}
