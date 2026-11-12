// State and keyboard navigation for searchable option lists, shared by the card
// editor's fields and the toolbar filters. One field's list is open at a time.
// The host object supplies:
//   pickerRows(field)            the rows to show, narrowed by `picker.query`
//   choosePickerRow(field, row)  what choosing a row does
//   pickerMulti(field)           true when choosing keeps the list open
//   pickerSelected(field, row)   whether a row is the current value (single-value fields)
// and may set `disabled` to keep every list closed.
function createPicker() {
  const closed = () => ({ field: "", query: "", active: 0, navigated: false });
  return {
    picker: closed(),
    pickerOpen(field) {
      return this.picker.field === field;
    },
    openPicker(field) {
      if (this.disabled || this.picker.field === field) return;
      this.picker = { ...closed(), field };
      if (!this.pickerMulti(field)) {
        this.picker.active = Math.max(0, this.pickerRows(field).findIndex(row => this.pickerSelected(field, row)));
      }
    },
    closePicker() {
      this.picker = closed();
    },
    setPickerQuery(field, text) {
      if (this.picker.field !== field) this.openPicker(field);
      this.picker.query = text;
      this.picker.active = 0;
      this.picker.navigated = Boolean(String(text).trim());
    },
    movePicker(field, step) {
      if (!this.pickerOpen(field)) this.openPicker(field);
      const count = this.pickerRows(field).length;
      if (!count) return;
      // The first arrow press highlights the current (or first) row before moving on.
      if (this.picker.navigated) this.picker.active = (this.picker.active + step + count) % count;
      this.picker.navigated = true;
    },
    pickerEnter(field, event) {
      if (!this.pickerOpen(field)) return;
      event.preventDefault();
      if (this.picker.navigated) this.choosePickerRow(field, this.pickerRows(field)[this.picker.active]);
      else if (!this.pickerMulti(field)) this.closePicker();
    },
  };
}
