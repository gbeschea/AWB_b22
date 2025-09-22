#!/usr/bin/env python3
"""
Download images from a list of URLs and rename them using names provided
in a second text file (one name per line). Lines starting with '#' or empty
lines are ignored in both files. Order matters and counts must match.

Usage:
    python grab_images_with_names.py --urls urls.txt --names names.txt
    python grab_images_with_names.py --urls urls.txt --names names.txt --outdir imgs --zipname imgs.zip
    python grab_images_with_names.py --urls urls.txt --names names.txt --skip-existing

Notes:
- If a target name has no extension, the script appends the inferred image
  extension (.jpg/.png/.webp/.avif/.gif). If the name already includes an
  extension, it is preserved.
- Name collisions are resolved by appending _1, _2, ... to the base name.
- Outputs: folder with renamed images, a .zip with the same files, and
  a mapping.txt showing "final_filename<TAB>source_url".
"""

import argparse
import os
import re
import sys
import time
import zipfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SAFE_CHARS_RE = re.compile(r'[^a-zA-Z0-9._-]+')

def parse_args():
    p = argparse.ArgumentParser(description="Download and rename images using names from a text file.")
    p.add_argument("--urls", required=True, help="Path to .txt containing image URLs (one per line).")
    p.add_argument("--names", required=True, help="Path to .txt containing target names (one per line).")
    p.add_argument("--outdir", type=str, default=None, help="Output folder (default: images_named).")
    p.add_argument("--zipname", type=str, default=None, help="Zip file name (default: <outdir>.zip).")
    p.add_argument("--timeout", type=int, default=60, help="Per-request timeout in seconds (default: 60).")
    p.add_argument("--tries", type=int, default=3, help="Retry attempts per URL (default: 3).")
    p.add_argument("--skip-existing", action="store_true", help="Skip downloading if target file already exists.")
    return p.parse_args()

def read_clean_lines(path: str):
    if not os.path.isfile(path):
        sys.exit(f"File not found: {path}")
    items = []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            items.append(line)
    if not items:
        sys.exit(f"No usable lines found in: {path}")
    return items

def guess_ext_from_url(url: str):
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    if ext in (".jpg", ".jpeg"):
        return ".jpg"
    if ext in (".png", ".webp", ".avif", ".gif"):
        return ext
    # try to infer by fragments
    lower = url.lower()
    for cand in [".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"]:
        if cand in lower:
            return ".jpg" if cand == ".jpeg" else cand
    return ".jpg"  # safe default

def split_name_ext(name: str):
    base, ext = os.path.splitext(name)
    # treat .jpeg as .jpg for uniformity
    if ext.lower() == ".jpeg":
        ext = ".jpg"
    return base, ext

def sanitize_base(name_base: str):
    clean = SAFE_CHARS_RE.sub("-", name_base.strip())
    clean = clean.strip(" ._-")
    return clean or "file"

def ensure_unique_path(outdir: str, filename: str):
    base, ext = os.path.splitext(filename)
    candidate = filename
    idx = 1
    while os.path.exists(os.path.join(outdir, candidate)):
        candidate = f"{base}_{idx}{ext}"
        idx += 1
    return candidate

def download(url: str, dest_path: str, tries: int = 3, timeout: int = 60):
    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            req = Request(url, headers={"User-Agent": ua})
            with urlopen(req, timeout=timeout) as r, open(dest_path, "wb") as f:
                f.write(r.read())
            return
        except Exception as e:
            last_err = e
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Failed to download {url!r} -> {dest_path!r}: {last_err}")

def main():
    args = parse_args()
    urls = read_clean_lines(args.urls)
    names_raw = read_clean_lines(args.names)

    if len(urls) != len(names_raw):
        sys.exit(f"Count mismatch: {len(urls)} URLs vs {len(names_raw)} names. They must match.")

    outdir = args.outdir or "images_named"
    os.makedirs(outdir, exist_ok=True)

    mapping = []
    total = len(urls)

    for idx, (url, raw_name) in enumerate(zip(urls, names_raw), start=1):
        base, name_ext = split_name_ext(raw_name)
        base = sanitize_base(base)
        ext = name_ext.lower() if name_ext else guess_ext_from_url(url)
        final_name = f"{base}{ext}"
        final_name = ensure_unique_path(outdir, final_name)
        dest_path = os.path.join(outdir, final_name)

        print(f"[{idx:02d}/{total}] {raw_name} -> {final_name}")
        if args.skip_existing and os.path.exists(dest_path):
            print("  - exists, skipping download")
        else:
            download(url, dest_path, tries=args.tries, timeout=args.timeout)

        mapping.append((final_name, url))

    zipname = args.zipname or f"{outdir}.zip"
    with zipfile.ZipFile(zipname, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for fname, _ in mapping:
            z.write(os.path.join(outdir, fname), arcname=fname)

    map_path = os.path.join(outdir, "mapping.txt")
    with open(map_path, "w", encoding="utf-8") as m:
        for fname, url in mapping:
            m.write(f"{fname}\t{url}\n")

    print(f"Done. Wrote {zipname} with {len(mapping)} files.")
    print(f"Mapping saved to {map_path}")
    print(f"Output folder: {outdir}")

if __name__ == "__main__":
    main()
