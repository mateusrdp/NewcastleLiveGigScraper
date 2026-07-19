#!/usr/bin/env python3
"""
src/main.py

Pipeline:
    1. Remove only this pipeline's previously *generated* output files
       (calendars/gigs*.ics) -- individual scraper output files are
       left as-is.
    2. Run every scraper script in src/scrapers/. Each one is expected to
       write its own .ics file into calendars/. If a scraper fails (or
       produces no events) it simply doesn't overwrite its file, so
       merging below falls back to that scraper's last successful
       output instead of losing that source for the run.
    3. Merge every .ics file in calendars/ (same-named events get
       combined, see merge_ics.py) into calendars/gigs.ics.
    4. For every filters/*.txt regex file, filter the merged calendar
       into calendars/gigs_<filter_name>.ics.

Run from anywhere with:
    python src/main.py
"""

import glob
import subprocess
import sys
from pathlib import Path

# Make merge_ics.py (sitting next to this file) importable regardless of
# where main.py is invoked from.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import merge_ics  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRAPERS_DIR = PROJECT_ROOT / "src" / "scrapers"
CALENDARS_DIR = PROJECT_ROOT / "calendars"
FILTERS_DIR = PROJECT_ROOT / "filters"


def clean_generated_outputs():
    """Remove this pipeline's own generated files (anything matching
    calendars/gigs*.ics) before regenerating them.

    Individual scraper .ics files are deliberately left alone -- if a
    scraper fails to produce a fresh file this run, its *previous*
    output stays in calendars/ and simply gets folded into the merge as
    if that scraper hadn't run, rather than dropping that source's
    events entirely. Non-scraper sources such as recurring_events.ics
    are untouched for the same reason: this function only ever deletes
    files matching calendars/gigs*.ics, which is exactly the naming
    pattern merge_step/filter_step use for everything they produce
    (gigs.ics, gigs_<filter_name>.ics, ...)."""
    CALENDARS_DIR.mkdir(parents=True, exist_ok=True)

    removed = []
    for path in glob.glob(str(CALENDARS_DIR / "gigs*.ics")):
        Path(path).unlink()
        removed.append(Path(path).name)

    note = f" (removed: {', '.join(sorted(removed))})" if removed else " (nothing to remove)"
    print(f"Cleaned generated outputs in '{CALENDARS_DIR}'{note}.")


def discover_scrapers():
    if not SCRAPERS_DIR.exists():
        return []
    return sorted(
        p for p in SCRAPERS_DIR.glob("*.py")
        if not p.name.startswith("_")
    )


def run_scrapers(scraper_paths):
    """Run each scraper as its own subprocess, with the project root as
    the working directory so relative paths like "calendars/..." and
    "filters/..." inside each scraper resolve correctly. One scraper
    failing doesn't stop the others."""
    results = []
    for path in scraper_paths:
        print(f"\n--- Running scraper: {path.relative_to(PROJECT_ROOT)} ---")
        try:
            proc = subprocess.run(
                [sys.executable, str(path)],
                cwd=str(PROJECT_ROOT),
                check=False,
            )
            ok = proc.returncode == 0
            if not ok:
                print(f"WARNING: {path.name} exited with code {proc.returncode}", file=sys.stderr)
            results.append((path.name, ok))
        except Exception as e:
            print(f"WARNING: {path.name} failed to run: {e}", file=sys.stderr)
            results.append((path.name, False))
    return results


def merge_step():
    print(f"\n--- Merging calendars in '{CALENDARS_DIR}' ---")
    events = merge_ics.load_events(str(CALENDARS_DIR))
    if not events:
        print("No events found to merge; skipping merge/filter steps.", file=sys.stderr)
        return None

    merged_cal = merge_ics.build_merged_calendar(events)
    output_path = CALENDARS_DIR / "gigs.ics"
    merge_ics.write_calendar(merged_cal, str(output_path))
    print(f"Wrote merged calendar to '{output_path}'.")
    return merged_cal


def filter_step(merged_cal):
    if merged_cal is None:
        return

    filters = merge_ics.load_keyword_filters(str(FILTERS_DIR))
    if not filters:
        print(f"\nNo filter files found in '{FILTERS_DIR}'; skipping filtering.")
        return

    print(f"\n--- Applying {len(filters)} filter(s) from '{FILTERS_DIR}' ---")
    for filter_name, keywords in filters.items():
        filtered_cal = merge_ics.filter_calendar(merged_cal, keywords)
        num_events = len(filtered_cal.walk("VEVENT"))
        out_path = CALENDARS_DIR / f"gigs_{filter_name}.ics"
        merge_ics.write_calendar(filtered_cal, str(out_path))
        print(f"Filter '{filter_name}': {num_events} event(s) -> {out_path}")


def main():
    clean_generated_outputs()

    scraper_paths = discover_scrapers()
    if not scraper_paths:
        print(f"No scraper scripts found in '{SCRAPERS_DIR}'.", file=sys.stderr)
    else:
        print(f"Found {len(scraper_paths)} scraper(s): "
              + ", ".join(p.name for p in scraper_paths))
        run_scrapers(scraper_paths)

    merged_cal = merge_step()
    filter_step(merged_cal)

    print("\nDone.")


if __name__ == "__main__":
    main()