#!/usr/bin/env python3
"""
run_address_validation.py
CLI pentru rularea validatorului de adrese în afara sync-ului.

Exemple rapide:
  # 1) Un singur test manual (fără DB, nu salvează nimic)
  python run_address_validation.py manual \
    --county "București" --city "București Sector 1" \
    --street "C A Rosetti 39" --zip 014345

  # 2) Revalidează câteva comenzi după id (din DB). Nu salvează în DB.
  python run_address_validation.py orders --ids 12345 12346 12347

  # 3) Revalidează toate comenzile marcate invalid/partial/not_found, până la 1000, și salvează în DB
  python run_address_validation.py orders \
    --invalid-only --limit 1000 --batch-size 500 --commit-every 500 --save

Setează DATABASE_URL în env sau folosește --db pentru a indica conexiunea.
Implicit importă validatorul din "address_service". Dacă ai alt fișier, folosește --module.
"""
import argparse
import asyncio
import csv
import json
import os
import re
from types import SimpleNamespace
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# ---- Config implicită ----
DEFAULT_DB_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/awb_hub"
)
# poți seta: export ADDR_VALIDATOR_MODULE=address_service_v8_3
DEFAULT_VALIDATOR_MODULE = os.getenv("ADDR_VALIDATOR_MODULE", "address_service")

def import_validator(module_name: str):
    import importlib
    mod = importlib.import_module(module_name)
    if not hasattr(mod, "validate_address_for_order"):
        raise RuntimeError(f"Modulul {module_name} nu expune validate_address_for_order(db, order).")
    return mod.validate_address_for_order

def colorful_status(s: str) -> str:
    try:
        import sys
        if not sys.stdout.isatty():
            return s
        colors = {"valid":"\033[92m","partial_match":"\033[93m","invalid":"\033[91m","not_found":"\033[95m"}
        reset = "\033[0m"
        return f"{colors.get(s,'')}{s}{reset}"
    except Exception:
        return s

# ------- DB plumbing --------
def make_session_factory(db_url: str):
    engine = create_async_engine(db_url, echo=False, future=True)
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ------- Models import --------
def import_models():
    import importlib
    return importlib.import_module("models")

async def validate_orders(
    db=None,
    validate_fn=None,
    invalid_only: bool = False,
    limit: Optional[int] = None,
    save: bool = False,
    quiet: bool = True,
    progress_every: int = 50,
    commit_every: int = 500,
    batch_size: int = 500,
    session_factory=None,
    csv_out=None,
    ids=None,
    **_kwargs,
):
    """
    Rulează validarea cu paginare stabilă pe ID (id DESC) ca să evităm loturi repetate.
    """

    from sqlalchemy import select, or_, desc, func
    try:
        from scripts import models
    except ImportError:
        import models

    def _parse_ids(raw):
        if raw is None:
            return None
        if isinstance(raw, (list, tuple, set)):
            out = []
            for x in raw:
                try: out.append(int(x))
                except: pass
            return out or None
        if isinstance(raw, str):
            parts = [p.strip() for p in re.split(r"[,\s]+", raw) if p.strip()]
            out = []
            for p in parts:
                try: out.append(int(p))
                except: pass
            return out or None
        try:
            return [int(raw)]
        except:
            return None

    ids_list = _parse_ids(ids)

    async def _count_candidates(db_session) -> int:
        stmt = select(func.count()).select_from(models.Order)
        if invalid_only:
            stmt = stmt.where(
                or_(
                    models.Order.address_status.is_(None),
                    models.Order.address_status.in_(["invalid", "not_found", "partial_match"]),
                )
            )
        if ids_list:
            stmt = stmt.where(models.Order.id.in_(ids_list))
        total = (await db_session.execute(stmt)).scalar_one()
        return min(total, int(limit)) if limit is not None else total

    async def _run_one_batch(db_session, to_process: Optional[int], bookmark_id: Optional[int]):
        current_limit = min(batch_size, int(to_process)) if to_process is not None else batch_size

        stmt = select(models.Order).order_by(desc(models.Order.id))
        if invalid_only:
            stmt = stmt.where(
                or_(
                    models.Order.address_status.is_(None),
                    models.Order.address_status.in_(["invalid", "not_found", "partial_match"]),
                )
            )
        if ids_list:
            stmt = stmt.where(models.Order.id.in_(ids_list))
        if bookmark_id is not None:
            stmt = stmt.where(models.Order.id < bookmark_id)

        stmt = stmt.limit(current_limit)
        orders = (await db_session.execute(stmt)).scalars().all()
        total = len(orders)
        if total == 0:
            return 0, bookmark_id  # nimic nou

        # procesează batch-ul
        rows_for_csv = []
        for idx, o in enumerate(orders, start=1):
            await validate_fn(db_session, o)
            if csv_out:
                rows_for_csv.append({
                    "id": o.id,
                    "status": getattr(o, "address_status", None),
                    "score": getattr(o, "address_score", None),
                    "errors": json.dumps(getattr(o, "address_validation_errors", None), ensure_ascii=False),
                    "suggestions": json.dumps(getattr(o, "address_suggestions", None), ensure_ascii=False),
                })
            if save and (idx % commit_every == 0):
                await db_session.commit()
            if quiet:
                if (idx % progress_every == 0) or (idx == total):
                    print(f"[batch {idx}/{total}] validate{' + saved' if save else ''}", flush=True)
            else:
                print(f"- {getattr(o, 'name', o.id)}: {getattr(o, 'address_status', 'n/a')} ({getattr(o, 'address_score', 0)})", flush=True)

        if save:
            await db_session.commit()
        if csv_out and rows_for_csv:
            header = ["id","status","score","errors","suggestions"]
            write_header = not os.path.exists(csv_out)
            with open(csv_out, "a", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=header)
                if write_header:
                    w.writeheader()
                w.writerows(rows_for_csv)

        # bookmark: următorul batch ia id < min_id din batch-ul curent
        new_bookmark = min(o.id for o in orders)
        return total, new_bookmark

    processed_total = 0

    if db is not None:
        total_candidates = await _count_candidates(db)
        print(f"Starting address validation… candidates: {total_candidates}", flush=True)
        if total_candidates == 0:
            print("Nimic de validat (0).", flush=True)
            return

        remaining = int(limit) if limit is not None else total_candidates  # IMPORTANT
        bookmark_id = None
        while True:
            done, bookmark_id = await _run_one_batch(db, remaining, bookmark_id)
            if done == 0:
                break
            processed_total += done
            if remaining is not None:
                remaining -= done
                if remaining <= 0:
                    break

    elif session_factory is not None:
        async with session_factory() as _db_count:
            total_candidates = await _count_candidates(_db_count)
        print(f"Starting address validation… candidates: {total_candidates}", flush=True)
        if total_candidates == 0:
            print("Nimic de validat (0).", flush=True)
            return

        remaining = int(limit) if limit is not None else total_candidates  # IMPORTANT
        bookmark_id = None
        while True:
            async with session_factory() as _db:
                done, bookmark_id = await _run_one_batch(_db, remaining, bookmark_id)
            if done == 0:
                break
            processed_total += done
            if remaining is not None:
                remaining -= done
                if remaining <= 0:
                    break
    else:
        raise RuntimeError("validate_orders: nici 'db' și nici 'session_factory' nu au fost furnizate.")

    print(f"Done. {processed_total} orders processed.", flush=True)


async def validate_manual(validate_fn, county, city, street, zip_code=None, name="MANUAL"):
    o = SimpleNamespace(
        name=name,
        shipping_province=county,
        shipping_city=city,
        shipping_address1=street,
        shipping_address2="",
        shipping_zip=zip_code or "",
        address_status=None,
        address_score=None,
        address_validation_errors=None,
    )
    class DummyDB:
        async def execute(self, *a, **kw): raise RuntimeError("Manual mode requires DB for nomenclator queries.")
        async def commit(self): pass
    db = DummyDB()

    print("\n[INFO] În modul 'manual' fără DB, se vor executa doar părțile care nu depind de DB.")
    try:
        await validate_fn(db, o)
    except Exception as e:
        print(f"[NOTE] Validatorul are nevoie de DB pentru nomenclator. Mesaj: {e}")
        print("Sugestie: folosește modul 'orders' cu un DB real sau pornește un DB local de test.")
    finally:
        print("\nRezultat parțial:")
        print(f"  nume:   {o.name}")
        print(f"  county: {county}")
        print(f"  city:   {city}")
        print(f"  street: {street}")
        print(f"  zip:    {zip_code or ''}")
        print(f"  status: {o.address_status}")
        print(f"  score:  {o.address_score}")
        print(f"  errors: {o.address_validation_errors}")

def build_parser():
    p = argparse.ArgumentParser(description="Rulează validatorul de adrese (în afara sync-ului).")
    p.add_argument("--db", default=DEFAULT_DB_URL, help="DATABASE_URL (postgresql+asyncpg://...)")
    p.add_argument("--module", default=DEFAULT_VALIDATOR_MODULE, help="Modulul din care importăm validatorul (ex: address_service sau address_service_v8_3).")

    sub = p.add_subparsers(dest="cmd", required=True)
    po = sub.add_parser("orders", help="Validează adresele din tabela Orders.")
    po.add_argument("--ids", nargs="*", type=int, help="Listă de id-uri de comenzi (separat prin spațiu).")
    po.add_argument("--invalid-only", action="store_true", help="Selectează doar comenzile cu status invalid/not_found/partial_match.")
    po.add_argument("--limit", type=int, default=100, help="Câte comenzi se selectează cel mult (când nu dai --ids).")
    po.add_argument("--save", action="store_true", help="Face commit în DB cu noile câmpuri address_status/score/errors.")
    po.add_argument("--csv-out", help="Scrie rezultatele într-un CSV (opțional).")
    po.add_argument("--batch-size", type=int, default=500, help="Dimensiunea lotului procesat într-o iterație (default 500).")
    po.add_argument("--commit-every", type=int, default=500, help="Commit la fiecare N comenzi (default 500).")
    po.add_argument("--progress-every", type=int, default=50, help="Log progres la fiecare N comenzi (default 50).")

    pm = sub.add_parser("manual", help="Testează rapid o adresă (fără DB real).")
    pm.add_argument("--county", required=True)
    pm.add_argument("--city", required=True)
    pm.add_argument("--street", required=True, help="Ex: 'C A Rosetti 39' sau 'Str. C.A. Rosetti nr 39'")
    pm.add_argument("--zip", dest="zip_code")

    return p

async def main_async():
    args = build_parser().parse_args()
    validate_fn = import_validator(args.module)

    if args.cmd == "orders":
        session_factory = make_session_factory(args.db)
        await validate_orders(
            session_factory=session_factory,
            validate_fn=validate_fn,
            ids=args.ids,
            invalid_only=args.invalid_only,
            limit=args.limit,
            save=args.save,
            csv_out=args.csv_out,
            batch_size=args.batch_size,
            commit_every=args.commit_every,
            progress_every=args.progress_every,
        )
    elif args.cmd == "manual":
        await validate_manual(
            validate_fn=validate_fn,
            county=args.county,
            city=args.city,
            street=args.street,
            zip_code=args.zip_code,
        )

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
