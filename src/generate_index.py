#!/usr/bin/env python3
"""
generate_index.py

Build a simple index.html for the GitHub Pages site, listing every .ics
file that's been published (whatever's sitting in the Pages output
folder at the time this runs — nothing is hardcoded, so new filter
calendars or new scraper sources show up automatically), with a plain
download/view link and a one-click "webcal://" subscribe link for each.

Usage:
    python src/generate_index.py <public_dir> <owner/repo>

<owner/repo> is used to build the absolute GitHub Pages URL
("https://<owner>.github.io/<repo>/") needed for the webcal:// links,
since that scheme requires an absolute URL. This assumes the default
"<owner>.github.io/<repo>/" Pages URL pattern — if a custom domain is
ever set up for this site instead, this will need updating.
"""

import glob
import html
import os
import sys


KNOWN_SOURCE_LABELS = {
    "newcastlelive": "NewcastleLive",
    "newcastleweekly": "Newcastle Weekly",
    "whirlwind": "Whirlwind Entertainment",
}


def label_for(filename):
    """Turn a filename into a human-friendly label. Known single-source
    filenames get a nicer name (since they can't be split by
    underscores/hyphens); anything else falls back to a generic
    title-cased label, so new scrapers or filters just work."""
    name = os.path.splitext(filename)[0]

    if name == "gigs":
        return "All Gigs (merged from every source)"

    if name.startswith("gigs_"):
        filter_name = name[len("gigs_"):].replace("_", " ").replace("-", " ").title()
        return f"Filtered: {filter_name}"

    if name.lower() in KNOWN_SOURCE_LABELS:
        return f"{KNOWN_SOURCE_LABELS[name.lower()]} (single source, unmerged)"

    return f"{name.replace('_', ' ').replace('-', ' ').title()} (single source, unmerged)"


def build_html(ics_files, base_url):
    items = []
    for fname in sorted(ics_files):
        label = html.escape(label_for(fname))
        https_url = f"{base_url}{fname}"
        webcal_url = https_url.replace("https://", "webcal://", 1)
        items.append(f"""
        <li>
          <span class="cal-name">{label}</span>
          <a href="{html.escape(https_url)}">Download / View</a>
          &middot;
          <a href="{html.escape(webcal_url)}">Subscribe</a>
        </li>""")

    items_html = "\n".join(items) if items else "<li>No calendars published yet — check back soon.</li>"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Newcastle Gig Guide Calendars</title>
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
  h1 {{ font-size: 1.6rem; }}
  ul {{ list-style: none; padding: 0; margin-top: 1.5rem; }}
  li {{
    padding: 0.75rem 0;
    border-bottom: 1px solid #e3e3e3;
  }}
  .cal-name {{ display: block; font-weight: 600; margin-bottom: 0.25rem; }}
  a {{ color: #0b5fff; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  footer {{ margin-top: 2.5rem; font-size: 0.9rem; color: #666; }}
</style>
</head>
<body>
  <h1>🎶 Newcastle Gig Guide Calendars</h1>
  <p>Enjoy the calendars below, and keep live music alive! If you can, please support the musicians and venues you see on them! ROCK ON!</p>
  <ul>
    {items_html}
  </ul>
  <footer>
    <p>"Download / View" opens or downloads the raw .ics file. "Subscribe" hands the calendar straight to your default calendar app (works well in Apple Calendar and Outlook). For Google Calendar, use its "Subscribe from URL" option with the Download link's URL instead.</p>
    <p>This page and the calendars linked from it are regenerated automatically.</p>
  </footer>
</body>
</html>
"""


def main():
    if len(sys.argv) != 3:
        print("Usage: python generate_index.py <public_dir> <owner/repo>", file=sys.stderr)
        sys.exit(1)

    public_dir = sys.argv[1]
    owner_repo = sys.argv[2]
    if "/" not in owner_repo:
        print(f"Expected <owner/repo>, got '{owner_repo}'", file=sys.stderr)
        sys.exit(1)

    owner, repo = owner_repo.split("/", 1)
    base_url = f"https://{owner}.github.io/{repo}/"

    ics_files = sorted(os.path.basename(p) for p in glob.glob(os.path.join(public_dir, "*.ics")))
    html_content = build_html(ics_files, base_url)

    out_path = os.path.join(public_dir, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Wrote {out_path} listing {len(ics_files)} calendar(s).")


if __name__ == "__main__":
    main()
