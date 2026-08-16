"""Sweep Roboflow Universe for datasets that actually contain Aurum classes.

Scores candidates by how many of {PCB, RAM, CPU, Connector} their class list
covers after Aurum label normalization, so a dataset called "e-waste" with only
{mouse, earbud} sinks and one called "components" with {ram, cpu} floats.

Usage:
    ROBOFLOW_API_KEY=... python scripts/search_universe.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ml.labels import load_label_map  # noqa: E402

API = "https://api.roboflow.com/universe/search"

QUERIES = [
    "ewaste",
    "e-waste",
    "e waste components",
    "ewaste detection",
    "electronic waste",
    "electronic components detection",
    "computer components",
    "pc components",
    "hardware components",
    "ram detection",
    "ram module",
    "memory module",
    "ddr ram",
    "cpu detection",
    "processor detection",
    "cpu ram",
    "motherboard components",
    "pcb detection",
    "circuit board detection",
    "connector detection",
    "recycling electronics",
    "scrap electronics",
    "class:ram",
    "class:cpu",
    "class:pcb",
    "class:connector",
    "class:motherboard",
    "class:processor",
]


def search(query: str, key: str) -> list[dict]:
    url = f"{API}?q={urllib.parse.quote(query)}&api_key={urllib.parse.quote(key)}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.load(r).get("results") or []
    except Exception as exc:  # network flake shouldn't kill a 28-query sweep
        print(f"  ! {query}: {exc}", file=sys.stderr)
        return []


def main() -> int:
    key = os.environ.get("ROBOFLOW_API_KEY")
    if not key:
        print("ROBOFLOW_API_KEY is not set", file=sys.stderr)
        return 1

    lm = load_label_map()
    targets = set(lm.classes)

    found: dict[str, dict] = {}
    hits_by_query: dict[str, list[str]] = defaultdict(list)

    for q in QUERIES:
        results = search(q, key)
        print(f"{q:34s} -> {len(results)}")
        for r in results:
            found.setdefault(r["url"], r)
            hits_by_query[q].append(r["url"])
        time.sleep(0.3)

    scored = []
    for url, r in found.items():
        covered: set[str] = set()
        unmapped: list[str] = []
        for c in r.get("classes") or []:
            try:
                aurum = lm.resolve(c)
            except KeyError:
                unmapped.append(c)
                continue
            if aurum:
                covered.add(aurum)
        if covered:
            scored.append((len(covered), r.get("images") or 0, url, r, sorted(covered), unmapped))

    scored.sort(key=lambda t: (-t[0], -t[1]))

    print("\n" + "=" * 100)
    print(f"{len(found)} unique datasets seen; {len(scored)} contain >=1 Aurum class")
    print("=" * 100)
    for n, imgs, url, r, covered, unmapped in scored[:40]:
        print(f"\n[{n}/{len(targets)}] {r['name']}  ({imgs} images, v{r.get('latestVersion')})")
        print(f"    {url}")
        print(f"    license: {r.get('license')}   type: {r.get('type')}")
        print(f"    covers : {covered}")
        print(f"    classes: {r.get('classes')}")
        if unmapped:
            print(f"    UNMAPPED (need review): {unmapped}")

    out = Path("reports/universe_search.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            [{"score": n, "covers": cov, "unmapped": un, **r} for n, _, _, r, cov, un in scored],
            indent=2,
        )
    )
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
