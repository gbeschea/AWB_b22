{% extends "_layout.html" %}
{% block title %}Procesare AWB{% endblock %}

{% block head_extra %}
<style>
  .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background-color: rgba(10,10,10,0.8); align-items: center; justify-content: center; }
  .modal.is-active { display: flex; }
  .modal-content { background-color: #fff; padding: 2rem; border-radius: 6px; width: 90%; max-width: 900px; max-height: 90vh; overflow-y: auto; }
  fieldset { border: 1px solid #ddd; padding: 1rem; margin-bottom: 1rem; border-radius: 4px; }
  legend { font-weight: bold; padding: 0 .5rem; }
  .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }
  .parcel-row { display: grid; grid-template-columns: 40px repeat(4, 1fr) 40px; gap: .5rem; align-items: center; margin-bottom: .5rem;}
</style>
{% endblock %}

{% block content %}
<div class="container">
  <h1 class="title">Procesare comenzi</h1>
  <div class="box is-shadowless" style="display:flex; justify-content:flex-end;">
    <button class="button is-info" id="bulkCreateAwbBtn">Generează AWB pentru Selecție</button>
  </div>
  <table class="table is-fullwidth is-striped is-hoverable">
    <thead>
        <tr>
            <th><input type="checkbox" id="selectAllOrders"></th>
            <th>Comanda</th>
            <th>Profil Asignat</th>
            <th>Acțiuni</th>
        </tr>
    </thead>
    <tbody>
        {% for order in orders %}
        <tr data-order-id="{{ order.id }}"
            data-order-name="{{ order.name }}"
            data-cod-amount="{{ order.total_price if order.financial_status == 'pending' else '0.0' }}"
            data-dpd-client-id="{{ order.store.dpd_client_id if order.store and order.store.dpd_client_id else '' }}"
            data-order-note="{{ order.note | replace('"', '&quot;') if order.note else '' }}">
            <td><input type="checkbox" name="order_ids" value="{{ order.id }}"></td>
            <td>{{ order.name }}</td>
            <td>{% if order.assigned_profile %}<span class="tag">{{ order.assigned_profile.name }}</span>{% else %}N/A{% endif %}</td>
            <td><button class="button is-small is-primary open-single-awb-modal" data-order-id="{{ order.id }}">Creează AWB</button></td>
        </tr>
        {% else %}
        <tr><td colspan="4" style="text-align:center;">Nu există comenzi pentru procesare.</td></tr>
        {% endfor %}
    </tbody>
  </table>
</div>

<div id="awbModal" class="modal">
  <div class="modal-content">
    <h2 id="awbModalTitle">Configurare AWB DPD</h2>
    <form id="awbForm">
      <input type="hidden" id="selectedOrderIds" name="order_ids">
      
      <fieldset>
        <div class="grid-3">
            <div><label>Profil Șablon</label><select id="profileSelect"><option value="">-- Fără profil --</option></select></div>
            <div><label>Cont Curier*</label><select id="courierAccount" name="account_key" required>{% for acc in courier_accounts %}<option value="{{ acc.account_key }}">{{ acc.name }}</option>{% endfor %}</select></div>
        </div>
      </fieldset>
      
      <details open>
        <summary><strong>Expeditor, Conținut și Colete</strong></summary>
        <fieldset>
          <div class="grid-3">
            <div><label>DPD Client ID (Sender)*</label><input type="text" id="dpdClientId" name="sender[clientId]" required></div>
            <div><label>Ref. 1 (Order ID)</label><input type="text" name="ref1" value="${orderName}" maxlength="30"></div>
            <div><label>Ref. 2</label><input type="text" name="ref2" maxlength="30"></div>
            <div><label>ID Oficiu Drop-off</label><input type="number" name="sender[dropoffOfficeId]"></div>
            <div><label>Format Conținut</label><input type="text" name="content[contents]" value="${orderName}" maxlength="30"></div>
            <div><label>Observații Livrare</label><textarea name="note" maxlength="200" rows="1"></textarea></div>
          </div>
          <hr>
          <label>Colete</label>
          <div id="parcelsContainer">
            <div class="parcel-row">
              <strong>#1:</strong>
              <input type="number" name="content[parcels][0][weight]" placeholder="Greutate (kg)*" step="0.1" required>
              <input type="number" name="content[parcels][0][size][width]" placeholder="Lățime (cm)">
              <input type="number" name="content[parcels][0][size][height]" placeholder="Înălțime (cm)">
              <input type="number" name="content[parcels][0][size][depth]" placeholder="Adâncime (cm)">
              <button type="button" class="remove-parcel-btn" style="visibility:hidden;">X</button>
            </div>
          </div>
          <button type="button" id="addParcelBtn" class="secondary outline" style="width: auto;">Adaugă Colet</button>
        </fieldset>
      </details>

      <details>
        <summary><strong>Servicii și Opțiuni de Livrare</strong></summary>
        <fieldset>
          <div class="grid-3">
            <div><label>Serviciu DPD*</label><select id="dpdServiceId" name="service[serviceId]" required><option value="">-- Încarcă --</option></select><button type="button" id="loadServicesBtn" class="secondary outline">Încarcă Servicii</button></div>
            <div><label>ID Oficiu Pickup (Locker)</label><input type="number" name="recipient[pickupOfficeId]"></div>
          </div>
          <div class="grid-3">
            <label><input type="checkbox" name="service[saturdayDelivery]" value="true"> Livrare Sâmbăta</label>
            <div><label>Oră Fixă Livrare</label><input type="number" name="service[fixedTimeDelivery]" placeholder="Ex: 1130"></div>
            <div><label>Amânare Livrare (zile)</label><input type="number" name="service[deferredDays]" placeholder="Ex: 1"></div>
          </div>
        </fieldset>
      </details>

      <details>
        <summary><strong>Servicii Adiționale (Ramburs, SWAP, etc.)</strong></summary>
        <fieldset>
          <div class="grid-3">
            <div><label>Sumă Ramburs</label><input type="number" id="codAmount" name="service[additionalServices][cod][amount]" step="0.01"></div>
            <div><label>Valoare Declarată</label><input type="number" name="service[additionalServices][declaredValue][amount]" step="0.01"></div>
            <label><input type="checkbox" name="service[additionalServices][declaredValue][fragile]" value="true"> Fragil</label>
            <label><input type="checkbox" name="service[additionalServices][obpd][option]" value="OPEN"> Deschidere la Livrare</label>
            <div><label>SWAP Service ID</label><input type="number" name="service[additionalServices][swap][serviceId]"></div>
            <div><label>SWAP Nr. Colete</label><input type="number" name="service[additionalServices][swap][parcelsCount]"></div>
          </div>
        </fieldset>
      </details>
      
      <div class="modal-actions">
        <button type="button" class="secondary" id="cancelAwbModalBtn">Anulează</button>
        <button type="submit" id="generateAwbBtn">Generează AWB</button>
      </div>
    </form>
  </div>
</div>
{% endblock %}

{% block scripts %}
    <script src="{{ url_for('static', path='js/processing.js') }}"></script>
{% endblock %}