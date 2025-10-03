// static/financials.js

(() => {
  const qs = (sel) => document.querySelector(sel);
  const qsa = (sel) => Array.from(document.querySelectorAll(sel));

  // Elemente
  const storeSelect = qs("#storeSelect");
  const statusSelect = qs("#statusSelect");          // <select multiple> populat de backend
  const startInput  = qs("#startDate");
  const endInput    = qs("#endDate");
  const sortSelect  = qs("#sortSelect");
  const syncBtn     = qs("#syncBtn");
  const clearBtn    = qs("#clearViewBtn");
  const tableBody   = qs("#ordersTable tbody");
  const emptyState  = qs("#emptyState");             // un <div> cu mesaj "Nu sunt date"
  const countBadge  = qs("#rowsCount");
  const loadingBar  = qs("#loadingBar");             // o bară / spinner simplu

  const CLEARED_KEY = "financialsCleared";

  // Utils
  const show = (el) => el && (el.style.display = "");
  const hide = (el) => el && (el.style.display = "none");
  const enable = (el) => el && (el.disabled = false);
  const disable = (el) => el && (el.disabled = true);

  function getSelectedStatuses() {
    return qsa("#statusSelect option:checked").map(o => o.value);
  }

  function getStoreParam() {
    const v = storeSelect?.value ?? "all";
    return (!v || v === "all") ? "all" : String(v);
  }

  function getQueryParams() {
    const params = new URLSearchParams();
    const store = getStoreParam();
    if (store !== "all") params.set("store_id", store);

    if (startInput.value) params.set("start", startInput.value);
    if (endInput.value)   params.set("end",   endInput.value);

    const statuses = getSelectedStatuses();
    if (statuses.length) params.set("statuses", statuses.join(","));

    if (sortSelect?.value) params.set("sort", sortSelect.value);

    return params.toString();
  }

  function renderRows(rows) {
    tableBody.innerHTML = "";
    if (!rows || !rows.length) {
      hide(countBadge);
      show(emptyState);
      return;
    }
    show(countBadge);
    hide(emptyState);

    countBadge.textContent = rows.length;

    const fmt = (d) => d ? new Date(d).toLocaleString() : "-";

    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${r.store_name ?? "-"}</td>
        <td>${r.name}</td>
        <td>${r.customer ?? "-"}</td>
        <td>${r.total_price?.toFixed ? r.total_price.toFixed(2) : r.total_price ?? "-"}</td>
        <td>${fmt(r.created_at)}</td>
        <td><span class="badge badge-fin ${r.financial_status}">${r.financial_status ?? "-"}</span></td>
        <td><span class="badge badge-ful ${r.fulfillment_status}">${r.fulfillment_status ?? "-"}</span></td>
        <td>${r.courier_status ?? "-"}</td>
        <td>${fmt(r.courier_status_at)}</td>
        <td>
          ${r.financial_status === "pending"
            ? `<button class="btn btn-xs mark-paid" data-order-id="${r.id}" data-store-id="${r.store_id}">Mark as paid</button>`
            : `<span class="text-muted">—</span>`}
        </td>
      `;
      tableBody.appendChild(tr);
    }
  }

  async function fetchStatuses() {
    try {
      const res = await fetch("/financials/statuses");
      if (!res.ok) return;
      const data = await res.json();
      statusSelect.innerHTML = "";
      // opțiunea goală pentru "toate"
      const optAll = document.createElement("option");
      optAll.value = "";
      optAll.textContent = "Toate statusurile curier";
      statusSelect.appendChild(optAll);

      (data.statuses || []).forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s;
        opt.textContent = s;
        statusSelect.appendChild(opt);
      });
    } catch (_) {}
  }

  async function loadOrders() {
    // dacă view e "cleared", nu încărcăm nimic
    if (localStorage.getItem(CLEARED_KEY) === "1") {
      tableBody.innerHTML = "";
      hide(countBadge);
      show(emptyState);
      return;
    }

    const query = getQueryParams();
    try {
      show(loadingBar);
      const res = await fetch(`/financials/data?${query}`);
      hide(loadingBar);
      if (!res.ok) throw new Error("Eroare la încărcarea datelor");
      const data = await res.json();
      renderRows(data.rows || []);
    } catch (e) {
      hide(loadingBar);
      console.error(e);
      tableBody.innerHTML = "";
      show(emptyState);
    }
  }

  async function startSync() {
    disable(syncBtn);
    try {
      const payload = {
        start_date: startInput.value || null,
        end_date:   endInput.value   || null,
        store_id:   getStoreParam(),   // "all" sau id
      };
      const res = await fetch("/financials/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error("Sync a eșuat");
      // IMPORTANT: după sync, scoatem flagul de cleared și re-încărcăm
      localStorage.removeItem(CLEARED_KEY);
      await loadOrders();
    } catch (e) {
      console.error(e);
      alert("Sync a eșuat. Vezi logs.");
    } finally {
      enable(syncBtn);
    }
  }

  function clearView() {
    // Persistăm "pagina goală" până la următorul sync
    localStorage.setItem(CLEARED_KEY, "1");
    tableBody.innerHTML = "";
    hide(countBadge);
    show(emptyState);
  }

  // Click pe rând – delegare pentru "Mark as paid"
  tableBody.addEventListener("click", async (ev) => {
    const btn = ev.target.closest(".mark-paid");
    if (!btn) return;
    const orderId = btn.dataset.orderId;
    const storeId = btn.dataset.storeId;

    btn.disabled = true;
    try {
      const res = await fetch("/financials/mark-as-paid", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ order_ids: [Number(orderId)], store_id: Number(storeId) }),
      });
      if (!res.ok) throw new Error("Mark as paid a eșuat");
      // Scoatem rândul din tabel (cerință: să dispară imediat)
      btn.closest("tr")?.remove();
      // Update count
      const left = qsa("#ordersTable tbody tr").length;
      if (left === 0) {
        hide(countBadge);
        show(emptyState);
      } else {
        countBadge.textContent = left;
      }
    } catch (e) {
      console.error(e);
      alert("Nu am reușit să marchez comanda ca plătită.");
    } finally {
      btn.disabled = false;
    }
  });

  // Filtre: reîncarcă la schimbare
  [storeSelect, statusSelect, sortSelect, startInput, endInput].forEach((el) => {
    el?.addEventListener("change", loadOrders);
  });

  // Asigură că click pe inputul de dată deschide nativ pickerul
  [startInput, endInput].forEach((el) => {
    el?.addEventListener("click", () => el.showPicker && el.showPicker());
  });

  syncBtn?.addEventListener("click", startSync);
  clearBtn?.addEventListener("click", clearView);

  // Init
  (async () => {
    await fetchStatuses();
    await loadOrders();
  })();
})();
