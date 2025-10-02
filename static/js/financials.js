// static/js/financials.js

document.addEventListener('DOMContentLoaded', function() {
    const syncButton = document.getElementById('syncButton');
    const markAsPaidButton = document.getElementById('markAsPaidButton');
    const selectAllCheckbox = document.getElementById('selectAll');
    const orderCheckboxes = document.querySelectorAll('.order-checkbox');
    const courierFilter = document.getElementById('courierFilter');
    const statusFilter = document.getElementById('statusFilter');
    const ordersTable = document.getElementById('ordersTable').getElementsByTagName('tbody')[0];

    // Funcție pentru a afișa o notificare (folosind un simplu alert)
    function showNotification(message, type = 'info') {
        alert(`[${type.toUpperCase()}] ${message}`);
    }

    // Sincronizare comenzi
    syncButton.addEventListener('click', function() {
        const startDate = document.getElementById('startDate').value;
        const endDate = document.getElementById('endDate').value;
        
        if (!startDate || !endDate) {
            showNotification('Te rog selectează un interval de date.', 'warning');
            return;
        }

        const formData = new FormData();
        formData.append('start_date', new Date(startDate).toISOString());
        formData.append('end_date', new Date(endDate).toISOString());
        // Momentan, trimitem ID-ul primului magazin. Ideal, aici ar trebui un selector.
        formData.append('store_ids', 1); 

        showNotification('Sincronizarea a început...', 'info');
        
        fetch('/financials/sync-range', {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            if (data.status === 'success') {
                showNotification(data.message, 'success');
                setTimeout(() => window.location.reload(), 1500);
            } else {
                showNotification('A apărut o eroare la sincronizare.', 'error');
            }
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('Eroare de rețea. Verifică consola.', 'error');
        });
    });

    // Marcare ca plătit
    markAsPaidButton.addEventListener('click', function() {
        const selectedOrders = Array.from(orderCheckboxes)
            .filter(cb => cb.checked)
            .map(cb => cb.value);

        if (selectedOrders.length === 0) {
            showNotification('Te rog selectează cel puțin o comandă.', 'warning');
            return;
        }
        
        if (!confirm(`Ești sigur că vrei să marchezi ${selectedOrders.length} comenzi ca fiind plătite? Această acțiune va încerca să captureze plata în Shopify.`)) {
            return;
        }

        const formData = new FormData();
        selectedOrders.forEach(id => formData.append('order_ids', id));

        fetch('/financials/mark-as-paid', {
            method: 'POST',
            body: formData
        })
        .then(response => response.json())
        .then(data => {
            showNotification(data.message, 'success');
            if (data.failed_orders && data.failed_orders.length > 0) {
                let errorMessage = 'Următoarele comenzi au eșuat:\n';
                data.failed_orders.forEach(fail => {
                    errorMessage += `- ${fail.name}: ${fail.error}\n`;
                });
                showNotification(errorMessage, 'error');
            }
            setTimeout(() => window.location.reload(), 2000);
        })
        .catch(error => {
            console.error('Error:', error);
            showNotification('Eroare de rețea. Verifică consola.', 'error');
        });
    });

    // Selectare/Deselectare totală
    selectAllCheckbox.addEventListener('change', function() {
        orderCheckboxes.forEach(cb => {
            cb.checked = selectAllCheckbox.checked;
        });
    });
    
    // Filtrare
    function applyFilters() {
        const courierValue = courierFilter.value.toLowerCase();
        const statusValue = statusFilter.value.toLowerCase();
        
        for (const row of ordersTable.rows) {
            const courierCell = row.cells[5].textContent.toLowerCase().trim();
            const statusCell = row.cells[6].textContent.toLowerCase().trim();
            
            const courierMatch = !courierValue || courierCell.includes(courierValue);
            const statusMatch = !statusValue || statusCell.includes(statusValue);
            
            if (courierMatch && statusMatch) {
                row.style.display = '';
            } else {
                row.style.display = 'none';
            }
        }
    }
    
    courierFilter.addEventListener('change', applyFilters);
    statusFilter.addEventListener('input', applyFilters);
});