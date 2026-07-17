"""
Scrapes The Stag & Hunter Hotel's "Upcoming Shows" page and writes
calendars/staghunter.ics.

The page itself is a Squarespace site that injects events client-side via
Algolia InstantSearch, so there's no usable static HTML to scrape. It
does, however, call a plain Algolia REST endpoint that returns clean
JSON directly — found via browser DevTools (Network tab, filter by
Fetch/XHR). That's what this scraper calls instead of trying to render
the page.

A couple of non-obvious things about the response, found by inspecting
real records:
  - The "DateStart" string field (e.g. "2026-07-25T10:00:00") LOOKS like
    local time but isn't — it's the same instant as "DateStartUnix" with
    no timezone marker, i.e. it's actually UTC written without a "Z".
    Using it directly would put every event ~10-11 hours off. The
    "DateStartUnix" epoch field is unambiguous and is what's used here,
    converted to Australia/Sydney.
  - "Bands" is sometimes an empty list even when the event clearly has
    performers (see EventName in that case) — falls back to EventName.
  - The API never gives an end time (DateEnd is always null in samples
    seen) — DTEND is intentionally left unset here; merge_ics.py fills
    in a default 3-hour duration for any timed event that arrives
    without one, so this scraper doesn't need to guess at one itself.

This script is run by src/main.py as one of several scrapers. It is
expected to be executed with the project root as the working directory
(src/main.py takes care of that), so the "calendars/..." path below
resolves to <project_root>/calendars/staghunter.ics.

Filtering by keyword happens once, centrally, in src/main.py after all
scrapers' calendars have been merged — this script only needs to produce
its own raw calendar.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper_debug import save_debug_page, save_request_debug_page

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

VENUE_NAME = "Stag & Hunter Hotel"
PAGE_URL = "https://stagandhunter.com.au/upcoming-shows"

ALGOLIA_URL = (
    "https://icgfyqwgtd-dsn.algolia.net/1/indexes/*/queries"
    "?x-algolia-agent=Algolia%20for%20vanilla%20JavaScript%20(lite)%203.27.1"
    "%3Binstantsearch.js%201.12.1%3BJS%20Helper%202.26.0"
    "&x-algolia-application-id=ICGFYQWGTD"
    "&x-algolia-api-key=473fe164190fd2e1ccdf6c58fbfea884"
)
ALGOLIA_INDEX = "prod_stag_and_hunter_eventguide"
HITS_PER_PAGE = 100

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "content-type": "application/x-www-form-urlencoded",
    "Origin": "https://stagandhunter.com.au",
    "Referer": "https://stagandhunter.com.au/",
})


def fetch_page(page_num):
    """Fetch one page of results from the Algolia index. Returns the
    parsed `results[0]` dict, or None on failure (after saving a debug
    snapshot of whatever was returned)."""
    payload = {
        "requests": [{
            "indexName": ALGOLIA_INDEX,
            "params": f"query=&hitsPerPage={HITS_PER_PAGE}&page={page_num}&facets=%5B%5D&tagFilters=",
        }]
    }

    r = session.post(ALGOLIA_URL, data=json.dumps(payload), timeout=20)
    if not r.ok:
        print(f"Error: POST to Algolia returned {r.status_code} {r.reason}")
        save_request_debug_page("staghunter", f"http_error_page{page_num}", r)
        return None

    try:
        data = r.json()
    except ValueError:
        print("Error: Algolia response was not valid JSON")
        save_request_debug_page("staghunter", f"invalid_json_page{page_num}", r)
        return None

    results = data.get("results")
    if not results:
        print("Error: Algolia response had no 'results'")
        save_request_debug_page("staghunter", f"no_results_page{page_num}", r)
        return None

    return results[0]


def html_to_text(raw_html):
    """Convert the API's HTML-formatted EventDescription into plain,
    readable text for the ICS description."""
    if not raw_html:
        return ""

    soup = BeautifulSoup(raw_html, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text("\n", strip=True)

    # Collapse runs of multiple blank lines down to a single blank line.
    cleaned = []
    blank_run = 0
    for line in text.splitlines():
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
        else:
            blank_run = 0
            cleaned.append(line.strip())

    return "\n".join(cleaned).strip()


def build_description(hit):
    bands = hit.get("Bands") or []
    special_guests = hit.get("SpecialGuests") or ""
    price_from = hit.get("PriceFrom")
    short_url = hit.get("ShortEventUrl") or ""

    lines = []
    if len(bands) > 1:
        lines.append("Support bands: " + ", ".join(bands[1:]))
    if len(special_guests) > 1:
        lines.append(f"Special guests: {special_guests}")

    if price_from is not None and price_from > 0:
        price_text = f"${price_from:.2f}"
    else:
        price_text = "FREE"
    lines.append(f"TICKETS: {price_text}")
    if short_url:
        lines.append(short_url)

    header = "\n".join(lines)
    body = html_to_text(hit.get("EventDescription"))

    return f"{header}\n\n{body}" if body else header


def parse_hit(hit):
    bands = hit.get("Bands") or []
    title = bands[0] if bands else hit.get("EventName", "").strip()
    if not title:
        return None

    unix_ts = hit.get("DateStartUnix")
    if not unix_ts:
        return None
    event_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc).astimezone(SYDNEY_TZ)

    guid = hit.get("EventGuid") or hit.get("objectID") or ""

    return {
        "title": title,
        "datetime": event_dt,
        "description": build_description(hit),
        "guid": guid,
    }


def build_calendar(hits):
    cal = Calendar()
    cal.add("prodid", "-//NewcastleLiveGigScraper//staghunter//EN")
    cal.add("version", "2.0")

    seen = set()

    for hit in hits:
        event_info = parse_hit(hit)
        if not event_info:
            continue

        key = event_info["guid"] or (event_info["title"], event_info["datetime"])
        if key in seen:
            continue
        seen.add(key)

        e = Event()
        e.add("SUMMARY", f"{event_info['title']} @ {VENUE_NAME}")
        e.add("LOCATION", VENUE_NAME)
        e.add("DTSTART", event_info["datetime"])
        # No DTEND set here on purpose -- the source never gives an end
        # time, and merge_ics.py fills in a default 3-hour duration for
        # any timed event that arrives without one.
        e.add("DESCRIPTION", event_info["description"])
        e.add("DTSTAMP", datetime.now(timezone.utc))

        uid_seed = event_info["guid"] or f"{event_info['title']}|{event_info['datetime']}"
        uid_hash = hashlib.md5(str(uid_seed).encode("utf-8")).hexdigest()
        e.add("UID", f"staghunter-{uid_hash}@staghunter")

        cal.add_component(e)

    return cal


def main():
    all_hits = []
    page = 0
    while True:
        result = fetch_page(page)
        if result is None:
            break

        hits = result.get("hits", [])
        all_hits.extend(hits)

        nb_pages = result.get("nbPages", 1)
        page += 1
        if page >= nb_pages:
            break

    cal = build_calendar(all_hits)

    os.makedirs("calendars", exist_ok=True)
    event_count = len(cal.walk("VEVENT"))
    if event_count > 0:
        with open("calendars/staghunter.ics", "wb") as f:
            f.write(cal.to_ical())

    print(f"Done: {event_count} events")


if __name__ == "__main__":
    main()