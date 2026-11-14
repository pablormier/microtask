// Shared metadata editor Alpine component used by both capture and card detail.
function cardMetadataEditor(initial, createMode, prefix, config, fieldErrors, kind = "task") {
  config = config || {};
  const value = normalizeMetadataValue(initial, config);
  const due = String(value.due || "");
  const picker = createPicker();
  return {
    createMode: Boolean(createMode),
    cardKind: kind,
    morePropertiesOpen: false,
    prefix: prefix || "mt-card-metadata",
    identity: "",
    disabled: false,
    fieldErrors: fieldErrors && typeof fieldErrors === "object" ? fieldErrors : {},
    value,
    tagsText: formatMetadataTags(value.tags),
    dateValue: due.slice(0, 10),
    timeValue: due.includes("T") ? due.split("T")[1].slice(0, 5) : "",
    tagsEdited: false,
    dueEdited: false,
    priorities: config.task_priorities || {},
    types: config.task_types || {},
    dueKinds: config.task_due_kinds || {},
    openStatuses: config.task_statuses || [],
    statusLabels: config.status_labels || {},
    projects: Array.from(new Set(config.workspace_projects || [])).sort(),
    topicTree: Array.isArray(config.topic_tree) ? config.topic_tree : [],
    tags: Array.from(new Set([...(config.suggested_tags || []), ...(config.workspace_tags || [])])).sort(),
    usage: config.workspace_usage || {},
    // Project, topic, and tags share one searchable picker; only one is open at a time.
    ...picker,
    replaceValue(next) {
      this.value = normalizeMetadataValue(next, config);
      const due = String(this.value.due || "");
      this.tagsText = formatMetadataTags(this.value.tags);
      this.dateValue = due.slice(0, 10);
      this.timeValue = due.includes("T") ? due.split("T")[1].slice(0, 5) : "";
      this.tagsEdited = false;
      this.dueEdited = false;
    },
    syncIdentity(identity, sourceValue, errors) {
      const nextIdentity = String(identity || "");
      if (nextIdentity !== this.identity) {
        this.identity = nextIdentity;
        this.replaceValue(sourceValue);
      }
      this.fieldErrors = errors && typeof errors === "object" ? errors : {};
    },
    syncTags() {
      this.tagsEdited = true;
      this.value.tags = parseMetadataTags(this.tagsText);
    },
    syncDue() {
      this.dueEdited = true;
      if (!this.dateValue) {
        this.timeValue = "";
        this.value.due = null;
        this.value.due_kind = null;
        return;
      }
      this.value.due = this.dateValue + (this.timeValue ? "T" + this.timeValue + ":00" : "");
    },
    syncDueKind() {
      this.value.due_kind = this.dateValue ? (this.value.due_kind || null) : null;
    },
    copy() {
      return { ...this.value, tags: this.value.tags.slice() };
    },
    emitChange() {
      this.$dispatch("metadata-change", this.copy());
    },
    pickerChoices(field) {
      if (field === "topic") return topicPickerRows(this.topicTree, "", { create: false });
      const names = { project: this.projects, tags: this.tags }[field] || [];
      return usageChoices(names, this.usage[field === "tags" ? "tag" : field] || {});
    },
    pickerMatch(field, text) {
      if (field === "topic") {
        const path = normalizeTopicPath(text).toLowerCase();
        return path ? this.topicTree.find(node => node.path.toLowerCase() === path) : null;
      }
      const lower = String(text || "").trim().toLowerCase();
      return lower ? this.pickerChoices(field).find(choice => choice.name.toLowerCase() === lower) : null;
    },
    pickerRows(field) {
      const query = this.picker.field === field ? this.picker.query.trim() : "";
      if (field === "topic") {
        const rows = topicPickerRows(this.topicTree, query);
        if (!query && this.value.topic) rows.unshift({ kind: "clear", name: "", label: "", depth: 0, count: 0 });
        return rows;
      }
      const lower = query.toLowerCase();
      const chosen = field === "tags" ? this.value.tags : [];
      const rows = this.pickerChoices(field)
        .filter(choice => !chosen.includes(choice.name) && (!lower || choice.name.toLowerCase().includes(lower)));
      const known = this.pickerMatch(field, query) || chosen.some(tag => tag.toLowerCase() === lower);
      if (query && !known) rows.push({ kind: "create", name: query, count: 0 });
      if (field !== "tags" && !query && this.value[field]) rows.unshift({ kind: "clear", name: "", count: 0 });
      return rows;
    },
    pickerMulti(field) {
      return field === "tags";
    },
    pickerSelected(field, row) {
      return row.name === this.value[field];
    },
    setPickerQuery(field, text) {
      if (field === "tags" && String(text).includes(",")) {
        // A comma (typed or pasted) completes each tag before it.
        const parts = String(text).split(",");
        text = parts.pop().trimStart();
        parts.forEach(part => this.addTag(part));
      }
      picker.setPickerQuery.call(this, field, text);
    },
    choosePickerRow(field, row) {
      if (!row) return;
      if (field === "tags") {
        this.addTag(row.name);
        return;
      }
      this.value[field] = row.kind === "clear" ? "" : row.name;
      this.closePicker();
      this.emitChange();
    },
    leavePicker(field) {
      // Typed text is kept when focus leaves, matching an existing value's spelling.
      if (!this.pickerOpen(field)) return;
      const query = this.picker.query.trim();
      const match = this.pickerMatch(field, query);
      if (query) this.choosePickerRow(field, match || { kind: "create", name: query });
      this.closePicker();
    },
    addTag(name) {
      const tag = String(name || "").trim();
      if (this.picker.field === "tags") {
        this.picker.query = "";
        this.picker.active = 0;
        this.picker.navigated = false;
      }
      if (!tag || this.value.tags.some(existing => existing.toLowerCase() === tag.toLowerCase())) return;
      const canonical = (this.pickerMatch("tags", tag) || { name: tag }).name;
      this.value.tags = [...this.value.tags, canonical];
      this.tagsText = formatMetadataTags(this.value.tags);
      this.tagsEdited = true;
      this.emitChange();
    },
    removeTag(tag) {
      this.value.tags = this.value.tags.filter(existing => existing !== tag);
      this.tagsText = formatMetadataTags(this.value.tags);
      this.tagsEdited = true;
      this.emitChange();
    },
  };
}
