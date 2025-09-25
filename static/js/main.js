// ===================== Main JS (global, complet) =====================
document.addEventListener('DOMContentLoaded', function () {

  // ---------- Utils ----------
  const el = (id) => document.getElementById(id);
  const q = (sel, root=document) => root.querySelector(sel);
  const qa = (sel, root=document) => Array.from(root.querySelectorAll(sel));
  const intVal = (id, dv=null) => { const v = el(id)?.value; const n = parseInt(v, 10); return Number.isFinite(n) ? n : dv; };
  const floatVal = (id, dv=null) => { const v = el(id)?.value; const f = parseFloat(v); return Number.isFinite(f) ? f : dv; };

  // ---------- Progress bar helpers ----------
  const syncProgressBar = el('syncProgressBar');
  const syncProgressText = el('syncProgressText');
  function showSyncProgress(message, isError = false) {
    if (syncProgressBar && syncProgressText) {
      syncProgressText.textContent = message;
      syncProgressText.style.color = isError ? '#d32f2f' : 'inherit';
      syncProgressBar.style.display = 'block';
    }
  }
  function hideSyncProgress() {
    if (syncProgressBar) syncProgressBar.style.display = 'none';
  }

  // ---------- WebSocket status (dacă e disponibil) ----------
  try {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws/status`);
    socket.onopen = () => console.log("WebSocket Connection Established.");
    socket.onmessage = e => {
      const d = JSON.parse(e.data);
      if (d.type === 'sync_end' || d.type === 'sync_error') {
        showSyncProgress((d.message || '') + " Reîncărcare...", d.type === 'sync_error');
        setTimeout(() => window.location.reload(), 2500);
      } else {
        showSyncProgress(d.message || '');
      }
    };
    socket.onerror = (error) => console.error("WebSocket Error:", error);
  } catch (error) {
    console.error("Failed to connect to WebSocket:", error);
  }

  // ===================== LOGICĂ PENTRU PAGINA PRINCIPALĂ DE COMENZI (/view) =====================
  const mainPageContainer = el('filterForm');
  if (mainPageContainer) {
    // ---- Sincronizare ----
    function startMainPageSync(url, body, options = {}) {
      fetch(url, { method: 'POST', body, ...options })
        .then(res => {
          if (res.status === 409) res.json().then(d => alert(d.detail || 'Sincronizare deja în desfășurare.'));
        })
        .catch(() => alert('Eroare la pornirea sincronizării.'));
    }
    el('syncOrdersButton')?.addEventListener('click', function() {
      const storeIds = Array.from(document.querySelectorAll('.store-checkbox:checked')).map(cb => parseInt(cb.value, 10));
      if (storeIds.length === 0) return alert('Te rog selectează cel puțin un magazin.');
      const days = parseInt(el('days-input').value, 10);
      startMainPageSync('/sync/orders', JSON.stringify({ store_ids: storeIds, days: days }), { headers: { 'Content-Type': 'application/json' } });
    });
    el('fullSyncButton')?.addEventListener('click', function() {
      const storeIds = Array.from(document.querySelectorAll('.store-checkbox:checked')).map(cb => parseInt(cb.value, 10));
      if (storeIds.length === 0) return alert('Te rog selectează cel puțin un magazin.');
      const days = parseInt(el('days-input').value, 10);
      startMainPageSync('/sync/full', JSON.stringify({ store_ids: storeIds, days: days }), { headers: { 'Content-Type': 'application/json' } });
    });
    el('syncCouriersButton')?.addEventListener('click', () => startMainPageSync('/sync/couriers', new FormData()));

    // ---- Filtre & sort ----
    let debounceTimeout;
    const submitFilterForm = () => { clearTimeout(debounceTimeout); mainPageContainer.submit(); };
    mainPageContainer.querySelectorAll('select, input[type="date"]').forEach(elm => elm.addEventListener('change', submitFilterForm));
    mainPageContainer.querySelectorAll('input[type="text"]').forEach(elm => elm.addEventListener('keyup', () => {
      clearTimeout(debounceTimeout);
      debounceTimeout = setTimeout(submitFilterForm, 500);
    }));

    // ---- Toggle detalii compact/detaliat ----
    const globalToggleButton = el('toggle-detailed-view');
    if (globalToggleButton) {
      globalToggleButton.addEventListener('click', () => {
        const isDetailed = globalToggleButton.dataset.state === 'detailed';
        const allDetailButtons = document.querySelectorAll('.details-toggle-btn');
        allDetailButtons.forEach(btn => {
          const detailsRow = el('details-' + btn.dataset.targetId);
          const isVisible = detailsRow && detailsRow.classList.contains('visible');
          if ((!isDetailed && !isVisible) || (isDetailed && isVisible)) {
            btn.click();
          }
        });
        globalToggleButton.textContent = isDetailed ? 'Vedere Detaliată' : 'Vedere Compactă';
        globalToggleButton.dataset.state = isDetailed ? 'compact' : 'detailed';
      });
    }

    // ---- Select all + print ----
    const selectAllCheckbox = el('selectAllCheckbox');
    const printButton = el('print-selected-button');
    const selectAllBanner = el('select-all-banner');

    function updateSelectedState() {
      if (!selectAllCheckbox) return;
      const checkedBoxes = document.querySelectorAll('.awb-checkbox:checked');
      const totalVisible = document.querySelectorAll('.awb-checkbox:not([disabled])').length;
      selectAllCheckbox.checked = checkedBoxes.length > 0 && checkedBoxes.length === totalVisible;

      const isSelectAllActive = el('select_all_filtered_confirm')?.value === 'true';
      let selectionText = ``;
      if (isSelectAllActive) {
        const total = selectAllBanner?.dataset.totalOrders;
        selectionText = `Toate cele <strong>${total}</strong> comenzi sunt selectate. <a href="#" id="unselect-all-link" style="margin-left:1rem;">Deselectează tot</a>`;
      } else if (checkedBoxes.length > 0 && checkedBoxes.length === totalVisible && totalVisible > 0) {
        const total = selectAllBanner?.dataset.totalOrders;
        selectionText = `Toate cele ${checkedBoxes.length} comenzi de pe pagină sunt selectate. <a href="#" id="select-all-filtered-link" style="margin-left:1rem;">Selectează toate cele ${total} comenzi</a>`;
      }
      if (selectAllBanner) {
        selectAllBanner.innerHTML = selectionText;
        selectAllBanner.style.display = selectionText ? 'block' : 'none';
      }
    }

    document.body.addEventListener('change', e => {
      if (e.target.matches('.awb-checkbox')) { updateSelectedState(); }
      if (e.target === selectAllCheckbox) {
        document.querySelectorAll('.awb-checkbox:not([disabled])').forEach(cb => cb.checked = e.target.checked);
        const selectAllConfirm = el('select_all_filtered_confirm');
        if (selectAllConfirm) selectAllConfirm.value = 'false';
        updateSelectedState();
      }
    });

    document.body.addEventListener('click', e => {
      if (e.target?.id === 'select-all-filtered-link' || e.target?.id === 'unselect-all-link') {
        e.preventDefault();
        const selectAllConfirm = el('select_all_filtered_confirm');
        if (selectAllConfirm) selectAllConfirm.value = e.target.id === 'select-all-filtered-link' ? 'true' : 'false';
        if (selectAllCheckbox) {
          const shouldBeChecked = e.target.id === 'select-all-filtered-link';
          selectAllCheckbox.checked = shouldBeChecked;
          document.querySelectorAll('.awb-checkbox:not([disabled])').forEach(cb => cb.checked = shouldBeChecked);
        }
        updateSelectedState();
      }
    });

    function sendPrintRequest(awbs) {
      if (!awbs || awbs.length === 0) return alert('Te rog selectează cel puțin un AWB.');
      const originalButtonText = "Printează AWB-urile Selectate";
      if (printButton) { printButton.textContent = "Se pregătește PDF..."; printButton.disabled = true; }
      let formData = new FormData();
      formData.append('awbs', awbs.join(','));

      fetch('/labels/merge_for_print', { method: 'POST', body: formData })
        .then(res => res.ok ? res.blob() : res.json().then(err => Promise.reject(new Error(err.detail || 'Eroare la generarea PDF-ului.'))))
        .then(blob => {
          const url = URL.createObjectURL(blob);
          const iframe = document.createElement('iframe');
          iframe.style.display = 'none';
          iframe.src = url;
          document.body.appendChild(iframe);
          iframe.onload = () => { try { iframe.contentWindow.print(); } catch (err) { alert("Eroare la deschiderea dialogului de printare. Dezactivați pop-up blocker-ul."); } };
        })
        .catch(err => alert(`Nu s-a putut genera documentul. Motiv: ${err.message}`))
        .finally(() => { if (printButton) { printButton.textContent = originalButtonText; printButton.disabled = false; } });
    }

    if (printButton) {
      printButton.addEventListener('click', function () {
        const selectAllConfirmInput = el('select_all_filtered_confirm');
        if (selectAllConfirmInput?.value === "true") {
          let params = new URLSearchParams(window.location.search);
          params.delete('page'); params.delete('sort_by');
          printButton.textContent = "Se încarcă AWB-urile...";
          printButton.disabled = true;
          fetch(`/get_awbs_for_filters?${params.toString()}`)
            .then(res => res.json()).then(data => sendPrintRequest(data.awbs))
            .catch(() => alert('A apărut o eroare la preluarea AWB-urilor.'))
            .finally(() => { printButton.textContent = "Printează AWB-urile Selectate"; printButton.disabled = false; });
        } else {
          const awbsToPrint = Array.from(document.querySelectorAll('.awb-checkbox:checked')).map(cb => cb.value);
          sendPrintRequest(awbsToPrint);
        }
      });
    }

    // ---- Paginație ----
    el('page-input')?.addEventListener('change', () => el('goToPageForm')?.submit());

    // ---- Validare adrese (batch) ----
    el('validateAddressesButton')?.addEventListener('click', () => {
      showSyncProgress('Inițiază validarea tuturor adreselor...');
      fetch('/sync/validate-addresses', { method: 'POST' })
        .then(r => r.json())
        .then(d => { showSyncProgress(d.message || 'Proces început.'); setTimeout(() => hideSyncProgress(), 5000); })
        .catch(err => { console.error(err); showSyncProgress('Eroare la pornirea procesului.', true); });
    });

    // set stare inițială banner
    updateSelectedState();
  }

  // ===================== /processing – modal Creează AWB (profile + auto) =====================
  const dlg = el('createAwbModal');
  if (dlg) {
    const form = el('createAwbForm');
    const submitBtn = el('submitAwbButton');

    // ---- Profiles (LocalStorage) ----
    const LS_PROFILES = 'awb_profiles_v1';
    const LS_LAST = 'awb_last_profile';
    const profileSelect = el('awbProfileSelect');

    const loadProfiles = () => { try { return JSON.parse(localStorage.getItem(LS_PROFILES) || '[]'); } catch { return []; } };
    const saveProfiles = (arr) => { try { localStorage.setItem(LS_PROFILES, JSON.stringify(arr)); } catch {} };
    const setLastProfile = (name) => { try { localStorage.setItem(LS_LAST, name || ''); } catch {} };
    const getLastProfile = () => { try { return localStorage.getItem(LS_LAST) || ''; } catch { return ''; } };

    const refreshProfileSelect = () => {
      const arr = loadProfiles();
      profileSelect.innerHTML = '<option value="">— (fără profil) —</option>';
      arr.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.name; opt.textContent = p.name;
        profileSelect.add(opt);
      });
      const last = getLastProfile();
      if (last && arr.find(p => p.name === last)) profileSelect.value = last;
    };

    const collectFormToProfile = (name) => ({
      name,
      courierKey: el('awbCourierKey').value || 'dpd-ro',
      service_id: intVal('awbServiceId'),
      parcels: intVal('awbParcelsCount', 1),
      weight_kg: floatVal('awbTotalWeight', 1.0),
      payer: el('awbPayer').value || 'SENDER',
      include_shipping_in_cod: el('awbIncludeShipInCod').checked,
      length_cm: intVal('awbLength'),
      width_cm: intVal('awbWidth'),
      height_cm: intVal('awbHeight'),
    });

    const applyProfileToForm = (p) => {
      if (!p) return;
      if (p.parcels != null) el('awbParcelsCount').value = p.parcels;
      if (p.weight_kg != null) el('awbTotalWeight').value = p.weight_kg;
      if (p.payer) el('awbPayer').value = p.payer;
      if (typeof p.include_shipping_in_cod === 'boolean') el('awbIncludeShipInCod').checked = p.include_shipping_in_cod;
      if (p.length_cm != null) el('awbLength').value = p.length_cm;
      if (p.width_cm != null) el('awbWidth').value = p.width_cm;
      if (p.height_cm != null) el('awbHeight').value = p.height_cm;
      // service_id îl aplicăm după ce opțiunile au fost încărcate
      const applyService = () => {
        const sel = el('awbServiceId');
        const opt = sel?.querySelector(`option[value="${p.service_id}"]`);
        if (opt) sel.value = String(p.service_id);
      };
      setTimeout(applyService, 50);
    };

    el('saveAwbProfileBtn')?.addEventListener('click', () => {
      const name = prompt('Numele profilului (ex: "DPD Standard"):');
      if (!name) return;
      const arr = loadProfiles();
      const idx = arr.findIndex(p => p.name === name);
      const prof = collectFormToProfile(name);
      if (idx >= 0) arr[idx] = prof; else arr.push(prof);
      saveProfiles(arr);
      setLastProfile(name);
      refreshProfileSelect();
      alert('Profil salvat.');
    });

    profileSelect?.addEventListener('change', () => {
      const arr = loadProfiles();
      const p = arr.find(x => x.name === profileSelect.value);
      if (p) { setLastProfile(p.name); applyProfileToForm(p); updateCodAuto(); }
      else { setLastProfile(''); }
    });

    // ---- Auto pickup date (frontend vizual; backend are fallback) ----
    const CUTOFF_HOUR = 16;
    const nextBusiness = () => {
      const now = new Date(); let d = new Date(now.getFullYear(), now.getMonth(), now.getDate());
      if (now.getHours() >= CUTOFF_HOUR) d.setDate(d.getDate() + 1);
      while ([6,0].includes(d.getDay())) d.setDate(d.getDate() + 1);
      return d.toISOString().slice(0,10);
    };

    // ---- Auto-COD din dataset-ul butonului din rând ----
    let userEditedCod = false;
    const updateCodAuto = () => {
      const btn = dlg._invokerBtn; if (!btn) return;
      const isPaid = (btn.dataset.paid || 'false') === 'true';
      const total = parseFloat(btn.dataset.total || '0') || 0;
      const ship = parseFloat(btn.dataset.shipping || '0') || 0;
      const include = el('awbIncludeShipInCod').checked;
      const autoCod = isPaid ? 0 : (include ? total : Math.max(total - ship, 0));
      const note = el('awbCodAutoNote');
      if (note) note.textContent = isPaid ? 'Comandă plătită online: COD 0' : `Sugestie: ${autoCod.toFixed(2)} RON`;
      if (!userEditedCod) {
        const codInput = el('awbCodAmount');
        codInput.value = isPaid ? '' : autoCod.toFixed(2);
      }
    };
    el('awbCodAmount')?.addEventListener('input', () => { userEditedCod = true; });
    el('awbIncludeShipInCod')?.addEventListener('change', () => updateCodAuto());

    // ---- Deschidere modal (delegare eveniment) ----
    document.body.addEventListener('click', (ev) => {
      const btn = ev.target.closest('.create-awb-btn');
      if (!btn) return;
      ev.preventDefault();

      el('awbOrderId').value = btn.dataset.orderId;
      el('awbCourierKey').value = btn.dataset.courierKey || 'dpd-ro';
      userEditedCod = false;

      // auto pickup date (vizual)
      const pd = el('awbPickupDate');
      pd.value = nextBusiness();
      const note = el('awbPickupNote');
      if (note) note.textContent = 'Propunere automată; poți lăsa gol, iar backend-ul va alege prima zi disponibilă.';

      // servicii disponibile
      const sel = el('awbServiceId');
      sel.innerHTML = '<option value="">Se încarcă...</option>'; sel.disabled = true;
      fetch(`/couriers/dpd/services?order_id=${encodeURIComponent(btn.dataset.orderId)}`)
        .then(r => r.ok ? r.json() : Promise.reject())
        .then(list => {
          sel.innerHTML = '';
          if (Array.isArray(list) && list.length) list.forEach(s => sel.add(new Option(`${s.id} - ${s.name}`, s.id)));
          else sel.add(new Option('2505 - DPD STANDARD', 2505));
          sel.disabled = false;
          // aplică profilul utilizat ultima dată
          const arr = loadProfiles(); const last = getLastProfile();
          const prof = arr.find(p => p.name === last);
          if (prof) applyProfileToForm(prof);
        })
        .catch(() => { sel.innerHTML = ''; sel.add(new Option('2505 - DPD STANDARD', 2505)); sel.disabled = false; });

      dlg._invokerBtn = btn; // pentru calcule COD
      updateCodAuto();

      dlg.showModal();
    });

    // close
    qa('[data-close-modal]').forEach(a => a.addEventListener('click', (e) => { e.preventDefault(); dlg.close(); }));
    q('.delete', dlg)?.addEventListener('click', (e) => { e.preventDefault(); dlg.close(); });

    // ---- Submit create AWB ----
    el('createAwbForm')?.addEventListener('submit', async (e) => {
      e.preventDefault();
      submitBtn.classList.add('is-loading'); submitBtn.disabled = true;

      const codRaw = el('awbCodAmount').value;
      const options = {
        service_id: intVal('awbServiceId'),
        parcels_count: intVal('awbParcelsCount', 1),
        total_weight: floatVal('awbTotalWeight', 1.0),
        length_cm: intVal('awbLength'),
        width_cm: intVal('awbWidth'),
        height_cm: intVal('awbHeight'),
        payer: el('awbPayer').value || 'SENDER',
        include_shipping_in_cod: el('awbIncludeShipInCod').checked
      };
      if (codRaw !== '') options.cod_amount = parseFloat(codRaw);
      const pd = el('awbPickupDate').value;
      if (pd) options.pickup_date = pd; // string YYYY-MM-DD

      const payload = {
        order_id: parseInt(el('awbOrderId').value, 10),
        courier_account_key: el('awbCourierKey').value || 'dpd-ro',
        options
      };

      try {
        const resp = await fetch('/actions/create-awb', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(payload)
        });
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.detail || 'Eroare necunoscută');
        }
        const data = await resp.json();
        alert(`AWB ${data.awb} a fost creat cu succes!`);
        dlg.close();
        window.location.reload();
      } catch (err) {
        alert(`Eroare: ${err.message || err}`);
      } finally {
        submitBtn.classList.remove('is-loading');
        submitBtn.disabled = false;
      }
    });

    // populate profiles dropdown on load
    (function initProfiles(){ try { refreshProfileSelect(); } catch(_){} })();
  }

});