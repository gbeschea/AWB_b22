#!/usr/bin/env python3
"""
Download a list of Shopify image URLs, rename them sequentially 101..149
(preserving extension), and zip them into images_101_149.zip

Usage (no deps beyond standard library):
    python grab_shopify_images_101_149.py
"""

import os
import sys
import time
import zipfile
from urllib.parse import urlparse
from urllib.request import Request, urlopen

# Ordered URLs (49 total => 101..149)
URLS = [
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/367260-media_swatch.avif?v=1752236094",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.10126.jpg?v=1752236239",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/emporio-stronger-with-you-amber___240912.webp?v=1752236402",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/119520-1.jpg?v=1752236465",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/698519_24869191596776732.jpg?v=1752236555",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/chrome-parfum___230714.webp?v=1752236608",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/obsession-for-men___130607.webp?v=1752237892",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.24787.avif?v=1752238125",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3614273852814.webp?v=1752238199",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3614274025637_60ML_y_l_elixir_main.jpg?v=1752238290",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/15217003-1855164187702019.jpg?v=1752238364",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.17124.avif?v=1752239021",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3274872456129_1920x1920_aede37b3-f3d0-43f8-8409-3d1ddf92a46f.avif?v=1752239066",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.686.avif?v=1752239151",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/563468m667kk7nccmhs_1280x1280_1a91dd7b-3ec0-440e-bef7-a389f606eaec.avif?v=1752239232",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/Libre-Absolu-Platine-50ml.webp?v=1752239311",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/211033-media_swatch.avif?v=1752239403",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/27550-media_swatch.avif?v=1752239584",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/chanel-coco-noir-100-ml-tester-original-1.jpg?v=1752240510",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.248.avif?v=1752240591",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/cacharel-amor_amor56ceccd92c393_1280x1280_fd7fbdb0-450f-4c30-802c-42f2c21941e5.avif?v=1752240637",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/8435415076814_1920x1920_936babf4-3d69-473e-940d-2f148d6cd808.avif?v=1752241717",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3614273961738_1920x1920_ee3b70ec-6cd2-4afc-b48f-4c5ea4dc237e.avif?v=1752241764",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.97458.avif?v=1752241889",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.96428.2x.avif?v=1752246900",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/352541-media_swatch.avif?v=1752247105",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/creed-1760-parfumuri-niche-queen-of-silk-75-ml-561ce81834a10489b4ff8b65_jpg.webp?v=1752247464",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/creed-1760-parfumuri-niche-centaurus-100-ml-d4decc1093561ab6ad4deba5_jpg.webp?v=1752247550",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3700550234678-1.jpg?v=1752247625",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.81490.2x.avif?v=1752247686",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.79608.2x.avif?v=1752247874",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/kl_sku_N0GD01_387x450_0.jpg?v=1752247988",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/375x500.6806.2x.avif?v=1752248077",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/364050-media_swatch.avif?v=1753339377",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/stronger-with-you-sandalwood-armani-1.webp?v=1753339488",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/dolce-gabbana-devotion-pour-homme-tester-edp.jpg?v=1753339632",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/344184-media_swatch.avif?v=1753339694",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/341685-media_swatch.avif?v=1753339752",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3274872481688_1_1920x1920_818c67f4-53c5-469e-b18c-32efd125a3c1.avif?v=1753339844",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3614274219579_1920x1920_09e82f1e-3ad4-4519-90eb-2e83b22a98a6.webp?v=1753339905",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/rabanne-parfum-phantom-elixir-parfum-intense-100-ml-f107e065b484d299b95bb4a4_jpg.webp?v=1753340045",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3274872479920_1_1920x1920_3f49123a-342b-45da-949c-22a19a8e5224.avif?v=1753340104",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/Image_Editor_1.png?v=1753340296",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/Image_Editor_2.png?v=1753340579",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/360708-media_swatch.webp?v=1753340710",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/3614274351019_1920x1920_c78da49c-4c2f-402a-9879-5c3736cbc6db.avif?v=1753340830",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/prd-front-87986-420x420.avif?v=1753340911",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/364127-media_swatch.avif?v=1753340985",
"https://cdn.shopify.com/s/files/1/0881/7275/7337/files/rabanne-apa-de-parfum-fame-the-couture-edition-80-ml-227c755258bc4dcda6bfd68d_jpg.webp?v=1753341036"
]

def get_ext(url: str) -> str:
    path = urlparse(url).path  # strips ?query automatically
    _, ext = os.path.splitext(path)
    return ext or ".bin"

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
    if len(URLS) != 49:
        print(f"WARNING: Expected 49 URLs (101..149). Got {len(URLS)}.")
    outdir = "images_101_149"
    os.makedirs(outdir, exist_ok=True)

    start_index = 101
    mapping = []

    for i, url in enumerate(URLS, start=start_index):
        ext = get_ext(url).lower()
        # normalize common CDN extensions
        if ext not in [".jpg", ".jpeg", ".png", ".webp", ".avif"]:
            # try to infer by URL fragments
            for cand in [".jpg", ".jpeg", ".png", ".webp", ".avif"]:
                if cand in url.lower():
                    ext = cand
                    break
        dest_name = f"{i}{ext}"
        dest_path = os.path.join(outdir, dest_name)
        print(f"[{i-100:02d}/49] Downloading -> {dest_name}")
        download(url, dest_path)
        mapping.append((url, dest_name))

    # Zip them
    zip_name = "images_101_149.zip"
    with zipfile.ZipFile(zip_name, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for _, fname in mapping:
            z.write(os.path.join(outdir, fname), arcname=fname)
    print(f"Done. Wrote {zip_name} with {len(mapping)} files.")
    # Also write a mapping txt
    with open("images_101_149_mapping.txt", "w", encoding="utf-8") as m:
        for url, fname in mapping:
            m.write(f"{fname}\t{url}\n")
    print("Mapping saved to images_101_149_mapping.txt")

if __name__ == "__main__":
    # Populate URLS from a separate file if someone pipes them via stdin
    main()
