"""Summarize the app's own latency logs (logs/latency-*.jsonl) from real
sessions: per-stage table overall and per direction, plus voice outcomes.

  python -m tools.latency_report                 # every log in logs/
  python -m tools.latency_report logs/latency-20261016-140000.jsonl
"""
import sys
from pathlib import Path

from translator import config
from translator.latency import load_rows, outcome_counts, stage_table


def report(paths) -> str:
    rows = load_rows(paths)
    speech = [r for r in rows if r.get("kind") == "speech"]
    out = [f"# Latency from {len(paths)} session log(s), {len(speech)} utterances", "",
           "## All", "", stage_table(speech), ""]
    for src in config.PAIR:
        sub = [r for r in speech if r.get("src") == src]
        if sub:
            name = f"{config.LANGUAGES[src].name} → {config.LANGUAGES[config.other(src)].name}"
            out += [f"## {name}", "", stage_table(sub), ""]
    out.append(f"Voice outcomes: {outcome_counts(speech)}")
    ignored = sum(1 for r in rows if r.get("kind") == "ignored")
    if ignored:
        out.append(f"Ignored as echo of the app's own voice: {ignored}")
    replays = sum(1 for r in rows if r.get("kind") == "replay")
    if replays:
        out.append(f"Replays: {replays}")
    return "\n".join(out)


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    paths = [Path(a) for a in args] or sorted(config.LOG_DIR.glob("latency-*.jsonl"))
    if not paths:
        print(f"No latency logs in {config.LOG_DIR}. Run the app first.")
        return 1
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(report(paths))
    return 0


if __name__ == "__main__":
    sys.exit(main())
