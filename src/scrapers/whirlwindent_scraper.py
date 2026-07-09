"""
Scrapes Whirlwind Entertainment's full gig guide and writes
calendars/whirlwind.ics.

This uses https://whirlwindent.com.au/gigguide.asp, which lists far more
events than the old weeklygigguide.asp page (which this replaces) and
has a completely different structure: a plain HTML table, one row per
gig, with four columns:

    Weekday DD/MM  |  ACT NAME  |  VENUE NAME  |  TIME

e.g. "Wed 08/07 | JESSIE-MAY K | HOTEL ELERMORE | 7.00PM". No year is
given (like the weekly guide's date headers, this needs guessing based
on today's date), and act/venue names are in ALL CAPS in the source --
left as-is here rather than guessing at a "corrected" casing, since
that risks mangling genuine acronyms/stylised names (e.g. "DJ ...",
"CC LEE", "T N R BAND").

The data is also visibly messier than the weekly guide's: real examples
seen include time-column typos ("PM.009", "9.00PPM", "2.0PM"), blank
times, "TBA", and even a stray "$45.00" (clearly a price that ended up
in the time column by mistake on the site's end). Anything that doesn't
cleanly parse as a time falls back to an all-day event, with the raw
text preserved in the description for transparency, rather than
guessing at a corrected time.

Rather than depending on guessed CSS classes for the table (not visible
to inspect ahead of time), rows are found by content shape: any <tr>
with exactly 4 cells whose first cell matches the "Weekday DD/MM"
pattern. This also means header/footer rows are skipped automatically,
since they won't match that pattern.

This script is run by src/main.py as one of several scrapers. It is
expected to be executed with the project root as the working directory
(src/main.py takes care of that), so the "calendars/..." path below
resolves to <project_root>/calendars/whirlwind.ics.

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
from scraper_debug import save_debug_page

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

GUIDE_URL = "https://whirlwindent.com.au/gigguide.asp"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# "Wed 08/07" -- weekday abbreviation (unused beyond pattern-matching;
# not cross-checked against the computed date) + DD/MM, no year.
DATE_RE = re.compile(r"^[A-Za-z]{3}\s+(\d{1,2})/(\d{1,2})$")

# "7.00PM", "9.30AM", tolerant of 1-2 digit minutes (e.g. the real typo
# "2.0PM" seen in source data, read as 2:00pm).
TIME_RE = re.compile(r"(\d{1,2})[.:](\d{1,2})\s*([AaPp][Mm])")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
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
    """Parse "Wed 08/07" into a date object, or None if `text` doesn't
    look like a date cell at all."""
    match = DATE_RE.match(text.strip())
    if not match:
        return None

    day, month = int(match.group(1)), int(match.group(2))
    year = guess_year(month)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time_text(time_text):
    """Return (hour, minute) for a parseable time, else None for blank
    cells, "TBA", a stray price, or anything else that doesn't cleanly
    match. Deliberately conservative -- garbled input falls back to an
    all-day event rather than risking a wrong guess."""
    if not time_text:
        return None

    t = time_text.strip()
    if not t or t.upper() in ("TBA", "TBC", "N/A"):
        return None
    if t.startswith("$"):
        # Seen in real data: a price sitting in the time column by
        # mistake on the site's end.
        return None

    match = TIME_RE.search(t)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    is_pm = match.group(3).upper().startswith("P")

    if hour > 23 or minute > 59:
        return None

    if is_pm and hour != 12:
        hour += 12
    if not is_pm and hour == 12:
        hour = 0

    return (hour, minute)


def extract_gig_rows(soup):
    """Find every table row that looks like a gig listing (exactly 4
    cells, first one a "Weekday DD/MM" date), anywhere on the page."""
    rows = []
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) != 4:
                continue

            texts = [c.get_text(" ", strip=True) for c in cells]
            event_date = parse_date_cell(texts[0])
            if not event_date:
                continue

            title, venue, time_text = texts[1], texts[2], texts[3]
            if not title or not venue:
                continue

            rows.append({
                "title": title,
                "venue": venue,
                "date": event_date,
                "time_text": time_text,
            })

    return rows


def scrape_gig_guide(url):
    r = session.get(url, timeout=20)
    if not r.ok:
        print(f"Error: GET {url} returned {r.status_code} {r.reason}")
        save_debug_page("whirlwind", "http_error", r.text)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    rows = extract_gig_rows(soup)

    if not rows:
        print("Warning: no gig rows found -- page structure may have changed.")
        save_debug_page("whirlwind", "zero_events_parsed", r.text)

    return rows


def build_calendar(events, source_url):
    cal = Calendar()
    cal.add("prodid", "-//NewcastleLiveGigScraper//whirlwind//EN")
    cal.add("version", "2.0")
    seen = set()

    for event_info in events:
        key = (event_info["title"], event_info["venue"], event_info["date"])
        if key in seen:
            continue
        seen.add(key)

        e = Event()
        e.add("SUMMARY", f"{event_info['title']} @ {event_info['venue']}")
        e.add("LOCATION", event_info["venue"])

        parsed_time = parse_time_text(event_info["time_text"])
        if parsed_time:
            hour, minute = parsed_time
            ev_date = event_info["date"]
            event_datetime = datetime(
                ev_date.year, ev_date.month, ev_date.day, hour, minute,
                tzinfo=SYDNEY_TZ,
            )
            e.add("DTSTART", event_datetime)
            # No DTEND set here on purpose — the shared post-processing
            # step in merge_ics.py fills in a default 3-hour duration for
            # any timed event that arrives without one.
        else:
            e.add("DTSTART", event_info["date"])

        details = [f"Reported time: {event_info['time_text'] or 'not specified'}", f"Source: {source_url}"]
        e.add("DESCRIPTION", "\n".join(details))
        e.add("DTSTAMP", datetime.now(timezone.utc))
        uid_seed = f"{event_info['title']}|{event_info['venue']}|{event_info['date']}".encode("utf-8")
        e.add("UID", f"whirlwind-{hashlib.md5(uid_seed).hexdigest()}@whirlwind")

        cal.add_component(e)

    return cal


def main():
    events = scrape_gig_guide(GUIDE_URL)
    cal = build_calendar(events, GUIDE_URL)

    os.makedirs("calendars", exist_ok=True)
    event_count = len(cal.walk("VEVENT"))
    if event_count > 0:
        with open("calendars/whirlwind.ics", "wb") as f:
            f.write(cal.to_ical())

    print(f"Done: {event_count} events")


if __name__ == "__main__":
    main()