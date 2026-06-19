"""
load_to_supabase.py
--------------------
Takes a result.json produced by either fifa_wc backend
(parse_fifa_report_prompt.md via Claude, or parse_fifa_report.py) and
upserts it into the Supabase `matches` table defined in schema.sql.

Auth: uses the SERVICE ROLE key (never the anon key) since RLS only grants
public SELECT — writes must come from this trusted pipeline.

Usage:
    export SUPABASE_URL=https://your-project.supabase.co
    export SUPABASE_SERVICE_ROLE_KEY=...
    python load_to_supabase.py result.json --backend prompt
    python load_to_supabase.py result.json --backend programmatic
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def require_env():
    missing = [n for n, v in [("SUPABASE_URL", SUPABASE_URL), ("SUPABASE_SERVICE_ROLE_KEY", SERVICE_KEY)] if not v]
    if missing:
        print(f"[error] missing env vars: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)


def to_row(result: dict, source_file: str, backend: str) -> dict:
    """Map result.json's top-level shape (see README) onto the matches columns."""
    meta = result.get("_meta", {})
    match = result.get("match", {})
    score = match.get("score", {})

    return {
        "home_team_name": match.get("home_team_name"),
        "away_team_name": match.get("away_team_name"),
        "home_score": score.get("home"),
        "away_score": score.get("away"),
        "competition": match.get("competition", "FIFA World Cup 2026"),
        "stage": match.get("stage"),
        "match_number": match.get("match_number"),
        "match_date": match.get("date"),
        "kickoff": match.get("kickoff"),
        "venue": match.get("venue"),
        "report_type": match.get("report_type", "Post Match Summary Report"),
        "source_file": meta.get("source_file", source_file),
        "skipped_pages": meta.get("skipped_pages", []),
        "pages_processed": meta.get("pages_processed", []),
        "pages": result.get("pages", {}),
        "raw_result": result,
        "parser_backend": backend,
        "status": "complete",
    }


def upsert_match(row: dict):
    """
    Upserts on match_number (unique index in schema.sql), so re-running the
    pipeline on the same report (e.g. FIFA re-publishes a corrected PMSR)
    overwrites rather than duplicates.
    """
    url = f"{SUPABASE_URL}/rest/v1/matches"
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=representation",
    }
    params = {"on_conflict": "match_number"} if row.get("match_number") is not None else None
    resp = requests.post(url, headers=headers, params=params, json=row, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(f"Supabase upsert failed [{resp.status_code}]: {resp.text}")
    return resp.json()


def mark_error(source_file: str, error_message: str, match_number=None):
    """Best-effort: record a failed parse so it's visible without digging through logs."""
    require_env()
    row = {
        "home_team_name": "Unknown", "away_team_name": "Unknown",
        "source_file": source_file, "status": "error", "error_message": str(error_message)[:2000],
    }
    if match_number is not None:
        row["match_number"] = match_number
    try:
        upsert_match(row)
    except Exception as e:
        print(f"[warn] could not record error row: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("result_json", help="Path to result.json from either parser backend")
    ap.add_argument("--backend", choices=["prompt", "programmatic"], default="programmatic")
    args = ap.parse_args()

    require_env()
    path = Path(args.result_json)
    result = json.loads(path.read_text())
    row = to_row(result, source_file=path.name, backend=args.backend)

    if not row["home_team_name"] or not row["away_team_name"]:
        print(f"[error] {path}: result.json missing match.home_team_name/away_team_name", file=sys.stderr)
        sys.exit(1)

    data = upsert_match(row)
    print(f"[ok] upserted {row['home_team_name']} vs {row['away_team_name']} "
          f"(match #{row['match_number']}) -> id={data[0]['id'] if data else '?'}")


if __name__ == "__main__":
    main()
