"""
fetch_reports.py
----------------
Scrapes the FIFA World Cup 2026 Match Report Hub for Post Match Summary Report
(PMSR) PDFs, downloads any that haven't been processed yet, and writes a small
manifest describing each one (teams, stage, match number, date) so the rest of
the pipeline doesn't have to re-parse that out of the PDF itself.

Run on a schedule (cron / GitHub Actions, see workflow.yml) — 2-3x/day is
plenty; FIFA publishes the PMSR shortly after each match ends.

Usage:
    python fetch_reports.py --out-dir ./incoming --state seen_reports.json
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HUB_URL = "https://www.fifatrainingcentre.com/en/fifa-world-cup-2026/match-report-hub.php"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; fifa-wc-pipeline/1.0)"}


def load_state(path: Path) -> set:
    if path.exists():
        return set(json.loads(path.read_text()).get("seen", []))
    return set()


def save_state(path: Path, seen: set):
    path.write_text(json.dumps({"seen": sorted(seen)}, ensure_ascii=False, indent=2))


def find_pmsr_links(hub_html: str, base_url: str):
    """
    Returns a list of dicts: {url, label} for every link on the hub page whose
    text or href suggests a Post Match Summary Report PDF.

    The hub's exact markup may change between matchdays — this looks for any
    <a> tag pointing at a .pdf whose surrounding text mentions "Post Match
    Summary" / "PMSR", which is robust to layout tweaks (the parser prompt
    backend benefits from the same kind of layout tolerance — see README).
    """
    soup = BeautifulSoup(hub_html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        label = " ".join(a.get_text(" ", strip=True).split())
        context = label + " " + href
        if re.search(r"post.?match.?summary|pmsr", context, re.I):
            links.append({"url": urljoin(base_url, href), "label": label or href.rsplit("/", 1)[-1]})
    return links


def safe_filename(url: str, label: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", label).strip("_") or url.rsplit("/", 1)[-1]
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub-url", default=HUB_URL)
    ap.add_argument("--out-dir", default="./incoming")
    ap.add_argument("--state", default="seen_reports.json")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state)
    seen = load_state(state_path)

    resp = requests.get(args.hub_url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    links = find_pmsr_links(resp.text, args.hub_url)

    new_files = []
    for link in links:
        if link["url"] in seen:
            continue
        fname = safe_filename(link["url"], link["label"])
        dest = out_dir / fname
        try:
            pdf_resp = requests.get(link["url"], headers=HEADERS, timeout=60)
            pdf_resp.raise_for_status()
            dest.write_bytes(pdf_resp.content)
        except requests.RequestException as e:
            print(f"[warn] failed to download {link['url']}: {e}", file=sys.stderr)
            continue
        seen.add(link["url"])
        new_files.append(str(dest))
        print(f"[new] {link['label']} -> {dest}")

    save_state(state_path, seen)

    # Print machine-readable summary so the orchestrator (pipeline.py) can
    # pick up exactly the new files without re-scanning the directory.
    print(json.dumps({"new_files": new_files}))


if __name__ == "__main__":
    main()
