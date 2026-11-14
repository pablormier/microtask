function resolveUrl(base, href) {
  if (href == null) return href;
  href = String(href);
  if (href === "" || href.startsWith("#")) return href;
  if (/^[a-z][a-z0-9+.-]*:/i.test(href)) return href;
  if (href.startsWith("//")) return href;
  try { return new URL(href, base).href; } catch (e) { return href; }
}

function referenceHref(target) {
  return "#mt-card-" + encodeURIComponent(target.id).replace(/[!'()*]/g,
    char => "%" + char.charCodeAt(0).toString(16));
}

function referenceSource(task, field, value, spanList) {
  // Python offsets count Unicode characters; Array.from does the same in JS.
  let source = Array.from(value == null ? (task[field] || "") : value);
  const spans = spanList || (task.reference_spans || {})[field] || [];
  // Offsets come from the shared core scanner. Work backwards to preserve them.
  for (const occurrence of [...spans].reverse()) {
    if (occurrence.state !== "resolved") continue;
    source = source.slice(0, occurrence.start).concat(
      "[" + source.slice(occurrence.start, occurrence.end).join("") + "](" + referenceHref(occurrence.target) + ")",
      source.slice(occurrence.end));
  }
  return source.join("");
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function protectMathSpans(source) {
  const mathList = [];
  const pattern = /(?:^|\n)(`{3,}|~{3,})[^\n]*\n[\s\S]*?\n\1(?=\r?\n|$)|(`+)(?:[^\r\n`]|(?!\2)`)*?\2|(\\\[[\s\S]*?\\\]|\$\$[\s\S]*?\$\$)|(\\\([\s\S]*?\\\)|\$(?!\s|\$)((?:\\\$|[^\$\r\n])+?)(?<!\s|\$)\$)/g;

  const protectedSource = String(source || "").replace(pattern, (match, fence, inlineCode, displayMath, inlineMath) => {
    if (fence || inlineCode) return match;
    const math = displayMath || inlineMath;
    if (math) {
      const index = mathList.length;
      mathList.push(math);
      return "@@MTMATH" + index + "@@";
    }
    return match;
  });
  return { protectedSource, mathList };
}

function restoreMathSpans(html, mathList) {
  if (!mathList || !mathList.length) return html;
  return String(html || "").replace(/@@MTMATH(\d+)@@/g, (match, indexStr) => {
    const index = parseInt(indexStr, 10);
    const math = mathList[index];
    return math != null ? escapeHtml(math) : match;
  });
}

function renderMarkdown(task, source) {
  const renderer = new marked.Renderer();
  const origLink = renderer.link.bind(renderer);
  renderer.link = function (...args) {
    const token = args[0];
    if (token && typeof token === "object" && "href" in token) {
      const isCardRef = (token.href && token.href.startsWith("#mt-card-")) ||
        (task.references || []).some(ref => ref.state === "resolved" &&
          token.href === referenceHref(ref.target));
      args[0] = Object.assign({}, token, {
        href: resolveUrl(task.base_uri || "", token.href),
        title: isCardRef ? undefined : token.title,
      });
    } else if (typeof token === "string") {
      const isCardRef = token.startsWith("#mt-card-") ||
        (task.references || []).some(ref => ref.state === "resolved" &&
          token === referenceHref(ref.target));
      args[0] = resolveUrl(task.base_uri || "", token);
      if (isCardRef) args[1] = undefined;
    }
    return origLink(...args);
  };
  const origImage = renderer.image.bind(renderer);
  renderer.image = function (...args) {
    const token = args[0];
    if (token && typeof token === "object" && "href" in token) {
      args[0] = Object.assign({}, token, { href: resolveUrl(task.base_uri || "", token.href) });
    } else if (typeof token === "string") {
      args[0] = resolveUrl(task.base_uri || "", token);
    }
    return origImage(...args);
  };
  const protectedMath = protectMathSpans(source || "");
  const raw = marked.parse(protectedMath.protectedSource, { gfm: true, breaks: false, renderer });
  const restored = restoreMathSpans(raw, protectedMath.mathList);
  const sanitized = DOMPurify.sanitize(restored, { USE_PROFILES: { html: true }, ADD_ATTR: ["target"] });
  if (typeof window !== "undefined" && window.renderMathInElement &&
      typeof document !== "undefined" && typeof document.createElement === "function") {
    const container = document.createElement("div");
    if (typeof Element !== "undefined" && "setHTML" in Element.prototype) {
      container.setHTML(sanitized);
    } else {
      container.innerHTML = sanitized;
    }
    window.renderMathInElement(container, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$", right: "$", display: false }
      ],
      throwOnError: false,
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"]
    });
    return container.innerHTML;
  }
  return sanitized;
}

function renderPreview(task, el) {
  if (!window.marked || !window.DOMPurify) return;
  const source = referenceSource(task, "body");
  el.innerHTML = renderMarkdown(task, source);
}

// Dashboard body preview only: make task-list checkboxes clickable, numbered in document order.
function enableTaskCheckboxes(el) {
  if (!el || typeof el.querySelectorAll !== "function") return 0;
  const boxes = Array.from(el.querySelectorAll('li input[type="checkbox"]'));
  boxes.forEach((box, index) => {
    box.removeAttribute("disabled");
    box.classList.add("mt-task-checkbox");
    box.setAttribute("data-mt-task-index", String(index));
  });
  return boxes.length;
}

function renderPropertyPanels(task, el) {
  if (!window.marked || !window.DOMPurify) return;
  const sections = [];
  const propertySection = (field, label) => {
    const value = String(task[field] || "");
    if (!value) return "";
    return '<section class="mt-property-markdown"><h3>' + escapeHtml(label) + "</h3>" +
      renderMarkdown(task, referenceSource(task, field, value)) + "</section>";
  };
  if (task.kind === "task") sections.push(propertySection("next_action", "Next action"));
  sections.push(propertySection("processing_instructions", "Processing instructions"));

  const comments = Array.isArray(task.comments) ? task.comments : [];
  if (comments.length) {
    const spans = ((task.reference_spans || {}).comments || []);
    const entries = comments.map((comment, index) => {
      const value = comment && typeof comment.text === "string" ? comment.text : "";
      const at = comment && comment.at || "";
      return '<article class="mt-comment-entry"><time datetime="' + escapeHtml(at) + '" title="' + escapeHtml(at) + '">' +
        escapeHtml(at ? formatCardDate(at) : "") + "</time>" +
        '<div class="mt-comment-body">' + renderMarkdown(task, referenceSource(task, "comments", value, spans[index])) + "</div></article>";
    }).join("");
    sections.push('<details class="mt-history-panel"><summary>Comments (' + comments.length +
      ")</summary><div class=\"mt-comment-history\">" + entries + "</div></details>");
  }

  const entries = Array.isArray(task.time_entries) ? task.time_entries : [];
  if (entries.length) {
    const rows = entries.map(entry => '<li><time>' + escapeHtml(entry.start || "") +
      "</time> – <time>" + escapeHtml(entry.end || "") + "</time> · " +
      escapeHtml(formatTrackedDuration(entry.elapsed_seconds)) + "</li>").join("");
    sections.push('<details class="mt-history-panel"><summary>Time entries (' + entries.length +
      ")</summary><ul>" + rows + "</ul></details>");
  }
  el.innerHTML = sections.join("");
}
