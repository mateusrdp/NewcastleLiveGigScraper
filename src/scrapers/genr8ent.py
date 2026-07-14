"""
Scrapes GENR8 Entertainment's gig guide and writes calendars/genr8ent.ics.

The page itself (https://genr8ent.com.au/gig-guide) injects all gig data
client-side, so there's no usable static HTML to scrape -- confirmed by
fetching it directly: the table headers render ("Date", "Artist",
"Venue", "Times and Details") but the table body is empty. It does,
however, call a plain POST endpoint that returns the data directly,
found via browser DevTools (Network tab, filter by Fetch/XHR):

    POST https://genr8ent.com.au/includes/process.php
    Content-Type: application/x-www-form-urlencoded; charset=UTF-8
    X-Requested-With: XMLHttpRequest
    body: context=getGigs&mode=all&month=0&date=&artist=

`mode=all&month=0` returns every upcoming gig in one response (849 at
the time this was written) rather than paginating month by month.

The response is JSON, but not structured gig data directly -- it's a
{"success": ..., "html": "..."} wrapper where "html" is a pre-rendered
HTML fragment: a run of `<div class="tr">` rows, each with 5 `<div
class="td">` cells: date, artist (name + link to their GENR8 artist
page), venue (name + link to their GENR8 venue page, plus a separate
"(see on map)" link -- except ~24% of venues have no GENR8 page at all,
in which case it's just plain text), a time range, and social-share
buttons (ignored). So this is parsed the same way as the other
scrapers' static-HTML pages, just after first unwrapping the JSON.

A few source quirks worth knowing about, found by inspecting the real
response:
  - Dates are given as "12th July" -- day + ordinal suffix + full month
    name, no year (guessed the same way as the other scrapers: if the
    month has already passed this year, assume next year).
  - Time ranges are normally "12:00 PM - 3:00 PM", but a few instead
    use dots ("7.00 PM - 10.00 PM") -- both are handled. A handful of
    rows have a completely blank time cell, which falls back to an
    all-day event.
  - About 14% of time ranges cross midnight (e.g. "10:00 PM - 1:00
    AM") -- detected by the end time being earlier in the day than the
    start time, in which case the end date is rolled forward a day.
    Unlike the other scrapers, this source gives an explicit end time,
    so DTEND is set directly here rather than relying on merge_ics.py's
    default 3-hour fallback.

This script is run by src/main.py as one of several scrapers. It is
expected to be executed with the project root as the working directory
(src/main.py takes care of that), so the "calendars/..." path below
resolves to <project_root>/calendars/genr8ent.ics.

Filtering by keyword happens once, centrally, in src/main.py after all
scrapers' calendars have been merged — this script only needs to produce
its own raw calendar.
"""

import hashlib
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper_debug import save_debug_page, save_request_debug_page

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

PROCESS_URL = "https://genr8ent.com.au/includes/process.php"
GUIDE_URL = "https://genr8ent.com.au/gig-guide"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

MONTHS_FULL = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# "12th July" -- day + ordinal suffix + full month name, no year.
DATE_RE = re.compile(r"^(\d{1,2})(?:st|nd|rd|th)\s+([A-Za-z]+)$", re.IGNORECASE)

# "12:00 PM - 3:00 PM", tolerant of a dot instead of a colon (seen in
# real source data: "7.00 PM - 10.00 PM").
TIME_RANGE_RE = re.compile(
    r"(\d{1,2})[.:](\d{2})\s*(AM|PM)\s*-\s*(\d{1,2})[.:](\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://genr8ent.com.au",
    "Referer": GUIDE_URL,
})


def guess_year(month, today=None):
    """The guide lists dates without a year. If the month has already
    passed relative to today, assume the listing is for next year."""
    today = today or datetime.now()
    year = today.year
    if month < today.month:
        year += 1
    return year


def parse_date_cell(text):
    """Parse "12th July" into a date object, or None if `text` doesn't
    look like a date cell at all."""
    match = DATE_RE.match(text.strip())
    if not match:
        return None

    day = int(match.group(1))
    month = MONTHS_FULL.get(match.group(2).lower())
    if not month:
        return None

    year = guess_year(month)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time_range(time_text):
    """Return ((start_hour, start_minute), (end_hour, end_minute),
    crosses_midnight) for a parseable "H:MM AM/PM - H:MM AM/PM" range,
    else None (a blank cell, or anything else that doesn't match)."""
    if not time_text or not time_text.strip():
        return None

    match = TIME_RANGE_RE.search(time_text.strip())
    if not match:
        return None

    sh, sm, sap, eh, em, eap = match.groups()
    start_hour = int(sh) % 12 + (12 if sap.upper() == "PM" else 0)
    end_hour = int(eh) % 12 + (12 if eap.upper() == "PM" else 0)
    start = (start_hour, int(sm))
    end = (end_hour, int(em))
    crosses_midnight = end < start

    return start, end, crosses_midnight


def extract_venue(venue_cell):
    """Return (venue_name, venue_url) from a venue cell, which is
    either "<a>Name</a> - <a>(see on map)</a>" or just plain text when
    the venue has no GENR8 page of its own."""
    links = venue_cell.find_all("a")
    if links:
        return links[0].get_text(" ", strip=True), links[0].get("href")
    return venue_cell.get_text(" ", strip=True), None


def extract_artist(artist_cell):
    """Return (artist_name, artist_url) from an artist cell, which is
    usually a link but sometimes plain text."""
    link = artist_cell.find("a")
    if link:
        return link.get_text(" ", strip=True), link.get("href")
    return artist_cell.get_text(" ", strip=True), None


def parse_gig_rows(html_fragment):
    """Parse the pre-rendered HTML fragment from the process.php
    response into a list of gig dicts."""
    soup = BeautifulSoup(html_fragment, "html.parser")
    events = []

    for row in soup.select("div.tr"):
        cells = row.select(":scope > div.td")
        if len(cells) < 4:
            continue

        event_date = parse_date_cell(cells[0].get_text(strip=True))
        if not event_date:
            continue

        artist, artist_url = extract_artist(cells[1])
        venue, venue_url = extract_venue(cells[2])
        time_text = cells[3].get_text(" ", strip=True)

        if not artist or not venue:
            continue

        events.append({
            "artist": artist,
            "artist_url": artist_url,
            "venue": venue,
            "venue_url": venue_url,
            "date": event_date,
            "time_text": time_text,
        })

    return events


def fetch_gigs():
    payload = {
        "context": "getGigs",
        "mode": "all",
        "month": "0",
        "date": "",
        "artist": "",
    }

    r = session.post(PROCESS_URL, data=payload, timeout=20)
    if not r.ok:
        print(f"Error: POST to {PROCESS_URL} returned {r.status_code} {r.reason}")
        save_request_debug_page("genr8ent", "http_error", r)
        return []

    try:
        data = r.json()
    except ValueError:
        print("Error: process.php response was not valid JSON")
        save_request_debug_page("genr8ent", "invalid_json", r)
        return []

    if not data.get("success"):
        print(f"Error: process.php reported failure: {data.get('message')}")
        save_request_debug_page("genr8ent", "not_success", r)
        return []

    html_fragment = data.get("html", "")
    events = parse_gig_rows(html_fragment)

    if not events:
        print("Warning: no gig rows parsed from a successful response -- markup may have changed.")
        save_debug_page("genr8ent", "zero_events_parsed", html_fragment)

    return events


def build_calendar(events, source_url):
    cal = Calendar()
    cal.add("prodid", "-//NewcastleLiveGigScraper//genr8ent//EN")
    cal.add("version", "2.0")
    seen = set()

    for event_info in events:
        key = (event_info["artist"], event_info["venue"], event_info["date"])
        if key in seen:
            continue
        seen.add(key)

        e = Event()
        e.add("SUMMARY", f"{event_info['artist']} @ {event_info['venue']}")
        e.add("LOCATION", event_info["venue"])

        ev_date = event_info["date"]
        parsed = parse_time_range(event_info["time_text"])
        if parsed:
            (sh, sm), (eh, em), crosses_midnight = parsed
            start_dt = datetime(ev_date.year, ev_date.month, ev_date.day, sh, sm, tzinfo=SYDNEY_TZ)
            end_date = ev_date + timedelta(days=1) if crosses_midnight else ev_date
            end_dt = datetime(end_date.year, end_date.month, end_date.day, eh, em, tzinfo=SYDNEY_TZ)
            e.add("DTSTART", start_dt)
            e.add("DTEND", end_dt)
        else:
            e.add("DTSTART", ev_date)
            # No DTEND set here on purpose — the shared post-processing
            # step in merge_ics.py fills in a default 3-hour duration for
            # any timed event that arrives without one. (This source
            # normally gives an explicit end time, set above; this
            # fallback only applies to the rare blank-time-cell rows.)

        details = [f"Reported time: {event_info['time_text'] or 'not specified'}"]
        if event_info["artist_url"]:
            details.append(f"Artist page: {event_info['artist_url']}")
        if event_info["venue_url"]:
            details.append(f"Venue page: {event_info['venue_url']}")
        details.append(f"Source: {source_url}")
        e.add("DESCRIPTION", "\n".join(details))

        e.add("DTSTAMP", datetime.now(timezone.utc))
        uid_seed = f"{event_info['artist']}|{event_info['venue']}|{event_info['date']}"
        uid_hash = hashlib.md5(uid_seed.encode("utf-8")).hexdigest()
        e.add("UID", f"genr8ent-{uid_hash}@genr8ent")

        cal.add_component(e)

    return cal


def main():
    events = fetch_gigs()
    cal = build_calendar(events, GUIDE_URL)

    os.makedirs("calendars", exist_ok=True)
    event_count = len(cal.walk("VEVENT"))
    if event_count > 0:
        with open("calendars/genr8ent.ics", "wb") as f:
            f.write(cal.to_ical())

    print(f"Done: {event_count} events")


if __name__ == "__main__":
    main()