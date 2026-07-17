"""
Scrapes Newcastle Weekly's gig guide and writes calendars/newcastleweekly.ics.

Structure of the page (as of writing): a series of <h2> date-separator
headings in the format "WEEKDAY DAY MONTH" (e.g. "FRIDAY 3 JULY", no
year given), each followed by a run of <p> tags in the format:

    <strong>Event Name</strong>, Venue, hh.mmam/pm

Occasional ads or notes are interspersed between events; these don't
match the expected shape and are skipped.

This script is run by src/main.py as one of several scrapers. It is
expected to be executed with the project root as the working directory
(src/main.py takes care of that), so the "calendars/..." path below
resolves to <project_root>/calendars/newcastleweekly.ics.

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

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

GUIDE_URL = "https://newcastleweekly.com.au/the-ultimate-newcastle-gig-guide/"

FULL_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

DATE_HEADER_RE = re.compile(r"^[A-Za-z]+\s+(\d{1,2})\s+([A-Za-z]+)$")
TIME_RE = re.compile(r"(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm)", re.IGNORECASE)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0"
})


def guess_year(month, today=None):
    """Gig guides list dates without a year. If the month has already
    passed relative to today, assume the listing is for next year."""
    today = today or datetime.now()
    year = today.year
    if month < today.month:
        year += 1
    return year


def parse_date_header(text):
    """Parse a heading like "FRIDAY 3 JULY" into a date object, or None
    if `text` doesn't look like a date header at all."""
    match = DATE_HEADER_RE.match(text.strip())
    if not match:
        return None

    day = int(match.group(1))
    month_name = match.group(2).lower()
    month = FULL_MONTHS.get(month_name)
    if not month:
        return None

    year = guess_year(month)
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time_text(time_text):
    """Return (hour, minute) if `time_text` contains a parseable clock
    time (including "noon"/"midday"/"midnight"), else None for vague text
    like "afternoon" or if the field is empty."""
    if not time_text:
        return None

    t = time_text.strip().lower()
    if t in ("noon", "midday"):
        return (12, 0)
    if t == "midnight":
        return (0, 0)

    match = TIME_RE.search(t)
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2)) if match.group(2) else 0
    meridiem = match.group(3).lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return (hour, minute)


def parse_event_paragraph(p_tag, event_date):
    """Parse a <p> tag expected to look like:
        <strong>Event Name</strong>, Venue, hh.mmam/pm
    Venue itself may contain commas, so only the *last* comma-separated
    segment is treated as the time. Returns None if the paragraph doesn't
    match the expected shape (e.g. it's an ad or a stray note)."""
    bold = p_tag.find(["strong", "b"])
    if not bold:
        return None

    title = bold.get_text(" ", strip=True).strip(" ,")
    if not title:
        return None

    full_text = p_tag.get_text(" ", strip=True)
    remainder = full_text
    if remainder.lower().startswith(title.lower()):
        remainder = remainder[len(title):]
    remainder = remainder.strip(" ,").strip()

    if not remainder or "," not in remainder:
        return None

    venue, _, time_text = remainder.rpartition(",")
    venue = venue.strip()
    time_text = time_text.strip()

    if not venue:
        return None

    parsed_time = parse_time_text(time_text)
    if parsed_time:
        hour, minute = parsed_time
        event_datetime = datetime(
            event_date.year, event_date.month, event_date.day, hour, minute,
            tzinfo=SYDNEY_TZ,
        )
    else:
        event_datetime = None

    return {
        "title": title,
        "venue": venue,
        "date": event_date,
        "time_text": time_text,
        "datetime": event_datetime,
    }


def scrape_gig_guide(url):
    """Return a list of parsed event dicts from the gig guide page."""
    r = session.get(url)
    if not r.ok:
        print(f"Error: GET {url} returned {r.status_code} {r.reason}")
        save_debug_page("newcastleweekly", "http_error", r.text)
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    date_headings = [h for h in soup.find_all(HEADING_TAGS) if parse_date_header(h.get_text(strip=True))]
    if not date_headings:
        print("Warning: no date-separator headings found — page structure may have changed.")
        save_debug_page("newcastleweekly", "no_date_headers", r.text)
        return []

    container = date_headings[0].parent
    siblings = container.find_all(True, recursive=False)

    events = []
    current_date = None
    started = False

    for child in siblings:
        if child.name in HEADING_TAGS:
            parsed_date = parse_date_header(child.get_text(strip=True))
            if parsed_date:
                current_date = parsed_date
                started = True
                continue
            elif started:
                # A non-date heading after the gig list has begun marks
                # the end of the gig guide section on the page.
                break
            else:
                continue

        if child.name == "p" and current_date is not None:
            event_info = parse_event_paragraph(child, current_date)
            if event_info:
                events.append(event_info)

    if not events:
        # Date headers were found, but nothing was parsed out from under
        # them — could be a genuinely quiet stretch, or the per-event
        # markup pattern changed. Worth a look either way.
        save_debug_page("newcastleweekly", "zero_events_parsed", r.text)

    return events


def build_calendar(events, source_url):
    cal = Calendar()
    cal.add("prodid", "-//NewcastleLiveGigScraper//newcastleweekly//EN")
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

        if event_info["datetime"] is not None:
            e.add("DTSTART", event_info["datetime"])
            # No DTEND set here on purpose — the shared post-processing
            # step in merge_ics.py fills in a default 3-hour duration for
            # any timed event that arrives without one.
        else:
            e.add("DTSTART", event_info["date"])

        details = [f"Reported time: {event_info['time_text'] or 'not specified'}", f"Source: {source_url}"]
        e.add("DESCRIPTION", "\n".join(details))
        e.add("DTSTAMP", datetime.now(timezone.utc))
        uid_seed = f"{event_info['title']}|{event_info['venue']}|{event_info['date']}".encode("utf-8")
        e.add("UID", f"newcastleweekly-{hashlib.md5(uid_seed).hexdigest()}@newcastleweekly")

        cal.add_component(e)

    return cal


def main():
    events = scrape_gig_guide(GUIDE_URL)
    cal = build_calendar(events, GUIDE_URL)

    os.makedirs("calendars", exist_ok=True)
    event_count = len(cal.walk("VEVENT"))
    if event_count > 0:
        with open("calendars/newcastleweekly.ics", "wb") as f:
            f.write(cal.to_ical())

    print(f"Done: {event_count} events")


if __name__ == "__main__":
    main()