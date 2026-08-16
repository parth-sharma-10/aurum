"""Build a real-world spot-check set from Wikimedia Commons.

The held-out test split shares its provenance with training: same Roboflow
projects, same photographers, same benches. Good numbers there do not prove the
model survives a different camera and a different table.

Commons is a genuinely different source. This script pulls freely-licensed
photographs of the four Aurum classes, records each file's license and author
for attribution, and — importantly — perceptually hashes every download against
the training split, because "unseen" has to be verified rather than assumed.

Nothing here is annotated, so the result is evidence to look at with
`python -m ml.realworld`, not a metric.

Usage:
    python scripts/fetch_realworld.py
    python scripts/fetch_realworld.py --per-query 6
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "data" / "realworld"
TRAIN = ROOT / "data" / "aurum" / "train" / "images"

API = "https://commons.wikimedia.org/w/api.php"

# Wikimedia's robot policy wants a descriptive agent with a contact route and a
# request rate a human could plausibly produce. Both are honoured below: a
# throttle between downloads and backoff on 429 rather than hammering through it.
UA = ("AurumVision/0.1 (https://github.com/; SIH e-waste research prototype; "
      "contact: parthrsharma1002@gmail.com) Python-urllib")
DOWNLOAD_DELAY = 1.5

# Queries chosen to span the four classes and, deliberately, awkward conditions:
# bare boards, installed modules, pin-side processors, connector banks.
QUERIES = {
    "PCB": ["printed circuit board motherboard", "computer motherboard top view"],
    "RAM": ["DDR4 DIMM memory module", "SDRAM memory module"],
    "CPU": ["CPU processor LGA package", "microprocessor pins underside"],
    "Connector": ["motherboard connector header", "pin header connector board"],
}

ALLOWED = ("cc0", "cc-by", "cc by", "public domain", "publicdomain", "cc-zero")


def api(params: dict) -> dict:
    params = {**params, "format": "json"}
    url = f"{API}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def download(url: str, dest: Path, attempts: int = 4) -> bool:
    """Fetch one file, backing off when Commons asks us to slow down."""
    for i in range(attempts):
        time.sleep(DOWNLOAD_DELAY)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                dest.write_bytes(r.read())
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and i < attempts - 1:
                wait = 5 * (2 ** i)
                print(f"    · rate-limited, waiting {wait}s")
                time.sleep(wait)
                continue
            print(f"    ! {dest.name}: HTTP {exc.code}", file=sys.stderr)
            return False
        except Exception as exc:
            print(f"    ! {dest.name}: {exc}", file=sys.stderr)
            return False
    return False


def search(query: str, limit: int) -> list[dict]:
    try:
        data = api({
            "action": "query", "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}", "gsrnamespace": 6,
            "gsrlimit": limit * 3, "prop": "imageinfo",
            "iiprop": "url|extmetadata|size", "iiurlwidth": 1280,
        })
    except Exception as exc:
        print(f"  ! search failed for {query!r}: {exc}", file=sys.stderr)
        return []
    return list((data.get("query") or {}).get("pages", {}).values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--per-query", type=int, default=4)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    seen_titles: set[str] = set()

    for aurum_class, queries in QUERIES.items():
        for q in queries:
            print(f"[{aurum_class}] {q}")
            kept = 0
            for page in search(q, args.per_query):
                if kept >= args.per_query:
                    break
                title = page.get("title", "")
                if title in seen_titles:
                    continue
                info = (page.get("imageinfo") or [{}])[0]
                meta = info.get("extmetadata") or {}
                lic = (meta.get("LicenseShortName", {}).get("value") or "").lower()
                if not any(a in lic for a in ALLOWED):
                    continue
                url = info.get("thumburl") or info.get("url")
                # Commons appends utm_* tracking params, so test the path only.
                path = urllib.parse.urlparse(url).path.lower() if url else ""
                if not path.endswith((".jpg", ".jpeg", ".png")):
                    continue

                safe = title.replace("File:", "").replace(" ", "_")
                safe = "".join(c for c in safe if c.isalnum() or c in "._-")[:80]
                if not safe.lower().endswith((".jpg", ".jpeg", ".png")):
                    safe += Path(path).suffix
                dest = OUT / f"{aurum_class}__{safe}"
                if not download(url, dest):
                    continue

                seen_titles.add(title)
                kept += 1
                manifest.append({
                    "file": dest.name,
                    "expected_class_hint": aurum_class,
                    "title": title,
                    "descriptionurl": info.get("descriptionurl"),
                    "license": meta.get("LicenseShortName", {}).get("value"),
                    "artist": _strip_html(meta.get("Artist", {}).get("value", "")),
                    "query": q,
                })
                print(f"    + {dest.name}  [{manifest[-1]['license']}]")

    if not manifest:
        print("No images retrieved.", file=sys.stderr)
        return 1

    # --- verify these are genuinely unseen ---------------------------------
    overlaps = []
    if TRAIN.is_dir():
        import imagehash
        from PIL import Image
        print("\nChecking downloads against the training split…")

        def ph(p):
            with Image.open(p) as im:
                return int(str(imagehash.phash(im.convert("RGB"))), 16)

        train_hashes = []
        for p in TRAIN.iterdir():
            if p.suffix.lower() in {".jpg", ".jpeg", ".png"}:
                try:
                    train_hashes.append((p.name, ph(p)))
                except Exception:
                    pass
        for m in manifest:
            try:
                h = ph(OUT / m["file"])
            except Exception:
                continue
            for tn, th in train_hashes:
                if bin(h ^ th).count("1") <= 5:
                    overlaps.append({"realworld": m["file"], "train": tn})
                    break
        print(f"  {len(train_hashes)} training images compared; "
              f"{len(overlaps)} overlap(s) found")

    out = {
        "source": "Wikimedia Commons",
        "retrieved_for": "real-world spot check of Aurum Vision v0.1",
        "n_images": len(manifest),
        "license_filter": "CC0 / CC BY / Public Domain only",
        "overlap_with_training": overlaps,
        "caveat": (
            "These images are NOT annotated. Running the model over them "
            "produces detections to inspect, not an accuracy measurement."
        ),
        "images": manifest,
    }
    (ROOT / "reports" / "realworld_sources.json").write_text(json.dumps(out, indent=2))
    print(f"\n{len(manifest)} images -> {OUT}")
    print("Provenance -> reports/realworld_sources.json")
    print(f"\nNext: python -m ml.realworld --path {OUT}")
    return 0


def _strip_html(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s).strip()


if __name__ == "__main__":
    raise SystemExit(main())
