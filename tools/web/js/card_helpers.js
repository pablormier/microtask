// Pure card calculations. Pass policy and the current time explicitly.
// This file has no DOM, storage, network or Alpine dependencies.

function formatMetadataTags(tags) {
  return (Array.isArray(tags) ? tags : []).map(value => {
    const tag = String(value);
    return /[",\r\n]/.test(tag) ? '"' + tag.replace(/"/g, '""') + '"' : tag;
  }).join(", ");
}

function parseMetadataTags(input) {
  const values = [];
  let value = "";
  let quoted = false;
  const text = String(input || "");
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (quoted) {
      if (character === '"' && text[index + 1] === '"') {
        value += '"';
        index += 1;
      } else if (character === '"') {
        quoted = false;
      } else {
        value += character;
      }
    } else if (character === ",") {
      if (value.trim()) values.push(value.trim());
      value = "";
    } else if (character === '"' && !value.trim()) {
      value = "";
      quoted = true;
    } else {
      value += character;
    }
  }
  if (value.trim()) values.push(value.trim());
  return Array.from(new Set(values));
}

function parseCardDueDate(value) {
  const raw = String(value || "");
  return new Date(raw.length === 10 ? raw + "T23:59:59" : raw);
}

function cardDueDayDifference(value, now) {
  const today = new Date(now); today.setHours(0, 0, 0, 0);
  const dueDay = new Date(value); dueDay.setHours(0, 0, 0, 0);
  return Math.round((dueDay - today) / 86400000);
}

function formatCardDate(value) {
  if (!value) return "—";
  const date = parseCardDueDate(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const options = { day: "numeric", month: "short", year: "numeric" };
  if (String(value).includes("T")) {
    options.hour = "2-digit";
    options.minute = "2-digit";
  }
  return date.toLocaleString(undefined, options);
}

function formatTimeUntil(target, now) {
  const diff = target.getTime() - now;
  const end = diff >= 0 ? target : new Date(now);
  const start = diff >= 0 ? new Date(now) : target;
  let months = (end.getFullYear() - start.getFullYear()) * 12 + (end.getMonth() - start.getMonth());
  const anchor = new Date(start);
  anchor.setMonth(anchor.getMonth() + months);
  if (anchor.getTime() > end.getTime()) {
    months -= 1;
    anchor.setMonth(anchor.getMonth() - 1);
  }
  let remaining = Math.max(0, end.getTime() - anchor.getTime());
  const weeks = Math.floor(remaining / 604800000);
  remaining -= weeks * 604800000;
  const days = Math.floor(remaining / 86400000);
  remaining -= days * 86400000;
  const parts = [];
  if (months >= 1) parts.push(months + "mo");
  if (weeks >= 1) parts.push(weeks + "w");
  if (days >= 1) parts.push(days + "d");
  if (parts.length === 0) {
    const hours = Math.floor(remaining / 3600000);
    const minutes = Math.floor(remaining / 60000) % 60;
    parts.push(
      String(hours).padStart(2, "0") + "h",
      String(minutes).padStart(2, "0") + "m"
    );
  }
  const span = parts.join(" ") || "0d";
  return diff < 0 ? "overdue by " + span : span + " left";
}

function formatTrackedDuration(seconds) {
  const whole = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = whole % 60;
  return String(hours).padStart(2, "0") + "h "
    + String(minutes).padStart(2, "0") + "m "
    + String(secs).padStart(2, "0") + "s";
}

function elapsedPercentage(start, end, now) {
  const current = new Date(now).getTime();
  const s = new Date(start).getTime();
  const e = new Date(end).getTime();
  if (isNaN(s) || isNaN(e)) return 0;
  if (e <= s) return current >= e ? 100 : 0;
  return Math.max(0, Math.min(100, ((current - s) / (e - s)) * 100));
}

const METADATA_FIELDS = ["status", "priority", "type", "project", "topic", "tags", "due", "due_kind"];

function metadataCoreObject(source) {
  const input = source && typeof source === "object" ? source : {};
  return {
    status: input.status || null,
    priority: input.priority || null,
    type: input.type || null,
    project: input.project || null,
    topic: input.topic || null,
    tags: Array.isArray(input.tags) ? input.tags.slice() : [],
    due: input.due || null,
    due_kind: input.due_kind || null,
  };
}

function normalizeMetadataValue(initial, config) {
  const source = initial && typeof initial === "object" ? initial : {};
  const statuses = Array.isArray(config.task_statuses) ? config.task_statuses : [];
  const defaultStatus = config.task_defaults?.status;
  const fallbackStatus = statuses.length
    ? (statuses.includes(defaultStatus) ? defaultStatus : statuses[0])
    : (defaultStatus || "inbox");
  return {
    status: source.status || fallbackStatus,
    priority: source.priority || config.task_defaults?.priority || "none",
    type: source.type || config.task_defaults?.type || "task",
    project: source.project || "",
    topic: source.topic || "",
    tags: Array.isArray(source.tags) ? source.tags.slice() : [],
    due: String(source.due || "") || null,
    due_kind: source.due_kind || null,
  };
}

// Topics are '/'-separated paths. `tree` is the server's flat depth-first list of
// {path, name, depth, count} nodes (implied parents included, counts include descendants).
function normalizeTopicPath(text) {
  return String(text || "").split("/").map(part => part.trim()).join("/").trim();
}

function topicSegments(path) {
  return String(path || "").split("/").map(part => part.trim()).filter(Boolean);
}

// Picker rows: the whole tree when empty; descendants for "learning/"-style paths;
// otherwise any node with a matching segment. A new valid path adds a create row.
function topicPickerRows(tree, query, { create = true } = {}) {
  const nodes = Array.isArray(tree) ? tree : [];
  const text = normalizeTopicPath(query);
  const option = (node, indented) => ({
    kind: "option", name: node.path, label: indented ? node.name : node.path,
    depth: indented ? node.depth : 0, count: node.count
  });
  if (!text) return nodes.map(node => option(node, true));
  const lower = text.toLowerCase();
  const rows = nodes
    .filter(node => {
      const path = node.path.toLowerCase();
      return lower.includes("/") ? path.startsWith(lower) : path.split("/").some(part => part.includes(lower));
    })
    .map(node => option(node, false));
  const valid = topicSegments(text).length === text.split("/").length;
  if (create && valid && !nodes.some(node => node.path.toLowerCase() === lower)) {
    // Reuse the spelling of parents that already exist.
    const parts = topicSegments(text).map((part, index, all) => {
      const known = nodes.find(node => node.path.toLowerCase() === all.slice(0, index + 1).join("/").toLowerCase());
      return known ? known.name : part;
    });
    rows.push({ kind: "create", name: parts.join("/"), label: parts.join("/"), depth: 0, count: 0 });
  }
  return rows;
}

// Picker rows for plain names, most-used first. `usage` maps a name to its card count.
function usageChoices(names, usage) {
  return names.map(name => ({ kind: "option", name, label: name, count: usage[name] || 0 }))
    .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
}

function cardDueState(task, now) {
  if (!task || !task.due) return "none";
  const d = parseCardDueDate(task.due);
  const diff = cardDueDayDifference(d, now);
  if (d.getTime() < now) return "overdue";
  if (diff === 0) return "today";
  if (diff <= 7) return "next7";
  return "future";
}

function formatDeadlineTimeLeft(dueValue, now) {
  const due = parseCardDueDate(dueValue);
  const days = cardDueDayDifference(due, now);
  if (due.getTime() < now) {
    return days < 0 ? Math.abs(days) + "d overdue" : "Overdue";
  }
  if (days === 0) return "Due today";
  if (days === 1) return "Due tomorrow";
  return days + " days left";
}

function formatProgressPercentLabel(pct) {
  return pct >= 99.995 ? "100%" : pct.toFixed(2) + "%";
}

function cardPriorityClass(priority) {
  return "priority-" + String(priority || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}
