// Reusable @ref + keyword card autocomplete for plain <textarea> note/task editors.
function findMentionTrigger(text, caretPos) {
  const value = String(text || "");
  const pos = typeof caretPos === "number" ? Math.max(0, Math.min(caretPos, value.length)) : value.length;
  const before = value.slice(0, pos);
  const match = before.match(/(?:^|[^\w@])@([a-zA-Z0-9_./:-]*(?:\s+[a-zA-Z0-9_./:-]+){0,2}\s?)$/);
  if (!match) return null;
  const query = match[1];
  const start = pos - query.length - 1;
  return { query, start, end: pos };
}

function matchMentionCards(cards, query, limit = 8) {
  const list = Array.isArray(cards) ? cards : [];
  const raw = String(query || "").trim().toLowerCase();
  const tokens = raw ? raw.split(/\s+/).filter(Boolean) : [];
  const cap = Math.max(1, Number(limit) || 8);

  if (!tokens.length) {
    return list.filter(card => card && card.ref).slice(0, cap);
  }

  const scored = [];
  for (const card of list) {
    if (!card || !card.ref) continue;
    const ref = String(card.ref || "").toLowerCase();
    const refBare = ref.startsWith("@") ? ref.slice(1) : ref;
    const title = String(card.title || "").toLowerCase();
    const project = String(card.project || "").toLowerCase();
    const topic = String(card.topic || "").toLowerCase();
    const tags = Array.isArray(card.tags) ? card.tags.map(t => String(t).toLowerCase()).join(" ") : "";
    const haystack = [refBare, title, project, topic, tags].join(" ");

    if (!tokens.every(token => haystack.includes(token))) continue;

    let score = 0;
    const joined = tokens.join(" ");
    const joinedBare = joined.startsWith("@") ? joined.slice(1) : joined;
    if (refBare === joinedBare) score += 100;
    else if (refBare.startsWith(joinedBare)) score += 80;
    else if (title.startsWith(joined)) score += 60;
    else if (title.includes(joined)) score += 40;
    else score += 20;

    if (!card.archived) score += 5;
    scored.push({ card, score });
  }

  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, cap).map(entry => entry.card);
}

const MENTION_MIRROR_PROPS = [
  "boxSizing", "width", "paddingTop", "paddingRight", "paddingBottom", "paddingLeft",
  "borderTopWidth", "borderRightWidth", "borderBottomWidth", "borderLeftWidth",
  "borderTopStyle", "borderRightStyle", "borderBottomStyle", "borderLeftStyle",
  "fontFamily", "fontSize", "fontWeight", "fontStyle", "fontVariant", "lineHeight",
  "letterSpacing", "wordSpacing", "textIndent", "textTransform", "tabSize",
];

function computeTextareaCaretPos(textarea, charIndex) {
  const fallback = { top: 0, left: 0, placeAbove: false };
  if (!textarea || typeof textarea.getBoundingClientRect !== "function" ||
      typeof document === "undefined" || typeof document.createElement !== "function" ||
      typeof window === "undefined" || typeof window.getComputedStyle !== "function") {
    return fallback;
  }
  const rect = textarea.getBoundingClientRect();
  if (!rect || (!rect.width && !rect.height)) return fallback;

  const style = window.getComputedStyle(textarea);
  const mirror = document.createElement("div");
  for (const prop of MENTION_MIRROR_PROPS) {
    mirror.style[prop] = style[prop];
  }
  mirror.style.position = "fixed";
  mirror.style.top = "0";
  mirror.style.left = "-9999px";
  mirror.style.visibility = "hidden";
  mirror.style.pointerEvents = "none";
  mirror.style.whiteSpace = "pre-wrap";
  mirror.style.overflowWrap = "break-word";
  mirror.style.wordWrap = "break-word";
  mirror.style.overflow = "hidden";
  mirror.style.width = rect.width + "px";

  const value = String(textarea.value || "");
  const index = Math.max(0, Math.min(Number(charIndex) || 0, value.length));
  mirror.textContent = value.slice(0, index);
  const marker = document.createElement("span");
  marker.textContent = value.slice(index, index + 1) || "@";
  mirror.appendChild(marker);

  document.body.appendChild(mirror);
  const markerLeft = marker.offsetLeft;
  const markerTop = marker.offsetTop;
  const lineHeight = marker.offsetHeight || parseFloat(style.lineHeight) || (parseFloat(style.fontSize) || 14) * 1.45;
  document.body.removeChild(mirror);

  const scrollTop = Number(textarea.scrollTop) || 0;
  const scrollLeft = Number(textarea.scrollLeft) || 0;
  const rawLeft = rect.left + markerLeft - scrollLeft;
  const unclampedTop = rect.top + markerTop - scrollTop;
  const minTop = rect.top + 4;
  const maxTop = Math.max(minTop, rect.bottom - lineHeight - 4);
  const rawTop = Math.max(minTop, Math.min(unclampedTop, maxTop));

  const menuWidth = 384;
  const winWidth = window.innerWidth || 1024;
  const winHeight = window.innerHeight || 768;

  let left = Math.round(rawLeft);
  if (left + menuWidth > winWidth - 12) {
    left = Math.max(12, winWidth - menuWidth - 12);
  }
  if (left < 12) left = 12;

  const spaceBelow = winHeight - (rawTop + lineHeight);
  const placeAbove = spaceBelow < 220 && rawTop > spaceBelow;

  return {
    top: placeAbove ? Math.round(rawTop - 4) : Math.round(rawTop + lineHeight + 4),
    left,
    placeAbove,
  };
}

function createMentionAutocomplete() {
  return {
    open: false,
    query: "",
    start: 0,
    end: 0,
    items: [],
    activeIndex: 0,
    pos: { top: 0, left: 0, placeAbove: false },

    close() {
      this.open = false;
      this.query = "";
      this.start = 0;
      this.end = 0;
      this.items = [];
      this.activeIndex = 0;
    },

    reposition(textarea) {
      if (!this.open || !textarea) return;
      this.pos = computeTextareaCaretPos(textarea, this.start);
    },

    update(textarea, cards) {
      if (!textarea || typeof textarea.value !== "string") {
        this.close();
        return;
      }
      const caret = typeof textarea.selectionStart === "number" ? textarea.selectionStart : textarea.value.length;
      const trigger = findMentionTrigger(textarea.value, caret);
      if (!trigger) {
        this.close();
        return;
      }
      const items = matchMentionCards(cards, trigger.query, 8);
      const queryText = trigger.query.trim();
      const canRemoteSearch = typeof fetch === "function" && queryText.length > 0;
      if (!items.length && !canRemoteSearch) {
        this.close();
        return;
      }
      const triggerChanged = !this.open || this.start !== trigger.start;
      this.open = items.length > 0;
      this.query = trigger.query;
      this.start = trigger.start;
      this.end = trigger.end;
      this.items = items;
      if (this.activeIndex >= items.length) this.activeIndex = 0;
      if (this.open && (triggerChanged || (!this.pos.top && !this.pos.left))) {
        this.pos = computeTextareaCaretPos(textarea, trigger.start);
      }
      if (canRemoteSearch) {
        const seq = (this._searchSeq = (this._searchSeq || 0) + 1);
        clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(async () => {
          try {
            const response = await fetch("/api/search?q=" + encodeURIComponent(queryText) + "&limit=8", { cache: "no-store" });
            if (!response.ok || seq !== this._searchSeq) return;
            const payload = await response.json();
            if (seq !== this._searchSeq || this.query !== trigger.query) return;
            const remote = Array.isArray(payload.results) ? payload.results.slice(0, 8) : [];
            if (!remote.length && !this.items.length) {
              this.close();
              return;
            }
            if (remote.length) {
              this.items = remote;
              this.open = true;
              if (this.activeIndex >= remote.length) this.activeIndex = 0;
              this.pos = computeTextareaCaretPos(textarea, trigger.start);
            }
          } catch (_) {}
        }, 120);
      }
    },

    move(delta) {
      if (!this.open || !this.items.length) return;
      const len = this.items.length;
      this.activeIndex = ((this.activeIndex + delta) % len + len) % len;
    },

    apply(textarea, item, onUpdate) {
      if (!textarea || !item || !item.ref) {
        this.close();
        return;
      }
      const value = String(textarea.value || "");
      const start = Math.max(0, Math.min(this.start, value.length));
      const end = Math.max(start, Math.min(this.end, value.length));
      const inserted = String(item.ref) + " ";
      const nextValue = value.slice(0, start) + inserted + value.slice(end);
      const nextCaret = start + inserted.length;

      textarea.value = nextValue;
      if (typeof onUpdate === "function") onUpdate(nextValue);
      if (typeof textarea.setSelectionRange === "function") {
        textarea.setSelectionRange(nextCaret, nextCaret);
      }
      if (typeof textarea.focus === "function") textarea.focus();
      this.close();
    },

    handleKeydown(event, textarea, onUpdate) {
      if (!this.open || !this.items.length || !event) return false;
      if (event.isComposing || event.keyCode === 229) return false;
      if (textarea && typeof textarea.value === "string") {
        const caret = typeof textarea.selectionStart === "number" ? textarea.selectionStart : textarea.value.length;
        const trigger = findMentionTrigger(textarea.value, caret);
        if (!trigger || trigger.start !== this.start || trigger.end !== this.end) {
          this.close();
          return false;
        }
      }
      if (event.key === "ArrowDown") {
        event.preventDefault();
        this.move(1);
        return true;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        this.move(-1);
        return true;
      }
      if ((event.key === "Enter" && !event.metaKey && !event.ctrlKey && !event.shiftKey) || event.key === "Tab") {
        event.preventDefault();
        if (typeof event.stopPropagation === "function") event.stopPropagation();
        this.apply(textarea, this.items[this.activeIndex] || this.items[0], onUpdate);
        return true;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        if (typeof event.stopPropagation === "function") event.stopPropagation();
        this.close();
        return true;
      }
      return false;
    },
  };
}
