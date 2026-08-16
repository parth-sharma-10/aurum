"""Download the pinned source datasets from Roboflow Universe.

Writes each dataset to data/raw/<key>/ in YOLO format, plus a
data/raw/<key>/_aurum_meta.json capturing the project metadata (license, image
count, per-class instance counts) exactly as the API reported it. DATA_SOURCES.md
is generated from those files, so the documented statistics cannot drift from
what was actually downloaded.

Usage:
    ROBOFLOW_API_KEY=... python -m ml.ingest
    ROBOFLOW_API_KEY=... python -m ml.ingest --only computer_parts
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "configs" / "datasets.yaml"
RAW = ROOT / "data" / "raw"

API = "https://api.roboflow.com"
EXPORT_FORMAT = "yolov11"  # Ultralytics-compatible YOLO txt + data.yaml


def _get_json(url: str, timeout: int = 60) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def fetch_metadata(workspace: str, project: str, key: str) -> dict:
    url = f"{API}/{workspace}/{project}?api_key={urllib.parse.quote(key)}"
    return (_get_json(url).get("project")) or {}


def request_export(
    workspace: str, project: str, version: int, key: str, attempts: int = 12, wait: int = 15
) -> str:
    """Ask Roboflow for a download link, waiting while it generates the export.

    A version that has never been exported in this format returns a 202-style
    'generating' response; polling is the documented way through it, so the
    retry lives here rather than in the caller's hands.
    """
    url = f"{API}/{workspace}/{project}/{version}/{EXPORT_FORMAT}?api_key={urllib.parse.quote(key)}"
    last = ""
    for i in range(attempts):
        try:
            data = _get_json(url)
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: {exc.read()[:200]!r}"
            time.sleep(wait)
            continue
        link = (data.get("export") or {}).get("link")
        if link:
            return link
        last = json.dumps(data)[:200]
        print(f"    export not ready ({i + 1}/{attempts}), waiting {wait}s…")
        time.sleep(wait)
    raise RuntimeError(
        f"{workspace}/{project} v{version}: no export link after {attempts} "
        f"attempts. Last response: {last}"
    )


def download_zip(link: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(link, timeout=900) as r:
        blob = r.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest)


def ingest_one(entry: dict, api_key: str, force: bool = False) -> dict:
    key = entry["key"]
    ws, proj, ver = entry["workspace"], entry["project"], entry["version"]
    dest = RAW / key
    meta_path = dest / "_aurum_meta.json"

    if meta_path.exists() and not force:
        print(f"  [{key}] already present, skipping (use --force to redo)")
        return json.loads(meta_path.read_text())

    print(f"  [{key}] {ws}/{proj} v{ver}")
    meta = fetch_metadata(ws, proj, api_key)
    link = request_export(ws, proj, ver, api_key)
    download_zip(link, dest)

    n_img = sum(
        len(list((dest / s / "images").glob("*")))
        for s in ("train", "valid", "test")
        if (dest / s / "images").is_dir()
    )

    record = {
        "key": key,
        "workspace": ws,
        "project": proj,
        "version": ver,
        "url": f"https://universe.roboflow.com/{ws}/{proj}",
        "name": meta.get("name"),
        "type": meta.get("type"),
        "license": meta.get("license"),
        "images_reported": meta.get("images"),
        "images_downloaded": n_img,
        "splits_reported": meta.get("splits"),
        "classes_reported": meta.get("classes"),
        "export_format": EXPORT_FORMAT,
        "downloaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": entry.get("note", "").strip(),
    }
    meta_path.write_text(json.dumps(record, indent=2))
    print(f"    -> {n_img} images, license={record['license']!r}")
    return record


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", action="append", help="ingest only these registry keys")
    ap.add_argument("--force", action="store_true", help="re-download even if present")
    args = ap.parse_args()

    api_key = os.environ.get("ROBOFLOW_API_KEY")
    if not api_key:
        print(
            "ROBOFLOW_API_KEY is not set. Get a free key at roboflow.com (Settings -> API Keys).",
            file=sys.stderr,
        )
        return 1

    entries = yaml.safe_load(REGISTRY.read_text())["roboflow"]
    if args.only:
        entries = [e for e in entries if e["key"] in set(args.only)]
        if not entries:
            print(f"no registry entries match {args.only}", file=sys.stderr)
            return 1

    print(f"Ingesting {len(entries)} dataset(s) into {RAW}")
    failures = []
    for entry in entries:
        try:
            ingest_one(entry, api_key, force=args.force)
        except Exception as exc:
            print(f"  !! {entry['key']} FAILED: {exc}", file=sys.stderr)
            failures.append(entry["key"])

    if failures:
        print(f"\n{len(failures)} dataset(s) failed: {failures}", file=sys.stderr)
        return 1
    print("\nAll datasets ingested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
