"""
pipeline.py
-----------
End-to-end run, meant to be triggered on a schedule (2-3x/day, or right after
each match — see workflow.yml):

  1. fetch_reports.py   -> downloads any new PMSR PDFs from the FIFA hub
  2. parse              -> turns each PDF into result.json, using whichever
                            fifa_wc backend you point it at:
                              --parser programmatic  -> parse_fifa_report.py (local, free, deterministic)
                              --parser prompt         -> Claude API + parse_fifa_report_prompt.md
  3. load_to_supabase.py -> upserts result.json into the `matches` table

Usage:
    export SUPABASE_URL=...
    export SUPABASE_SERVICE_ROLE_KEY=...
    export ANTHROPIC_API_KEY=...   # only needed for --parser prompt

    python pipeline.py --parser programmatic \
        --parser-script /path/to/fifa_wc/parse_fifa_report.py

    python pipeline.py --parser prompt \
        --prompt-file /path/to/fifa_wc/parse_fifa_report_prompt.md
"""
import argparse
import base64
import json
import subprocess
import sys
from pathlib import Path

import load_to_supabase as loader


def parse_programmatic(pdf_path: Path, parser_script: Path, out_path: Path):
    """Calls the existing parse_fifa_report.py exactly as documented in the README."""
    subprocess.run(
        [sys.executable, str(parser_script), str(pdf_path), str(out_path)],
        check=True,
    )
    return json.loads(out_path.read_text())


def parse_prompt(pdf_path: Path, prompt_text: str, model: str = "claude-sonnet-4-6"):
    """
    Sends the PDF + extraction prompt to the Claude API, exactly as the README's
    backend 1 describes (attach prompt + PDF, model returns result.json and
    nothing else). Requires ANTHROPIC_API_KEY in the environment.
    """
    import anthropic  # pip install anthropic

    client = anthropic.Anthropic()
    pdf_b64 = base64.b64encode(pdf_path.read_bytes()).decode()

    resp = client.messages.create(
        model=model,
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
                {"type": "text", "text": prompt_text},
            ],
        }],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hub-url", default=None)
    ap.add_argument("--incoming-dir", default="./incoming")
    ap.add_argument("--results-dir", default="./results")
    ap.add_argument("--state", default="seen_reports.json")
    ap.add_argument("--parser", choices=["programmatic", "prompt"], required=True)
    ap.add_argument("--parser-script", help="Path to parse_fifa_report.py (for --parser programmatic)")
    ap.add_argument("--prompt-file", help="Path to parse_fifa_report_prompt.md (for --parser prompt)")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    args = ap.parse_args()

    incoming = Path(args.incoming_dir)
    results_dir = Path(args.results_dir)
    incoming.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: fetch
    fetch_cmd = [sys.executable, str(Path(__file__).parent / "fetch_reports.py"),
                 "--out-dir", str(incoming), "--state", args.state]
    if args.hub_url:
        fetch_cmd += ["--hub-url", args.hub_url]
    fetch_proc = subprocess.run(fetch_cmd, capture_output=True, text=True, check=True)
    print(fetch_proc.stdout)
    last_line = fetch_proc.stdout.strip().splitlines()[-1] if fetch_proc.stdout.strip() else "{}"
    new_files = json.loads(last_line).get("new_files", [])

    if not new_files:
        print("[pipeline] no new reports.")
        return

    prompt_text = Path(args.prompt_file).read_text() if args.parser == "prompt" else None

    for pdf_str in new_files:
        pdf_path = Path(pdf_str)
        out_path = results_dir / (pdf_path.stem + ".json")
        print(f"[pipeline] parsing {pdf_path.name} via {args.parser} backend...")
        try:
            if args.parser == "programmatic":
                if not args.parser_script:
                    raise ValueError("--parser-script is required for --parser programmatic")
                result = parse_programmatic(pdf_path, Path(args.parser_script), out_path)
            else:
                result = parse_prompt(pdf_path, prompt_text, model=args.model)
                out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

            row = loader.to_row(result, source_file=pdf_path.name, backend=args.parser)
            loader.upsert_match(row)
            print(f"[pipeline] loaded {row['home_team_name']} vs {row['away_team_name']} into Supabase.")
        except Exception as e:
            print(f"[pipeline] FAILED on {pdf_path.name}: {e}", file=sys.stderr)
            loader.mark_error(pdf_path.name, e)


if __name__ == "__main__":
    main()
