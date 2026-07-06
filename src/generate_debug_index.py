#!/usr/bin/env python3
"""
generate_debug_index.py

Build a simple debug.html for the GitHub Pages site, listing every
scraper-failure snapshot found in <public_dir>/debug/ (put there by
copying calendars/debug/* during the workflow — see update_ics.yml).

calendars/ is wiped at the start of every pipeline run (src/main.py), so
these snapshots never accumulate across runs — each run's debug.html
only ever reflects that run's failures (or the lack of any).

Usage:
    python src/generate_debug_index.py <public_dir>
"""

import glob
import html
import os
import sys

def build_html(debug_files):
    items = []
    for fname in sorted(debug_files):
        items.append(f'<li><a href="debug/{html.escape(fname)}">{html.escape(fname)}</a></li>')

    items_html = "\n".join(items) if items else "<li>No failed-page snapshots from the last run — nothing to debug right now.</li>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Scraper Debug Snapshots</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    max-width: 640px;
    margin: 3rem auto;
    padding: 0 1.25rem;
    line-height: 1.5;
    color: #222;
  }}
  h1 {{ font-size: 1.5rem; }}
  ul {{ padding-left: 1.2rem; margin-top: 1.5rem; }}
  li {{ margin: 0.5rem 0; }}
  a {{ color: #0b5fff; text-decoration: none; word-break: break-all; }}
  a:hover {{ text-decoration: underline; }}
  footer {{ margin-top: 2.5rem; font-size: 0.9rem; color: #666; }}
</style>
</head>
<body>
  <h1>🐛 Scraper Debug Snapshots</h1>
  <p>Raw pages a scraper failed to parse (or unexpectedly got zero events
  from) on the most recent run, saved here for inspection instead of
  needing to dig through Actions logs.</p>
  <ul>
    {items_html}
  </ul>
  <footer>
    <p>This list is wiped and regenerated on every run — it only ever
    reflects the most recent run's failures.</p>
  </footer>
</body>
</html>
"""

def main():
    if len(sys.argv) != 2:
        print("Usage: python generate_debug_index.py <public_dir>", file=sys.stderr)
        sys.exit(1)

    public_dir = sys.argv[1]
    debug_dir = os.path.join(public_dir, "debug")
    debug_files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(debug_dir, "*")))

    html_content = build_html(debug_files)
    out_path = os.path.join(public_dir, "debug.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Wrote {out_path} listing {len(debug_files)} debug snapshot(s).")


if __name__ == "__main__":
    main()