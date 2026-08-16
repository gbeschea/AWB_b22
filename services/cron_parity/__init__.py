"""
cron_parity — portul capabilităților cronului xConnector în Order Hub (duplicate, colete, surpriză,
COD capture), în mod SHADOW (log-only): detectoarele rulează pe comenzile sincronizate în DB-ul OH
și LOGHEAZĂ ce-ar face cronul, fără nicio scriere în Shopify. Gated de env CRON_PARITY_SHADOW=1.

Semantica de decizie e PARITATE cu xconnector.py (commit 10cdd69) — vezi fiecare modul. Acțiunile
outward (orderCancel / orderMarkAsPaid / tags / order-edit surpriză) se activează abia la cutover,
per capabilitate, refolosind mutațiile deja existente în services/shopify_service + order_edit.
"""
