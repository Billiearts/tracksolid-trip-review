(function () {
  'use strict';

  class TruckCombobox {
    constructor(root) {
      this.root        = root;
      this.hiddenInput = root.querySelector('input[type="hidden"]');
      this.input       = root.querySelector('.combobox-input');
      this.clearBtn    = root.querySelector('.combobox-clear');
      this.toggleBtn   = root.querySelector('.combobox-toggle');
      this.listbox     = root.querySelector('.combobox-listbox');
      this.status      = root.querySelector('.combobox-status');

      this.devices     = null;   // null = not loaded yet
      this.filtered    = [];
      this.activeIndex = -1;
      this.loading     = false;
      this.error       = null;

      this._bind();
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

      // Delegated: works even after the listbox is rebuilt.
      // mousedown (not click) so we can preventDefault to keep input focus.
      this.listbox.addEventListener('mousedown', (e) => {
        const li = e.target.closest('.combobox-option');
        if (!li) return;
        e.preventDefault();
        const d = this.filtered[parseInt(li.dataset.index, 10)];
        if (d) this._select(d);
      });

      // Hover tracking: only flips aria-selected. No re-render.
      this.listbox.addEventListener('mouseover', (e) => {
        const li = e.target.closest('.combobox-option');
        if (!li) return;
        const idx = parseInt(li.dataset.index, 10);
        if (idx !== this.activeIndex) this._setActive(idx);
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
      this.hiddenInput.value = '';
      this.clearBtn.hidden = true;
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
          this._select(this.filtered[this.activeIndex]);
        }
      } else if (e.key === 'Escape') {
        this.close();
      }
    }

    _move(dir) {
      if (!this.filtered.length) return;
      const next =
        (this.activeIndex + dir + this.filtered.length) % this.filtered.length;
      this._setActive(next);
      const el = this.listbox.children[next];
      if (el) el.scrollIntoView({ block: 'nearest' });
    }

    _setActive(idx) {
      if (idx === this.activeIndex) return;
      this.activeIndex = idx;
      const items = this.listbox.children;
      for (let i = 0; i < items.length; i++) {
        items[i].setAttribute('aria-selected', i === idx ? 'true' : 'false');
      }
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
          const name = d.name.toLowerCase();
          const imei = d.imei.toLowerCase();
          if (name.startsWith(q) || imei.startsWith(q)) prefix.push(d);
          else if (name.includes(q) || imei.includes(q)) contains.push(d);
        }
        this.filtered = prefix.concat(contains).slice(0, 500);
      }
      this.activeIndex = this.filtered.length ? 0 : -1;
      this._render();
    }

    _select(device) {
      if (!device) return;
      this.hiddenInput.value = device.imei;
      this.input.value = device.name;
      this.clearBtn.hidden = false;
      this.close();
    }

    clear() {
      this.hiddenInput.value = '';
      this.input.value = '';
      this.clearBtn.hidden = true;
      this.input.focus();
      this.open();
    }

    _render() {
      // Loading
      if (this.loading) {
        this.status.hidden = false;
        this.status.textContent = 'Loading trucks\u2026';
        this.listbox.innerHTML = '';
        return;
      }
      // Error
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
      // Not loaded yet
      if (this.devices === null) {
        this.status.hidden = true;
        this.listbox.innerHTML = '';
        return;
      }
      // No match
      if (!this.filtered.length) {
        this.status.hidden = false;
        const q = this.input.value.trim();
        this.status.textContent = q
          ? 'No trucks match "' + q + '". Try a shorter search.'
          : 'No trucks on this account.';
        this.listbox.innerHTML = '';
        return;
      }
      // List
      this.status.hidden = true;
      const frag = document.createDocumentFragment();
      for (let i = 0; i < this.filtered.length; i++) {
        const d = this.filtered[i];
        const li = document.createElement('li');
        li.className = 'combobox-option';
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', i === this.activeIndex ? 'true' : 'false');
        li.dataset.index = i;

        const nameEl = document.createElement('span');
        nameEl.className = 'combobox-option-name';
        nameEl.textContent = d.name;

        const metaEl = document.createElement('span');
        metaEl.className = 'combobox-option-meta';
        metaEl.textContent = d.imei + (d.driver ? ' \u00b7 ' + d.driver : '');

        li.appendChild(nameEl);
        li.appendChild(metaEl);
        frag.appendChild(li);
      }
      this.listbox.innerHTML = '';
      this.listbox.appendChild(frag);
    }
  }

  document.querySelectorAll('[data-combobox]').forEach(function (el) {
    new TruckCombobox(el);
  });
})();