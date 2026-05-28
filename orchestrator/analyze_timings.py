import json
import sys
import statistics
from pathlib import Path

COLUMNS = [
    ("recording_ms",      "Audio Capture"),
    ("asr_ms",            "ASR (Whisper tiny.en)"),
    ("llm_ttft_ms",       "LLM Time-to-First-Token"),
    ("llm_total_ms",      "LLM Total Generation"),
    ("tts_total_ms",      "TTS Output Duration (synthesis + playback)"),
    ("end_to_end_ms",     "End-to-End (speech → first audio)"),
    ("wake_to_play_end_ms", "Full Span (wake → audio done)"),
]

def load(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                # Backfill tts_total_ms from raw offsets if missing
                if r.get("tts_total_ms") is None:
                    offsets = r.get("raw_offsets_ms", {})
                    tts_start = offsets.get("tts_start")
                    tts_play_end = offsets.get("tts_play_end")
                    if tts_start is not None and tts_play_end is not None:
                        r["tts_total_ms"] = round(tts_play_end - tts_start, 1)
                rows.append(r)
    return rows

def stats(values: list[float]) -> tuple[float, float, float, float]:
    if len(values) < 2:
        mean = values[0] if values else float("nan")
        return mean, 0.0, mean, mean
    return (
        statistics.mean(values),
        statistics.stdev(values),
        min(values),
        max(values),
    )

def print_table(rows: list[dict]) -> None:
    print(f"\nN = {len(rows)} interactions\n")
    print(f"{'Stage':<40} {'Mean (ms)':>10} {'SD (ms)':>10} {'Min (ms)':>10} {'Max (ms)':>10}")
    print("-" * 82)
    for key, label in COLUMNS:
        values = [r[key] for r in rows if r.get(key) is not None]
        if not values:
            print(f"{label:<40} {'—':>10} {'—':>10} {'—':>10} {'—':>10}")
            continue
        mean, sd, lo, hi = stats(values)
        print(f"{label:<40} {mean:>10.1f} {sd:>10.1f} {lo:>10.1f} {hi:>10.1f}")
    print()

def print_latex_table(rows: list[dict]) -> None:
    """Print a LaTeX table fragment for direct paste into the thesis."""
    print("\n% ── LaTeX table (paste into Section 5.6) ──────────────────")
    print("\\begin{table}[h]")
    print("  \\centering")
    print("  \\caption{End-to-end pipeline latency breakdown (n=" + str(len(rows)) + " voice interactions,")
    print("           Gemma~4E4B via Ollama, faster-whisper tiny.en, Piper TTS)}")
    print("  \\label{tab:latency}")
    print("  \\begin{tabular}{lrrrr}")
    print("    \\toprule")
    print("    \\textbf{Stage} & \\textbf{Mean (ms)} & \\textbf{SD} & \\textbf{Min} & \\textbf{Max} \\\\")
    print("    \\midrule")
    for key, label in COLUMNS:
        values = [r[key] for r in rows if r.get(key) is not None]
        if not values:
            print(f"    {label} & — & — & — & — \\\\")
            continue
        mean, sd, lo, hi = stats(values)
        print(f"    {label} & {mean:.1f} & {sd:.1f} & {lo:.1f} & {hi:.1f} \\\\")
    print("    \\bottomrule")
    print("  \\end{tabular}")
    print("\\end{table}")

def per_category(rows: list[dict]) -> None:
    """Split by utterance type if you labelled them."""
    categories = {}
    for r in rows:
        cat = r.get("category", "unlabelled")
        categories.setdefault(cat, []).append(r)
    if len(categories) <= 1:
        return
    print("\nPer-category breakdown (end_to_end_ms):")
    print(f"{'Category':<25} {'N':>4} {'Mean':>8} {'SD':>8}")
    print("-" * 48)
    for cat, cat_rows in sorted(categories.items()):
        values = [r["end_to_end_ms"] for r in cat_rows if r.get("end_to_end_ms") is not None]
        if values:
            print(f"{cat:<25} {len(values):>4} {statistics.mean(values):>8.1f} {statistics.stdev(values) if len(values)>1 else 0.0:>8.1f}")

if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "timer_log.jsonl"
    if not Path(path).exists():
        print(f"[error] File not found: {path}", file=sys.stderr)
        sys.exit(1)

    rows = load(path)
    if not rows:
        print("[error] No records found in log.", file=sys.stderr)
        sys.exit(1)

    print(f"\nLoaded {len(rows)} timing records from {path}")
    print_table(rows)
    print_latex_table(rows)
    per_category(rows)
