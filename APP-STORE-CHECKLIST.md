# Order Hub — Shopify App Store readiness

Status of the store-submission requirements. ✅ = done in code, ☐ = needs a human action
(Partner Dashboard, assets, or a live host).

## Code / platform (mostly ✅)
- ✅ **Embedded app** — `embedded = true`, App Bridge loaded as the FIRST `<head>` script
  (`frontend/index.html`), Polaris UI, renders in the admin iframe (`frame-ancestors` CSP in `routes/spa.py`).
- ✅ **Session-token auth** — every `/api/*` call verified as an HS256 Shopify session-token JWT
  (`services/shopify_auth.py`).
- ✅ **OAuth install** — `routes/auth.py` (HMAC + signed state, code→token, encrypted at rest).
- ✅ **GraphQL only** — no REST Admin API usage (verified). API version `2026-04`.
- ✅ **Mandatory GDPR webhooks** — `customers/data_request`, `customers/redact`, `shop/redact`
  (HMAC-verified) declared in `shopify.app.toml` + handled in `routes/webhooks.py`.
- ✅ **Token encryption at rest** — AES-256-GCM (`crypto.py` / `encrypted_types.py`).
- ✅ **Billing** — Shopify Billing GraphQL (`services/shopify_billing.py`); Free + Pro, plan changeable
  in-app (`/app/billing`), reconcile-on-load, reinstall-proof trial (`services/app_ledger.py`).
- ✅ **Reinstall** — OAuth re-runs; `create_or_update_store` upsert re-activates; trial does NOT reset.
- ✅ **Health** — `/api/health`.

## Partner Dashboard / listing (☐ — human)
- ☐ **Register the app** in the Partner Dashboard → get `client_id` + secret → set env
  (`SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`) and fill `client_id` in `shopify.app.toml`.
- ☐ **App URLs** — set `application_url = https://<host>/app` and the OAuth redirect
  `https://<host>/auth/callback` (both in the toml, replace `REPLACE_WITH_APP_HOST`).
- ☐ **Protected Customer Data = Level 1** — the app PERSISTS customer fields (name, phone, shipping
  address on `orders`) to generate AWBs. Declare Level 1 + justification: "shipping address & contact
  are required to create courier waybills (AWB) and are redacted on `customers/redact`."
- ☐ **Scope justification** — `read/write_orders` + fulfillment scopes: "read orders to generate AWBs,
  write fulfillment + tracking back to Shopify." No `read_all_orders` (only recent orders needed).
- ☐ **Test credentials / instructions** — reviewer install steps + how to reach paid features
  (billing is in **test mode** via `SHOPIFY_BILLING_TEST` until you flip it, so a reviewer can subscribe
  without being charged).
- ☐ **Listing assets** — screenshots must show the actual embedded UI (no browser chrome, **no pricing
  in images**), 60–90s demo video, app icon (no Shopify trademark), app-card subtitle (no stats/claims).
- ☐ **Emergency developer contact** — Account settings.

## Before submit
- ☐ Deploy to the live HTTPS host; set all env vars (`AWB_B2_ENC_KEY`, `SHOPIFY_*`, `SESSION_SECRET`,
  `AWB_B2_CORS_ORIGINS`).
- ☐ Run the multi-tenancy migration (`f1a2b3c4d5e6`) on a DB **copy** first, then prod.
- ☐ `shopify app deploy --client-id=<id>` to register scopes + webhooks on the platform.
- ☐ Flip `SHOPIFY_BILLING_TEST=false` when ready to charge real money.
- ☐ Install on a dev store and walk every screen live (the first true end-to-end verification).
