// Clipboard paste, drag-and-drop, and file-picker upload helpers for attachments.
function extractClipboardFiles(event) {
  const data = event && event.clipboardData;
  if (!data) return [];
  const files = [];
  if (data.items && data.items.length) {
    for (const item of Array.from(data.items)) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
  }
  if (!files.length && data.files && data.files.length) {
    return Array.from(data.files);
  }
  return files;
}

function extractDropFiles(event) {
  const data = event && event.dataTransfer;
  if (!data || !data.files || !data.files.length) return [];
  return Array.from(data.files);
}

function insertTextAtCursor(textarea, currentText, snippet) {
  const text = String(currentText || "");
  if (textarea && typeof textarea.selectionStart === "number" && typeof textarea.selectionEnd === "number") {
    const start = textarea.selectionStart;
    const end = textarea.selectionEnd;
    const next = text.slice(0, start) + snippet + text.slice(end);
    const cursor = start + snippet.length;
    return { text: next, cursor };
  }
  const sep = text && !text.endsWith("\n") ? "\n" : "";
  const next = text + sep + snippet;
  return { text: next, cursor: next.length };
}

async function uploadAttachmentFile(apiToken, file, options = {}) {
  const headers = {
    "X-Microtask-Token": apiToken || "",
    "Content-Type": (file && file.type) || "application/octet-stream",
  };
  if (file && file.name) {
    headers["X-Filename"] = encodeURIComponent(file.name);
  }
  if (options && options.scope) {
    headers["X-Attachment-Scope"] = String(options.scope);
  }
  const response = await fetch("/api/attachments", {
    method: "POST",
    headers,
    body: file,
  });
  let payload = {};
  if (typeof readJsonResponse === "function") {
    payload = await readJsonResponse(response);
  } else {
    try {
      payload = await response.json();
    } catch (_) {}
  }
  if (!response.ok || !payload.attachment) {
    throw new Error(payload.error || ("Attachment upload failed (" + response.status + ")"));
  }
  return payload.attachment;
}

let _mtAttachmentUploadSeq = 0;

async function uploadAndInsertAttachments({ files, textarea, getText, setText, apiToken, scope, notify, nextTick }) {
  const list = Array.from(files || []).filter(Boolean);
  if (!list.length) return [];
  const entries = list.map(file => {
    const seq = ++_mtAttachmentUploadSeq;
    const isImage = Boolean(file.type && file.type.startsWith("image/"));
    const displayName = (file.name || (isImage ? "image" : "file")).replace(/[\[\]\r\n]+/g, " ").trim();
    const placeholder = (isImage ? "!" : "") + "[Uploading " + displayName + "…](#mt-upload-" + seq + ")";
    return { file, placeholder };
  });
  const block = entries.map(entry => entry.placeholder).join("\n");
  const inserted = insertTextAtCursor(textarea, getText(), block);
  setText(inserted.text);
  if (textarea && typeof nextTick === "function") {
    nextTick(() => {
      try {
        textarea.selectionStart = inserted.cursor;
        textarea.selectionEnd = inserted.cursor;
      } catch (_) {}
    });
  }
  const uploaded = [];
  for (const { file, placeholder } of entries) {
    try {
      const attachment = await uploadAttachmentFile(apiToken, file, { scope });
      const current = String(getText() || "");
      if (current.includes(placeholder)) {
        setText(current.replace(placeholder, () => attachment.markdown));
      } else if (typeof notify === "function") {
        notify("Uploaded; link not inserted: " + attachment.markdown, false);
      }
      uploaded.push(attachment);
    } catch (error) {
      const current = String(getText() || "");
      if (current.includes(placeholder)) {
        setText(current.replace(placeholder, () => ""));
      }
      if (typeof notify === "function") {
        notify(error && error.message ? error.message : "Attachment upload failed", true);
      }
    }
  }
  return uploaded;
}
