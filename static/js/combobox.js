(function () {
  'use strict';

  const WARN_THRESHOLD = 10;
  const MAX_TRUCKS     = 200;

  class TruckMultiCombobox {
    constructor(root) {
      this.root       = root;
      this.hiddenIn   = root.querySelector('input[type="hidden"].combobox-value');
      this.chipsEl    = root.querySelector('.combobox-chips');
      this.input      = root.querySelector('.combobox-input');
      this.clearBtn   = root.querySelector('.combobox-clear');
      this.toggleBtn  = root.querySelector('.combobox-toggle');
      this.listbox    = root.querySelector('.combobox-listbox');
      this.status     = root.querySelector('.combobox-status');
      this.countEl    = root.querySelector('.combobox-count');
      this.warnEl     = root.querySelector('.combobox-warn');

      this.devices     = null;   // null = not loaded yet
      this.filtered    = [];
      this.activeIndex = -1;
      this.loading     = false;
      this.error       = null;

      this.selected    = new Map();
      this.order       = [];

      this._bind();
      this._renderChips();
      this._updateCount();
    }

    _bind() {
      this.input.addEventListener('focus', () => this.open());
      this.input.addEventListener('input', () => this._onInput());
      this.input.addEventListener('keydown', (e) => this._onKey(e));

      this.toggleBtn.addEventListener('click', (e) => {
        e.preventDefault();
        if (this.listbox.hidden) this.open(); else this.close();
      });

      this.clearBtn.addEventListener('click', (e) => {
        e.preventDefault();
        this.clear();
      });

      this.listbox.addEventListener('mousedown', (e) => {
        if (e.target.closest('.combobox-listbox-header')) return;

        const li = e.target.closest('.combobox-option');
        if (!li) return;
        e.preventDefault();
        const d = this.filtered[parseInt(li.dataset.index, 10)];
        if (d) this._toggle(d);
      });

      this.listbox.addEventListener('mouseover', (e) => {
        const li = e.target.closest('.combobox-option');
        if (!li) return;
        const idx = parseInt(li.dataset.index, 10);
        if (idx !== this.activeIndex) this._setActive(idx);
      });

      this.chipsEl.addEventListener('click', (e) => {
        const btn = e.target.closest('.combobox-chip-remove');
        if (!btn) return;
        e.preventDefault();
        this._remove(btn.dataset.imei);
      });

      document.addEventListener('click', (e) => {
        if (!this.root.contains(e.target)) this.close();
      });
    }

    async _loadDevices() {
      if (this.devices !== null) return;
      this.loading = true;
      this.error = null;
      this._render();
      try {
        const r = await fetch('/api/devices');
        if (!r.ok) throw new Error('HTTP ' + r.status);
        const j = await r.json();
        if (j.error) throw new Error(j.error);
        this.devices = j.devices || [];
      } catch (e) {
        this.error = 'Unable to load trucks.';
        this.devices = null;
      } finally {
        this.loading = false;
        this._render();
      }
    }

    async open() {
      this.listbox.hidden = false;
      this.input.setAttribute('aria-expanded', 'true');
      if (this.devices === null && !this.loading) {
        await this._loadDevices();
      }
      this._filter();
    }

    close() {
      this.listbox.hidden = true;
      this.status.hidden = true;
      this.input.setAttribute('aria-expanded', 'false');
      this.activeIndex = -1;
    }

    _onInput() {
      if (this.listbox.hidden) {
        this.listbox.hidden = false;
        this.input.setAttribute('aria-expanded', 'true');
      }
      this._filter();
    }

    _onKey(e) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        if (this.listbox.hidden) { this.open(); return; }
        this._move(1);
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        this._move(-1);
      } else if (e.key === 'Enter') {
        if (!this.listbox.hidden && this.activeIndex >= 0) {
          e.preventDefault();
          const d = this.filtered[this.activeIndex];
          if (d) this._toggle(d);
        }
      } else if (e.key === 'Escape') {
        this.close();
      }
      // NOTE: Backspace on an empty input no longer removes the last chip.
      // Users remove chips via the × button (or "Clear all" in the listbox
      // header). The old shortcut conflicted with the "clear the search,
      // type a new truck, keep the old one" workflow.
    }

    _move(dir) {
      if (!this.filtered.length) return;
      const next =
        (this.activeIndex + dir + this.filtered.length) % this.filtered.length;
      this._setActive(next);
      const el = this.listbox.querySelector(
        '.combobox-option[data-index="' + next + '"]'
      );
      if (el) el.scrollIntoView({ block: 'nearest' });
    }

    _setActive(idx) {
      if (idx === this.activeIndex) return;
      this.activeIndex = idx;
      const items = this.listbox.querySelectorAll('.combobox-option');
      items.forEach(function (li) {
        li.setAttribute(
          'aria-selected',
          parseInt(li.dataset.index, 10) === idx ? 'true' : 'false'
        );
      });
    }

    _filter() {
      if (this.devices === null) {
        this.filtered = [];
        this._render();
        return;
      }
      const q = this.input.value.trim().toLowerCase();
      if (!q) {
        this.filtered = this.devices.slice(0, 500);
      } else {
        const prefix = [];
        const contains = [];
        for (const d of this.devices) {
          const name = (d.name || '').toLowerCase();
          const imei = (d.imei || '').toLowerCase();
          if (name.startsWith(q) || imei.startsWith(q)) prefix.push(d);
          else if (name.includes(q) || imei.includes(q)) contains.push(d);
        }
        this.filtered = prefix.concat(contains).slice(0, 500);
      }
      this.activeIndex = this.filtered.length ? 0 : -1;
      this._render();
    }

    _toggle(device) {
      if (!device) return;
      if (this.selected.has(device.imei)) {
        this._remove(device.imei);
        return;
      }
      if (this.selected.size >= MAX_TRUCKS) {
        this._flashWarn('You can select at most ' + MAX_TRUCKS + ' trucks per search.');
        return;
      }
      this.selected.set(device.imei, device);
      this.order.push(device.imei);
      this._afterChange();
    }

    _remove(imei) {
      if (!this.selected.has(imei)) return;
      this.selected.delete(imei);
      const i = this.order.indexOf(imei);
      if (i >= 0) this.order.splice(i, 1);
      this._afterChange();
    }

    clear() {
      this.selected.clear();
      this.order.length = 0;
      this._afterChange();
      this.input.focus();
      this.open();
    }

    _afterChange() {
      this._syncHidden();
      this._renderChips();
      this._updateCount();
      this._render();
    }

    _syncHidden() {
      this.hiddenIn.value = JSON.stringify(this.order.map((imei) => {
        const d = this.selected.get(imei);
        return { imei, name: d.name, driver: d.driver || '' };
      }));
    }

    _renderChips() {
      this.chipsEl.innerHTML = '';
      if (!this.order.length) {
        this.clearBtn.hidden = true;
        return;
      }
      this.clearBtn.hidden = false;
      const frag = document.createDocumentFragment();
      for (const imei of this.order) {
        const d = this.selected.get(imei);
        const chip = document.createElement('span');
        chip.className = 'combobox-chip';
        chip.title = d.name + ' · ' + imei;

        const label = document.createElement('span');
        label.className = 'combobox-chip-label';
        label.textContent = d.name;

        const rm = document.createElement('button');
        rm.type = 'button';
        rm.className = 'combobox-chip-remove';
        rm.dataset.imei = imei;
        rm.setAttribute('aria-label', 'Remove ' + d.name);
        rm.textContent = '\u00d7';

        chip.appendChild(label);
        chip.appendChild(rm);
        frag.appendChild(chip);
      }
      this.chipsEl.appendChild(frag);
    }

    _updateCount() {
      const n = this.order.length;
      if (n === 0) {
        this.countEl.hidden = true;
        this.warnEl.hidden = true;
        return;
      }
      this.countEl.hidden = false;
      this.countEl.innerHTML =
        '<strong>' + n + '</strong> truck' + (n === 1 ? '' : 's') + ' selected';

      if (n > WARN_THRESHOLD) {
        this.warnEl.hidden = false;
        this.warnEl.innerHTML =
          '<strong>Heads up:</strong> searching many trucks uses many ' +
          'Tracksolid API calls and can take several minutes. ' +
          'If the daily API limit is reached, only earlier trucks will be included.';
      } else {
        this.warnEl.hidden = true;
      }
    }

    _flashWarn(msg) {
      this.warnEl.hidden = false;
      this.warnEl.textContent = msg;
    }

    _render() {
      if (this.loading) {
        this.status.hidden = false;
        this.status.textContent = 'Loading trucks\u2026';
        this.listbox.innerHTML = '';
        return;
      }

      if (this.error) {
        this.status.hidden = false;
        this.status.innerHTML = '';
        const txt = document.createElement('span');
        txt.textContent = this.error + ' ';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'combobox-retry';
        btn.textContent = 'Try again';
        btn.addEventListener('click', () => {
          this.devices = null;
          this._loadDevices();
        });
        this.status.appendChild(txt);
        this.status.appendChild(btn);
        this.listbox.innerHTML = '';
        return;
      }

      if (this.devices === null) {
        this.status.hidden = true;
        this.listbox.innerHTML = '';
        return;
      }

      if (!this.filtered.length) {
        this.status.hidden = false;
        const q = this.input.value.trim();
        this.status.textContent = q
          ? 'No trucks match "' + q + '". Try a shorter search.'
          : 'No trucks on this account.';
        this.listbox.innerHTML = '';
        return;
      }

      this.status.hidden = true;
      const frag = document.createDocumentFragment();

      const header = document.createElement('li');
      header.className = 'combobox-listbox-header';
      header.setAttribute('aria-hidden', 'true');

      const allLabel = document.createElement('label');
      const allCb = document.createElement('input');
      allCb.type = 'checkbox';
      const filteredImis = this.filtered.map((d) => d.imei);
      const selectedInFiltered = filteredImis.filter((i) => this.selected.has(i)).length;
      allCb.checked = filteredImis.length > 0 && selectedInFiltered === filteredImis.length;
      allCb.indeterminate = selectedInFiltered > 0 && selectedInFiltered < filteredImis.length;
      allCb.addEventListener('change', () => {
        if (allCb.checked) this._selectAll(this.filtered);
        else this._clearFiltered(this.filtered);
      });
      allLabel.appendChild(allCb);
      allLabel.appendChild(document.createTextNode(
        this.input.value.trim()
          ? ' Select all matching'
          : ' Select all'
      ));

      const clearLink = document.createElement('button');
      clearLink.type = 'button';
      clearLink.className = 'combobox-selectall-link';
      clearLink.textContent = 'Clear all';
      clearLink.addEventListener('mousedown', (e) => {
        e.preventDefault();
        this.clear();
      });

      header.appendChild(allLabel);
      header.appendChild(clearLink);
      frag.appendChild(header);

      for (let i = 0; i < this.filtered.length; i++) {
        const d = this.filtered[i];
        const checked = this.selected.has(d.imei);

        const li = document.createElement('li');
        li.className = 'combobox-option';
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', checked ? 'true' : 'false');
        li.dataset.index = i;
        li.dataset.imei = d.imei;

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.checked = checked;
        cb.tabIndex = -1;
        cb.addEventListener('mousedown', (e) => e.preventDefault());

        const text = document.createElement('div');
        text.className = 'combobox-option-text';

        const nameEl = document.createElement('span');
        nameEl.className = 'combobox-option-name';
        nameEl.textContent = d.name;

        const metaEl = document.createElement('span');
        metaEl.className = 'combobox-option-meta';
        metaEl.textContent = d.imei + (d.driver ? ' \u00b7 ' + d.driver : '');

        text.appendChild(nameEl);
        text.appendChild(metaEl);
        li.appendChild(cb);
        li.appendChild(text);

        if (i === this.activeIndex) li.setAttribute('aria-selected', 'true');

        frag.appendChild(li);
      }

      this.listbox.innerHTML = '';
      this.listbox.appendChild(frag);
    }

    _selectAll(list) {
      let hitCap = false;
      for (const d of list) {
        if (this.selected.has(d.imei)) continue;
        if (this.selected.size >= MAX_TRUCKS) { hitCap = true; break; }
        this.selected.set(d.imei, d);
        this.order.push(d.imei);
      }
      if (hitCap) {
        this._flashWarn('Stopped at ' + MAX_TRUCKS + ' trucks (the per-search cap).');
      }
      this._afterChange();
    }

    _clearFiltered(list) {
      const drop = new Set(list.map((d) => d.imei));
      for (const imei of Array.from(this.selected.keys())) {
        if (drop.has(imei)) this._remove(imei);
      }
    }
  }

  document.querySelectorAll('[data-combobox-multi]').forEach(function (el) {
    new TruckMultiCombobox(el);
  });
})();