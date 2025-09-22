#!/usr/bin/env python3
"""
AVIF converter: AVIF -> PNG (default) or JPG.
Requires Python 3.8+ and one of:
  - pillow-heif (recommended):  pip install pillow pillow-heif
  - pillow-avif-plugin:         pip install pillow pillow-avif-plugin

Usage examples:
  python avif_to_png.py /path/to/file.avif
  python avif_to_png.py /path/to/folder -r
  python avif_to_png.py /path/to/folder -r --to jpg --quality 92

On macOS/Windows the wheels include libheif, so no extra system deps.
On some Linux distros you may need: sudo apt-get install libheif1 libheif-dev
"""
import argparse
import sys
from pathlib import Path
from typing import Iterable, Tuple

# Try to register an AVIF opener for Pillow.
PIL_AVIF_READY = False
try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
    PIL_AVIF_READY = True
except Exception:
    try:
        import pillow_avif  # type: ignore  # noqa: F401 (auto-registers)
        PIL_AVIF_READY = True
    except Exception:
        PIL_AVIF_READY = False

from PIL import Image  # type: ignore

AVIF_EXTS = {".avif", ".avifs"}

def find_inputs(path: Path, recursive: bool) -> Iterable[Path]:
    if path.is_file():
        yield path
    else:
        pattern = "**/*" if recursive else "*"
        for p in path.glob(pattern):
            if p.is_file() and p.suffix.lower() in AVIF_EXTS:
                yield p

def out_name(in_path: Path, out_dir: Path, to_ext: str) -> Path:
    return out_dir / (in_path.stem + to_ext)

def convert_one(src: Path, dst: Path, to_fmt: str, jpg_quality: int, overwrite: bool) -> Tuple[bool, str]:
    if dst.exists() and not overwrite:
        return False, f"SKIP (exists): {dst.name}"
    try:
        with Image.open(src) as im:
            # Preserve ICC if present
            icc = im.info.get("icc_profile")
            if to_fmt == "PNG":
                save_kwargs = {"optimize": True}
                if icc: save_kwargs["icc_profile"] = icc
                im.save(dst, format="PNG", **save_kwargs)
            else:
                # For JPG, ensure no alpha channel
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    im = im.convert("RGB")
                save_kwargs = {"quality": jpg_quality, "optimize": True, "progressive": True}
                if icc: save_kwargs["icc_profile"] = icc
                im.save(dst, format="JPEG", **save_kwargs)
        return True, f"OK: {src.name} -> {dst.name}"
    except Exception as e:
        return False, f"ERROR {src.name}: {e}"

def main():
    if not PIL_AVIF_READY:
        print("No AVIF plugin registered for Pillow.\n"
              "Install one of:\n"
              "  pip install pillow pillow-heif   # recommended\n"
              "  pip install pillow pillow-avif-plugin\n", file=sys.stderr)
        sys.exit(2)

    ap = argparse.ArgumentParser(description="Convert AVIF/AVIFS images to PNG (default) or JPG.")
    ap.add_argument("input", type=str, help="AVIF file or directory")
    ap.add_argument("-o", "--outdir", type=str, default="converted_images", help="Output directory (default: ./converted_images)")
    ap.add_argument("-r", "--recursive", action="store_true", help="Recurse into subfolders when input is a directory")
    ap.add_argument("--to", choices=["png", "jpg"], default="png", help="Output format (png/jpg)")
    ap.add_argument("--quality", type=int, default=95, help="JPEG quality (when --to jpg)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing files")
    args = ap.parse_args()

    src_path = Path(args.input).expanduser().resolve()
    out_dir = Path(args.outdir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    to_fmt = "PNG" if args.to.lower() == "png" else "JPEG"
    to_ext = ".png" if to_fmt == "PNG" else ".jpg"

    targets = list(find_inputs(src_path, args.recursive))
    if not targets:
        print("No AVIF files found.", file=sys.stderr)
        sys.exit(1)

    ok = 0
    for src in targets:
        dst = out_name(src, out_dir, to_ext)
        success, msg = convert_one(src, dst, to_fmt, args.quality, args.overwrite)
        print(msg)
        if success: ok += 1

    print(f"Done: {ok}/{len(targets)} converted to {to_fmt} in {out_dir}")

if __name__ == "__main__":
    main()
