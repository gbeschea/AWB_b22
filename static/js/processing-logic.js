/* processing-logic.js — v2.6 (compatible) */
(() => {
  // tiny helpers
  const q  = (sel, ctx=document) => ctx.querySelector(sel);
  const qa = (sel, ctx=document) => Array.from(ctx.querySelectorAll(sel));

  // modal & form
  const modal    = q('#awbModal');
  const form     = q('#awbForm');

  // triggers
  const btnBulk  = q('#bulkCreateAwbBtn');

  // fields (keep the original ids/names from your template)
  const inpOrderIds = q('#order_ids');
  const inpServiceId = q('#serviceId');
  const selProfile   = q('#shipmentProfile');           // <select name="shipment_profile_id" id="shipmentProfile">
  const selCourier   = q('#courierAccountKey');         // <select name="courier_account_key" id="courierAccountKey">
  const inpParcels   = q('#parcelsCount');              // <input name="parcels_count" id="parcelsCount">
  const inpWeight    = q('#totalWeight');               // <input name="total_weight" id="totalWeight">
  const inpCOD       = q('#codAmount');                 // <input name="cod_amount" id="codAmount">
  const selPayer     = q('#payer');                     // <select name="payer" id="payer">
  const inpThird     = q('#third_party_client_id');     // <input name="third_party_client_id" id="third_party_client_id"> (optional)
  const thirdWrap    = q('#third-party-wrap');          // wrapper shown only for THIRD_PARTY (optional)

  const btnCancel    = q('#cancelAwbModalBtn');

  // modal show/hide (uses .is-active like before)
  const showModal = () => { if (modal) modal.classList.add('is-active'); };
  const hideModal = () => { if (modal) modal.classList.remove('is-active'); };

  // profile data reader from <option data-*>
  const getSelectedProfileData = () => {
    if (!selProfile) return {};
    const opt = selProfile.selectedOptions && selProfile.selectedOptions[0];
    if (!opt) return {};
    const ds = opt.dataset || {};
    return {
      dk:   ds.courierKey || "",                 // courier account key
      sid:  ds.serviceId   || "",                // service id
      p:    ds.parcels     || "",                // default parcels
      w:    ds.weight      || "",                // default weight
      payer: (ds.payer || "").toUpperCase(),     // default payer
      incod: (ds.includeCod || "").toString().toLowerCase() === 'true',
      cod:   ds.cod || "",
      tpid:  ds.thirdPartyClientId || ds.thirdpartyclientid || ""  // from data-third-party-client-id
    };
  };

  // toggle THRID_PARTY UI
  const toggleThirdParty = () => {
    const isThird = (selPayer && (selPayer.value || '').toUpperCase() === 'THIRD_PARTY');
    if (thirdWrap) thirdWrap.style.display = isThird ? '' : 'none';
    if (inpThird)  inpThird.disabled = !isThird;
  };

  // apply defaults from selected profile
  const applyProfileDefaults = () => {
    const pd = getSelectedProfileData();

    // 1) preselect courier account based on profile (and relax required)
    if (selCourier) {
      if (pd.dk) {
        selCourier.value = pd.dk;
        selCourier.removeAttribute('required');
      } else {
        selCourier.setAttribute('required', 'required');
        if (!selCourier.value) selCourier.selectedIndex = 0;
      }
    }

    // 2) set hidden service_id
    if (inpServiceId && pd.sid) inpServiceId.value = pd.sid;

    // 3) defaults parcels/weight/payer
    if (inpParcels && pd.p)  inpParcels.value = pd.p;
    if (inpWeight  && pd.w)  inpWeight.value  = pd.w;
    if (selPayer   && pd.payer) {
      selPayer.value = pd.payer;
      toggleThirdParty();
    }

    // 4) COD defaults (if profile supplies)
    if (inpCOD && pd.cod) {
      try { inpCOD.value = parseFloat(pd.cod) || 0; } catch {}
    }

    // 5) third party client id default (if present)
    if (inpThird && pd.tpid) inpThird.value = pd.tpid;

    // 6) optional: show selected service name/ID
    const svcBadge = q('#selectedServiceName');
    if (svcBadge) svcBadge.textContent = pd.sid ? `Serviciu: #${pd.sid}` : 'Serviciu: —';
  };

  // collect selected order ids from list
  const gatherSelectedOrderIds = () =>
    qa('.order-checkbox:checked').map(cb => cb.value);

  // open modal for a set of ids
  const openModalForOrders = (orderIds) => {
    if (!inpOrderIds) return;
    try {
      inpOrderIds.value = JSON.stringify(orderIds || []);
    } catch {
      inpOrderIds.value = '[]';
    }
    // when opening, apply current profile defaults (if any)
    applyProfileDefaults();
    showModal();
  };

  // ——— Handlers ———
  document.addEventListener('click', (e) => {
    const tgt = e.target;

    // Row button (Creează AWB)
    if (tgt && tgt.classList && tgt.classList.contains('create-awb-btn')) {
      e.preventDefault();
      const tr = tgt.closest('tr');
      const orderId = tr?.dataset?.orderId;
      if (orderId) openModalForOrders([orderId]);
      else openModalForOrders([]);
    }

    // Bulk
    if (tgt && tgt.id === 'bulkCreateAwbBtn') {
      e.preventDefault();
      const ids = gatherSelectedOrderIds();
      if (!ids.length) {
        alert('Selectează cel puțin o comandă.');
        return;
      }
      openModalForOrders(ids);
    }
  });

  // profile change
  selProfile?.addEventListener('change', applyProfileDefaults);

  // payer change (toggle third-party UI)
  selPayer?.addEventListener('change', toggleThirdParty);
  toggleThirdParty();

  // cancel
  btnCancel?.addEventListener('click', (e) => { e.preventDefault(); hideModal(); });

  // submit
  form?.addEventListener('submit', async (e) => {
    e.preventDefault();

    const endpoint = form.dataset.endpoint || '/actions/create-awb';

    const ids = (() => {
      try { return JSON.parse(inpOrderIds?.value || '[]'); } catch { return []; }
    })();
    const isBulk = ids.length > 1;

    const pd = getSelectedProfileData();

    // courier key
    const courier_account_key = (pd.dk || selCourier?.value || '').trim();

    // service id (hidden or from profile)
    const rawSid = (inpServiceId?.value || pd.sid || '').toString().trim();
    const service_id = rawSid ? parseInt(rawSid, 10) : null;

    // numbers
    const parcels_count = parseInt(inpParcels?.value || '1', 10);
    const total_weight  = parseFloat(inpWeight?.value || '1');
    const cod_amount    = parseFloat(inpCOD?.value || '0');

    // payer
    const payer = (selPayer?.value || 'SENDER').toUpperCase();

    // profile id
    const shipment_profile_id = selProfile?.value ? parseInt(selProfile.value, 10) : null;

    // third party id
    const third_party_client_id = (payer === 'THIRD_PARTY')
      ? ((inpThird?.value || pd.tpid || '').trim())
      : '';

    // basic client-side validation (backend still validates)
    if (!courier_account_key && !shipment_profile_id) {
      alert('Selectează un cont de curier sau alege un profil care are cont configurat.');
      return;
    }

    const basePayload = {
      shipment_profile_id,
      courier_account_key,
      service_id,
      parcels_count,
      total_weight,
      cod_amount,
      payer,
      third_party_client_id
    };

    const payload = isBulk ? { ...basePayload, order_ids: ids } : { ...basePayload, order_id: ids[0] };

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      const text = await res.text();
      let data = {};
      try { data = JSON.parse(text); } catch {}

      if (!res.ok) {
        const msg = data?.detail || data?.message || text || res.statusText || 'Eroare necunoscută';
        throw new Error(msg);
      }

      console.log('[awb] success:', data);
      alert('AWB creat cu succes!');
      hideModal();
      location.reload();
    } catch (err) {
      console.error('[awb] submit error:', err);
      alert('Nu am putut genera AWB. ' + (err?.message || err));
    }
  });

  console.debug('[awb] v2.6 loaded');
})();
