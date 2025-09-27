console.log("✅ Fișierul processing.js v4 (Payload-First) a fost încărcat!");

document.addEventListener('DOMContentLoaded', function() {
    // --- Definirea elementelor DOM ---
    const bulkCreateBtn = document.getElementById('bulkCreateAwbBtn');
    const awbModal = document.getElementById('awbModal');
    const awbForm = document.getElementById('awbForm');

    if (!bulkCreateBtn || !awbModal || !awbForm) {
        console.error("❌ Elemente esențiale lipsesc. Scriptul nu poate rula.");
        return;
    }

    const openModal = () => awbModal.classList.add('is-active');
    const closeModal = () => awbModal.classList.remove('is-active');

    /**
     * Funcția cheie: Transformă datele din formular într-un obiect JSON ierarhic.
     * Citește atributele `name` de forma "object[key][subkey]"
     */
    function serializeFormToJSON(form) {
        const formData = new FormData(form);
        const obj = {};

        for (const [key, value] of formData.entries()) {
            if (value === '' || value === null) continue; // Ignoră câmpurile goale

            // Convertește 'true'/'false' din checkbox-uri
            let processedValue = value;
            if (value === 'true') processedValue = true;
            if (value === 'false') processedValue = false;
            
            // Verifică dacă cheia indică o ierarhie (ex: "service[additionalServices][cod][amount]")
            if (key.includes('[')) {
                const keys = key.match(/[^[\]]+/g); // Extrage cheile: ['service', 'additionalServices', 'cod', 'amount']
                keys.reduce((acc, currentKey, index) => {
                    const isLast = index === keys.length - 1;
                    const isArray = /^\d+$/.test(keys[index + 1]);

                    if (isLast) {
                        acc[currentKey] = processedValue;
                    } else {
                        if (!acc[currentKey]) {
                            acc[currentKey] = isArray ? [] : {};
                        }
                    }
                    return acc[currentKey];
                }, obj);
            } else {
                obj[key] = processedValue;
            }
        }
        return obj;
    }
    
    /** Deschide modalul și îl pregătește cu datele comenzii. */
    function setupAndShowModal(orderIds) {
        if (orderIds.length === 0) { return alert('Selectează o comandă.'); }
        awbForm.reset();
        document.getElementById('selectedOrderIds').value = orderIds.join(',');
        
        const orderRow = document.querySelector(`tr[data-order-id="${orderIds[0]}"]`);
        if (orderRow) {
            const { dpdClientId, orderName, orderNote } = orderRow.dataset;
            if (dpdClientId) document.querySelector('[name="sender[clientId]"]').value = dpdClientId;
            if (orderName) document.querySelector('[name="ref1"]').value = orderName;
            if (orderNote) document.querySelector('[name="note"]').value = orderNote;
        }
        openModal();
    }

    // --- Event Listeners ---
    bulkCreateBtn.addEventListener('click', () => {
        const selectedIds = Array.from(document.querySelectorAll('input[name="order_ids"]:checked')).map(cb => cb.value);
        setupAndShowModal(selectedIds);
    });

    document.querySelectorAll('.open-single-awb-modal').forEach(button => {
        button.addEventListener('click', function() {
            setupAndShowModal([this.dataset.orderId]);
        });
    });

    awbForm.addEventListener('submit', function(event) {
        event.preventDefault();
        const payload = serializeFormToJSON(this);
        console.log("Payload DPD generat:", JSON.stringify(payload, null, 2));
        alert('Payload-ul a fost generat și afișat în consolă (F12). Verifică dacă structura este corectă.');
        
        // Aici vei face fetch către backend cu acest payload
        // fetch('/labels/generate-dpd-awb', { method: 'POST', body: JSON.stringify(payload), ... })
        closeModal();
    });
    
    // Logica pentru adăugarea dinamică a coletelor
    let parcelCount = 1;
    document.getElementById('addParcelBtn').addEventListener('click', function() {
        const index = parcelCount++;
        const newParcelRow = document.createElement('div');
        newParcelRow.className = 'parcel-row';
        newParcelRow.innerHTML = `
            <strong>#${index + 1}:</strong>
            <input type="number" name="content[parcels][${index}][weight]" placeholder="Greutate (kg)*" step="0.1" required>
            <input type="number" name="content[parcels][${index}][size][width]" placeholder="Lățime (cm)">
            <input type="number" name="content[parcels][${index}][size][height]" placeholder="Înălțime (cm)">
            <input type="number" name="content[parcels][${index}][size][depth]" placeholder="Adâncime (cm)">
            <button type="button" class="remove-parcel-btn">X</button>
        `;
        document.getElementById('parcelsContainer').appendChild(newParcelRow);
    });
    
    // Logica pentru ștergerea coletelor
    document.getElementById('parcelsContainer').addEventListener('click', function(e) {
        if (e.target.classList.contains('remove-parcel-btn')) {
            e.target.parentElement.remove();
        }
    });

    // Închidere modal
    document.getElementById('closeAwbModalBtn').addEventListener('click', closeModal);
    document.getElementById('cancelAwbModalBtn').addEventListener('click', closeModal);
    document.addEventListener('keydown', (event) => { if (event.key === 'Escape') closeModal(); });
});