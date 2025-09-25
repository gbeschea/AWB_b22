#!/usr/bin/env python3
import argparse, json, sys
import requests

BASE_URL_DEFAULT = "https://api.dpd.ro/v1"

def pp_services(label, services):
    print(f"\n=== {label} — {len(services)} servicii ===")
    for s in services:
        sid = s.get("id") or s.get("service", {}).get("id")
        name = s.get("name") or s.get("service", {}).get("name")
        name_en = s.get("nameEn") or s.get("service", {}).get("nameEn")
        req_size = s.get("requireParcelSize")
        req_weight = s.get("requireParcelWeight")
        cargo = s.get("cargoType")
        print(f"- {sid}: {name} / {name_en} (cargo={cargo}, reqSize={req_size}, reqWeight={req_weight})")

def call_services(base_url, user, password):
    url = f"{base_url}/services"
    body = {"userName": user, "password": password}
    r = requests.post(url, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    services = data.get("services") or data
    if services is None:
        services = []
    return services

def call_destination_services(base_url, user, password, client_id, country_id=None, site_id=None, post_code=None, date=None):
    url = f"{base_url}/services/destination"
    if not (country_id or site_id or post_code):
        raise SystemExit("Provide at least one of: --country-id with --post-code, or --site-id")

    address_location = {}
    if site_id:
        address_location["siteId"] = site_id
    if country_id:
        address_location["countryId"] = country_id
    if post_code:
        address_location["postCode"] = post_code

    body = {
        "userName": user,
        "password": password,
    }
    if date:
        body["date"] = date

    body["sender"] = {"clientId": client_id}
    body["recipient"] = {"privatePerson": True, "addressLocation": address_location}

    r = requests.post(url, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()
    services = data.get("services") or data
    if services is None:
        services = []
    return services

def main():
    ap = argparse.ArgumentParser(description="Fetch DPD RO services and destination services")
    ap.add_argument("--base-url", default=BASE_URL_DEFAULT)
    ap.add_argument("--user", default="200925904")
    ap.add_argument("--password", default="6815745245")
    ap.add_argument("--client-id", type=int, required=True, help="Your DPD clientId (numeric)")
    ap.add_argument("--country-id", type=int, default=642, help="Destination countryId (e.g., 642 for Romania)")
    ap.add_argument("--post-code", type=str, default=None, help="Destination post code (e.g., 060274)")
    ap.add_argument("--site-id", type=int, default=None, help="Destination siteId (alternative to postCode)")
    ap.add_argument("--date", type=str, default=None, help="Pickup date YYYY-MM-DD (optional)")
    args = ap.parse_args()

    try:
        generic = call_services(args.base_url, args.user, args.password)
        pp_services("SERVICES (generic)", generic)
    except Exception as e:
        print(f"[WARN] /services failed: {e}", file=sys.stderr)

    try:
        dest = call_destination_services(
            args.base_url, args.user, args.password, args.client_id,
            country_id=args.country_id, site_id=args.site_id, post_code=args.post_code, date=args.date
        )
        pp_services("SERVICES (destination)", dest)
    except Exception as e:
        print(f"[ERROR] /services/destination failed: {e}", file=sys.stderr)
        sys.exit(2)

if __name__ == "__main__":
    main()