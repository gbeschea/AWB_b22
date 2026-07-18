# AWB Hub — Master Plan: Shopify-ready · Refactored · Efficient · Lightweight

Goal: turn this crufty **single-tenant internal** FastAPI app into a **lean, efficient,
multi-tenant, Shopify-installable SaaS**. Live in production (awb.arona.ro) → every change is
backward-compatible, verified by import + tests, and **deployed by the user**, never auto-pushed.

North star (from the owner): **(1) Shopify-ready · (2) refactored · (3) technologically efficient · (4) light / runs smoothly.**

---

## ✅ DONE

### Security hardening (agent pass)
- AES-256-GCM encrypt-at-rest for `Store.access_token`, `Store.shared_secret`, `CourierAccount.credentials`
  via transparent `EncryptedString`/`EncryptedJSON` column types (`crypto.py`, `encrypted_types.py`).
  Legacy plaintext reads pass through; new writes encrypt. Migration `e7b3f9a1c2d5` (DO-NOT-RUN header).
- CORS: removed invalid `allow_origins=["*"]`+credentials → env allowlist `AWB_B2_CORS_ORIGINS` (dev localhost fallback).
- 3 mandatory GDPR webhooks: `customers/data_request`, `customers/redact`, `shop/redact` (HMAC-verified).
- Fail-closed: app refuses to boot without `AWB_B2_ENC_KEY`.

### Shopify-ready (Phase 1a)
- `shopify.app.toml` — standalone app (`embedded=false`, login-via-Shopify), scopes derived from code
  (`read/write_orders` + fulfillment + `read_products`), OAuth redirect, GDPR privacy webhooks declared.
- `routes/auth.py` — real OAuth **install → consent → callback → token** flow (HMAC + signed/cookie-bound
  state, code→token exchange, store upsert, webhook registration, session cookie). **Replaces the manual
  "paste domain + token" form.** `crud.create_or_update_store` upsert (idempotent on reinstall; token
  encrypted at rest). Wired in `main.py`, import-verified.

### Refactor Phase 1 (mechanical, low-risk) + efficiency quick wins
- Removed junk: `100MB`, `compară`/`compará` (0-byte), `services/address_service v1.py`,
  `config/sameday_id_delivered copy.json`, 3 dead plaintext courier secret files, `arhiva_printuri/` (25 MB
  runtime output untracked). Sanitized a real DB password out of `.env.example`.
- `main.py`: killed duplicate imports + **double-registered routers** (couriers settings/data + financials
  were each included twice) → routes **83 → 69**, zero functional loss.
- DB engine: `pool_pre_ping=True`, `pool_size=10`, `max_overflow=20`, `pool_recycle=1800` (stale-connection
  resilience against remote PG = the biggest "runs smoothly" fix).
- Dropped unused `pypdf` from requirements (PyPDF2 is the one imported).

---

## 🔜 REMAINING (phased)

### Phase 1b — lightweight / runs easily
- **Slim multi-stage Dockerfile** (`python:3.12-slim` + `uv`, non-root, `--no-cache`) + `.dockerignore`
  (exclude `.venv`, `templates` cache, fonts if unused, `scripts/*.csv`). Fast cold start, small image.
- Move `tqdm` (script-only progress bars) out of the web runtime deps if not imported by the app.
- Confirm CPU-bound PDF **merge** (`PdfMerger`) isn't blocking the event loop; if inline in an async
  handler, wrap in `run_in_threadpool`.

### Phase 2 — structural refactor (needs tests first)
- **Characterization tests** for pure logic (`crypto` round-trip + legacy passthrough, `address_service`
  validator, courier `base`/factory, HMAC verify) — the safety net for everything below. (Currently **zero tests**.)
- **Courier route de-dup** — `routes/couriers_profiles_full.py` is a newer partial-rewrite that **collides**
  with `routes/couriers.py` on `""`, `/profiles/{id}/edit`, `/profiles/{id}/delete`; whichever loads first
  shadows the other (latent bug). Pick the authoritative impl, merge into ONE module, delete the shadowed dup.
- **Fix `routes/financials.py`** — `mark-as-paid` queries non-existent columns → 500 + marks the wrong orders.
- **Split `models.py`** into a `models/` package (store · order · courier · shipment · validation) — optional, cosmetic.
- Config side-effects: `settings.py` loads JSON at import time; make lazy/explicit. Drop `"MODIFICARE"` scars.

### Phase 3 — multi-tenancy (the big one)
- `Account`/workspace model above `Store`; session-auth guard so the app isn't wide-open (today: **no auth at all**).
- Scope every query by account; make `ShipmentProfile` per-account (currently globally unique → breaks on tenant #2).
- Data migration for the existing single tenant → one Account.

### Phase 4 — App Store readiness
- External billing (Stripe or Shopify managed), Partner Dashboard: PCD **Level 1** (persists customer PII),
  test creds, listing assets, emergency contact. Register the app + fill `client_id` in the toml.

---

## Operating rules
- Backward-compatible only; verify with `python -c "import main"` (+ tests once they exist) after each change.
- No deploy, no prod migration, no `git push` without explicit OK. Migrations tested on a DB **copy** first.
- Secrets only via env / secret store — never in git.
