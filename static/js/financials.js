// static/js/financials.js
document.addEventListener('DOMContentLoaded', () => {
  // ----- Controls (filters)
  const storeFilter   = document.getElementById('storeFilter');
  const courierFilter = document.getElementById('courierFilter');
  const fromInput     = document.getElementById('ordersFrom');
  const toInput       = document.getElementById('ordersTo');
  const statusMulti   = document.getElementById('statusMulti');

  // ----- Table
  const tbody       = document.getElementById('orders-tbody');
  const countSpan   = document.getElementById('orders-count');
  const selectAll   = document.getElementById('selectAll');
  const markPaidBtn = document.getElementById('markPaidBtn');

  // ----- Sync panel
  const startDate   = document.getElementById('startDate');
  const endDate     = document.getElementById('endDate');
  const storeSelect = document.getElementById('storeSelect');
  const syncButton  = document.getElementById('syncButton');
  const clearBtn    = document.getElementById('clearViewButton');
  const clearedMsg  = document.getElementById('clearedMessage');

  // ---- Make date inputs open the picker only on a user gesture
  ['startDate','endDate','ordersFrom','ordersTo'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.addEventListener('pointerdown', () => {
      if (typeof el.showPicker === 'function') {
        try { el.showPicker(); } catch (_) {}
      }
    });
  });

  // ---- Avoid implicit selection of first option in a <select multiple>
  if (statusMulti) Array.from(statusMulti.options).forEach(o => (o.selected = false));

  // ---- Helpers
  const fmtDate = iso => {
    if (!iso) return '';
    const d = new Date(iso);
    return Number.isNaN(d.getTime())
      ? ''
      : d.toLocaleDateString('ro-RO', { year: 'numeric', month: '2-digit', day: '2-digit' });
  };

  const getSelectedStatuses = () =>
    Array.from(statusMulti?.selectedOptions || []).map(o => o.value).filter(Boolean);

  // ---- Render
  const renderRows = rows => {
    tbody.innerHTML = '';
    if (!rows.length) {
      const tr = document.createElement('tr');
      const td = document.createElement('td');
      td.colSpan = 8;
      td.className = 'text-center text-muted py-4';
      td.textContent = 'Niciun rezultat';
      tr.appendChild(td);
      tbody.appendChild(tr);
      countSpan.textContent = '0 rezultate';
      return;
    }

    rows.forEach(r => {
      const tr = document.createElement('tr');

      const tdSel = document.createElement('td');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'row-select';
      cb.value = r.id;
      tdSel.appendChild(cb);

      const tdOrder = document.createElement('td');   tdOrder.textContent = r.name || r.id;
      const tdCust  = document.createElement('td');   tdCust.textContent  = r.customer || '-';
      const tdDate  = document.createElement('td');   tdDate.textContent  = fmtDate(r.created_at);
      const tdFin   = document.createElement('td');   tdFin.textContent   = r.financial_status || '';
      const tdFulf  = document.createElement('td');   tdFulf.textContent  = r.fulfillment_status || '';
      const tdC     = document.createElement('td');   tdC.textContent     = r.courier || '';
      const tdCRaw  = document.createElement('td');   tdCRaw.textContent  = r.courier_raw_status || '';

      tr.append(tdSel, tdOrder, tdCust, tdDate, tdFin, tdFulf, tdC, tdCRaw);
      tbody.appendChild(tr);
    });
    countSpan.textContent = `${rows.length} rezultate`;

    // Populate courier dropdown from data (preserve current selection & "Toți Curierii")
    const current = courierFilter.value;
    const keepFirst = courierFilter.options[0]; // "Toți Curierii"
    const values = new Set(['', ...rows.map(r => r.courier).filter(Boolean)]);
    while (courierFilter.options.length) courierFilter.remove(0);
    courierFilter.appendChild(keepFirst);
    values.delete(''); // we already have the blank option
    [...values].sort().forEach(v => {
      const opt = document.createElement('option');
      opt.value = v; opt.textContent = v;
      courierFilter.appendChild(opt);
    });
    // restore selection if still available
    if (values.has(current)) courierFilter.value = current;
  };

  // ---- Fetch list
  async function loadOrders() {
    const params = new URLSearchParams();
    if (storeFilter?.value)   params.set('store_id', storeFilter.value);
    if (courierFilter?.value) params.set('courier', courierFilter.value);
    if (fromInput?.value)     params.set('start', fromInput.value);
    if (toInput?.value)       params.set('end', toInput.value);

    const sts = getSelectedStatuses();
    if (sts.length) params.set('statuses', sts.join(','));

    try {
      const res = await fetch(`/financials/data?${params.toString()}`);
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const data = await res.json();
      renderRows(Array.isArray(data.rows) ? data.rows : []);
    } catch (err) {
      console.error('Eroare la /financials/data:', err);
      renderRows([]);
    }
  }

  // ---- Mark as Paid
  async function markSelectedPaid() {
    const ids = Array.from(document.querySelectorAll('.row-select:checked')).map(i => i.value);
    if (!ids.length) return;

    const fd = new FormData();
    ids.forEach(id => fd.append('order_ids', id));

    try {
      const res = await fetch('/financials/mark-as-paid', { method: 'POST', body: fd });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      await loadOrders();
    } catch (e) {
      console.error('mark-as-paid error:', e);
    }
  }

  // ---- Sync
  async function doSync() {
    const payload = {
      start: startDate?.value || null,
      end:   endDate?.value   || null,
      store_id: storeSelect?.value ? Number(storeSelect.value) : null,
    };

    // Try /sync-range first, fall back to /sync if needed
    async function post(url) {
      const r = await fetch(url, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      return r.json();
    }

    try {
      try { await post('/financials/sync-range'); }
      catch { await post('/financials/sync'); }
      await loadOrders();
    } catch (e) {
      console.error('sync error:', e);
    }
  }

  // ---- Events
  [storeFilter, courierFilter, fromInput, toInput, statusMulti].forEach(el => {
    if (el) el.addEventListener('change', loadOrders);
  });

  if (selectAll) {
    selectAll.addEventListener('change', () => {
      document.querySelectorAll('.row-select').forEach(cb => { cb.checked = selectAll.checked; });
    });
  }

  if (markPaidBtn) markPaidBtn.addEventListener('click', markSelectedPaid);
  if (syncButton)  syncButton.addEventListener('click', doSync);
  if (clearBtn)    clearBtn.addEventListener('click', () => {
    tbody.innerHTML = '';
    countSpan.textContent = '';
    // keep only the first option (“Toți Curierii”)
    while (courierFilter.options.length > 1) courierFilter.remove(1);
    clearedMsg?.classList.remove('d-none');
  });

  // ---- Initial load
  loadOrders();
});
