/* processing-logic.js — v2.5 */
(() => {
  const q = (sel, ctx=document) => ctx.querySelector(sel);
  const qa = (sel, ctx=document) => [...ctx.querySelectorAll(sel)];

  const modal = q('#awbModal');
  const form  = q('#awbForm');
  const btnBulk = q('#bulkCreateAwbBtn');

  const inpOrderIds = q('#order_ids');
  const inpServiceId = q('#serviceId');
  const selProfile = q('#shipmentProfile');
  const selCourier = q('#courierAccountKey');
  const inpParcels  = q('#parcelsCount');
  const inpWeight   = q('#totalWeight');
  const inpCOD      = q('#codAmount');
  const selPayer    = q('#payer');

  const btnCancel  = q('#cancelAwbModalBtn');

  const showModal = () => modal.classList.add('is-active');
  const hideModal = () => modal.classList.remove('is-active');

  const getSelectedProfileData = () => {
    const opt = selProfile.selectedOptions && selProfile.selectedOptions[0];
    if (!opt) return {};
    return {
      dk:   opt.dataset.courierKey || "",        // courier account key din profil
      sid:  opt.dataset.serviceId   || "",       // service id din profil
      p:    opt.dataset.parcels     || "",       // default parcels
      w:    opt.dataset.weight      || "",       // default weight
      payer: opt.dataset.payer      || "",       // default payer
      incod: (opt.dataset.includeCod || "").toString().toLowerCase() === 'true',
      cod:   opt.dataset.cod || ""
    };
  };

  const applyProfileDefaults = () => {
    const pd = getSelectedProfileData();

    // 1) preselectează curierul din profil (și scoate required dacă e setat)
    if (pd.dk) {
      selCourier.value = pd.dk;
      selCourier.removeAttribute('required');
    } else {
      // dacă profilul NU are cont, lăsăm required pe selectul de curier
      selCourier.setAttribute('required', 'required');
      if (!selCourier.value) {
        // asigură-te că rămâne “– Selectează –” când profilul nu are cont
        selCourier.selectedIndex = 0;
      }
    }

    // 2) setează service_id în inputul ascuns
    if (pd.sid) inpServiceId.value = pd.sid;

    // 3) valori implicite pachete/greutate/plătitor
    if (pd.p)     inpParcels.value = pd.p;
    if (pd.w)     inpWeight.value  = pd.w;
    if (pd.payer) selPayer.value   = pd.payer;

    // 4) COD din profil (dacă există) – altfel nu atinge
    if (pd.cod) {
      try { inpCOD.value = parseFloat(pd.cod) || 0; } catch (_) {}
    }
  };

  const gatherSelectedOrderIds = () =>
    qa('.order-checkbox:checked').map(cb => cb.value);

  const openModalForOrders = (orderIds) => {
    console.debug('[awb] modal open for ids:', orderIds);
    inpOrderIds.value = JSON.stringify(orderIds);
    // când se deschide, aplică profilul curent dacă e ales deja
    applyProfileDefaults();
    showModal();
  };

  // ——— Handlers ———
  document.addEventListener('click', (e) => {
    // buton din rând „Creează AWB”
    if (e.target && e.target.classList.contains('create-awb-btn')) {
      const tr = e.target.closest('tr');
      const orderId = tr?.dataset?.orderId;
      openModalForOrders([orderId]);
    }

    // buton „AWB Bulk”
    if (e.target && e.target.id === 'bulkCreateAwbBtn') {
      const ids = gatherSelectedOrderIds();
      if (!ids.length) {
        alert('Selectează cel puțin o comandă.');
        return;
      }
      openModalForOrders(ids);
    }
  });

  // pe schimbarea profilului – aplică default-urile
  selProfile?.addEventListener('change', applyProfileDefaults);

  // anulare modal
  btnCancel?.addEventListener('click', hideModal);

  // submit formular
  form?.addEventListener('submit', async (e) => {
    e.preventDefault();

    const endpoint = form.dataset.endpoint || '/actions/create-awb';
    const ids = (() => {
      try { return JSON.parse(inpOrderIds.value || '[]'); } catch { return []; }
    })();

    // SINGLE vs BULK
    const isBulk = ids.length > 1;

    const pd = getSelectedProfileData();

    // 1) determină courier_account_key (din profil sau din select)
    const courier_account_key = (pd.dk || selCourier.value || '').trim();

    // 2) determină service_id (din input, din profil, sau gol)
    const rawSid = (inpServiceId.value || pd.sid || '').toString().trim();
    const service_id = rawSid ? parseInt(rawSid, 10) : null;

    // 3) restul câmpurilor
    const parcels_count = parseInt(inpParcels.value || '1', 10);
    const total_weight  = parseFloat(inpWeight.value || '1');
    const cod_amount    = parseFloat(inpCOD.value || '0');
    const payer         = (selPayer.value || 'SENDER').toUpperCase();

    // 4) profil
    const shipment_profile_id = selProfile.value ? parseInt(selProfile.value, 10) : null;

    // Validări minime pe client (nu blochează fallback-ul din backend)
    if (!courier_account_key && !shipment_profile_id) {
      alert('Selectează un cont de curier sau alege un profil care are cont configurat.');
      return;
    }

    const basePayload = {
      shipment_profile_id,
      courier_account_key,   // poate fi "" — backend o va completa din profil
      service_id,            // poate fi null — backend o va completa din profil
      parcels_count,
      total_weight,
      cod_amount,
      payer
    };

    let payload;
    if (isBulk) {
      payload = { ...basePayload, order_ids: ids };
      console.debug('[awb] submit bulk →', payload);
    } else {
      payload = { ...basePayload, order_id: ids[0] };
      console.debug('[awb] submit single →', payload);
    }

    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const t = await res.text();
        console.error('[awb] submit error:', t || res.statusText);
        throw new Error(t || res.statusText);
      }

      const data = await res.json().catch(() => ({}));
      console.log('[awb] success:', data);
      alert('AWB creat cu succes!');
      hideModal();
      location.reload(); // dacă vrei să vezi AWB-ul atașat în listă
    } catch (err) {
      console.error('[awb] submit error:', err);
      alert('Nu am putut genera AWB. Vezi consola pentru detalii.');
    }
  });

  console.debug('[awb] v2.5 loaded');
})();
