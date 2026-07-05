"""
Scrapes Whirlwind Entertainment's weekly gig guide and writes
calendars/whirlwind.ics.

The page (a classic server-rendered .asp page, not a JS single-page app)
lists one or more date-separator headings in the format:

    "Sunday, 5 July 2026"

(each preceded by a decorative emoji, e.g. an emoji for weather/day).
Each date heading is followed by a run of gig "cards", each rendered as
a short sequence of text fragments:

    🎤                      <- a small icon (mic/headphones/etc.)
    Dai Pritchard           <- artist/act name
    📍 Anna Bay Tavern      <- venue, pin-icon prefixed
    🕒 3:00 PM              <- time, clock-icon prefixed

(Venue and time sometimes render as two lines, sometimes as one line
with both icons — this scraper handles both.)

Rather than depending on guessed CSS class names (which weren't visible
to inspect ahead of time), this scraper works off the flattened,
document-order text content of the page (BeautifulSoup's
`stripped_strings`) and recognizes each fragment by its distinctive
emoji marker. This is more resilient to markup changes than brittle
selectors, at the cost of relying on the emoji markers staying put — if
the site redesigns and drops/changes those icons, this will need an
update.

This script is run by src/main.py as one of several scrapers. It is
expected to be executed with the project root as the working directory
(src/main.py takes care of that), so the "calendars/..." path below
resolves to <project_root>/calendars/whirlwind.ics.

Filtering by keyword happens once, centrally, in src/main.py after all
scrapers' calendars have been merged — this script only needs to produce
its own raw calendar.
"""

import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event

GUIDE_URL = "https://whirlwindent.com.au/weeklygigguide.asp"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

FULL_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# "Sunday, 5 July 2026" (year is given explicitly on this site, unlike
# some other gig guides, so no year-guessing is needed here).
DATE_HEADER_RE = re.compile(r"^([A-Za-z]+),\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})$")

TIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)", re.IGNORECASE)

VENUE_MARKER = "📍"
TIME_MARKER = "🕒"

# Once we've started capturing gigs, hitting this exact text marks the
# end of the gig-guide section and the start of the page footer.
STOP_MARKER = "Whirlwind Entertainment"

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0"
})


def strip_leading_symbols(text):
    """Strip any leading emoji/punctuation (e.g. a weather icon) so a
    date heading like "☀️Sunday, 5 July 2026" becomes "Sunday, 5 July
    2026" for regex matching."""
    return re.sub(r"^[^A-Za-z]+", "", text).strip()


def parse_date_header(text):
    """Parse "Sunday, 5 July 2026" into a date object, or None if `text`
    isn't a date header."""
    match = DATE_HEADER_RE.match(strip_leading_symbols(text))
    if not match:
        return None

    _weekday, day, month_name, year = match.groups()
    month = FULL_MONTHS.get(month_name.lower())
    if not month:
        return None

    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


def parse_time_text(time_text):
    """Return (hour, minute) for a parseable time (including
    "noon"/"midday"/"midnight"), else None."""
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
    minute = int(match.group(2))
    meridiem = match.group(3).lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return (hour, minute)


def is_icon_only(line):
    """True if `line` is just a decorative icon/emoji with no letters —
    used to detect the start of a new gig card."""
    return bool(line) and len(line) <= 8 and not re.search(r"[A-Za-z]", line)


def parse_lines(lines, source_url):
    """Walk the flattened, document-order text fragments of the page
    and pull out (title, venue, date, time_text) dicts for each gig."""
    events = []
    current_date = None
    started = False
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]

        parsed_date = parse_date_header(line)
        if parsed_date:
            current_date = parsed_date
            started = True
            i += 1
            continue

        if not started:
            i += 1
            continue

        if line == STOP_MARKER:
            break

        if is_icon_only(line):
            if i + 1 >= n:
                break
            title = lines[i + 1]

            venue_text = ""
            time_text = ""
            j = i + 2
            lookahead_limit = min(j + 2, n)
            while j < lookahead_limit:
                candidate = lines[j]
                if VENUE_MARKER in candidate:
                    if TIME_MARKER in candidate:
                        venue_part, _, time_part = candidate.partition(TIME_MARKER)
                        venue_text = venue_part.replace(VENUE_MARKER, "").strip()
                        time_text = time_part.strip()
                    else:
                        venue_text = candidate.replace(VENUE_MARKER, "").strip()
                    j += 1
                elif TIME_MARKER in candidate:
                    time_text = candidate.replace(TIME_MARKER, "").strip()
                    j += 1
                else:
                    break

            if venue_text and current_date and title:
                events.append({
                    "title": title,
                    "venue": venue_text,
                    "date": current_date,
                    "time_text": time_text,
                })
                i = j
                continue

        # Doesn't match any recognized fragment — skip a single line
        # (e.g. a stray "25 gigs" counter) and keep going.
        i += 1

    return events


def scrape_gig_guide(url):
    r = session.get(url)
    if not r.ok:
        print(f"Error: GET {url} returned {r.status_code} {r.reason}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    lines = list(soup.stripped_strings)
    return parse_lines(lines, url)


def build_calendar(events, source_url):
    cal = Calendar()
    seen = set()

    for event_info in events:
        key = (event_info["title"], event_info["venue"], event_info["date"])
        if key in seen:
            continue
        seen.add(key)

        e = Event()
        e.name = f"{event_info['title']} @ {event_info['venue']}"
        e.location = event_info["venue"]

        parsed_time = parse_time_text(event_info["time_text"])
        if parsed_time:
            hour, minute = parsed_time
            ev_date = event_info["date"]
            e.begin = datetime(
                ev_date.year, ev_date.month, ev_date.day, hour, minute,
                tzinfo=SYDNEY_TZ,
            )
        else:
            e.begin = event_info["date"]
            e.make_all_day()

        details = [f"Reported time: {event_info['time_text'] or 'not specified'}", f"Source: {source_url}"]
        e.description = "\n".join(details)

        cal.events.add(e)

    return cal


def main():
    events = scrape_gig_guide(GUIDE_URL)
    cal = build_calendar(events, GUIDE_URL)

    os.makedirs("calendars", exist_ok=True)
    if len(cal.events) > 0:
        with open("calendars/whirlwind.ics", "w", encoding="utf-8") as f:
            f.writelines(cal)

    print(f"Done: {len(cal.events)} events")


if __name__ == "__main__":
    main()
