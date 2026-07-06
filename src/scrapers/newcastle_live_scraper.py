"""
Scrapes NewcastleLive's gig guide and writes calendars/newcastlelive.ics.

This script is run by src/main.py as one of several scrapers. It is
expected to be executed with the project root as the working directory
(src/main.py takes care of that), so the "calendars/..." path below
resolves to <project_root>/calendars/newcastlelive.ics.

Filtering by keyword happens once, centrally, in src/main.py after all
scrapers' calendars have been merged — this script only needs to produce
its own raw calendar.
"""

import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper_debug import save_debug_page

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event
from datetime import datetime, timezone
import hashlib
import re
import time

BASE_URL = "https://newcastlelive.com.au/gig-guide-event-calendar/page-{}/"
GUIDE_URL = "https://newcastlelive.com.au/gig-guide-event-calendar/"
AJAX_URL = "https://newcastlelive.com.au/wp-admin/admin-ajax.php"

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,
    "May": 5, "Jun": 6, "Jul": 7, "Aug": 8,
    "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}

def parse_date(text):
    # Format used by nlgg AJAX cards, e.g. 16/6/2026 or 16/06.
    slash_match = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text)
    if slash_match:
        day = int(slash_match.group(1))
        month = int(slash_match.group(2))
        year_str = slash_match.group(3)
        year = int(year_str) if year_str else datetime.now().year
        if year < 100:
            year += 2000

        try:
            return datetime(year, month, day)
        except ValueError:
            return None

    # Legacy fallback for abbreviated month names, e.g. "Jun 16".
    match = re.search(r"\b([A-Za-z]{3})\s+(\d{1,2})\b", text)
    if not match:
        return None

    mon = match.group(1)
    day = int(match.group(2))

    if mon not in MONTHS:
        return None

    return datetime(datetime.now().year, MONTHS[mon], day)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://newcastlelive.com.au/",
})

# Common substrings seen on bot-challenge / block pages (Cloudflare and
# similar). Used only to make failures easier to diagnose from CI logs —
# this can't bypass a real JS challenge, it just names what happened.
CHALLENGE_PAGE_MARKERS = [
    "just a moment", "cf-browser-verification", "cf_chl", "cf-chl",
    "attention required", "access denied", "captcha", "are you a robot",
]


def looks_like_challenge_page(html_text):
    lowered = html_text.lower()
    return any(marker in lowered for marker in CHALLENGE_PAGE_MARKERS)


def fetch_page(page_num, retries=3, backoff_seconds=2):
    url = BASE_URL.format(page_num)
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=20)
            return r.text, url, r.ok, r.status_code, r.reason
        except requests.RequestException as e:
            last_exc = e
            print(f"Warning: request to {url} failed (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise last_exc

def parse_nlgg_config(page_html):
    # Extract the inline object assigned to window.NLGG from the page.
    match = re.search(r"window\.NLGG\s*=\s*\{(.*?)\}\s*;", page_html, re.DOTALL)
    if not match:
        return None

    block = match.group(1)

    def get_value(key, default=""):
        m = re.search(rf"\b{re.escape(key)}\s*:\s*\"([^\"]*)\"", block)
        return m.group(1) if m else default

    return {
        "nonce": get_value("nonce"),
        "venueFilter": get_value("venueFilter"),
        "dateFilter": get_value("dateFilter"),
        "marketFilter": get_value("marketFilter", "false"),
        "limitFilter": get_value("limitFilter")
    }

# Text we've been told appears on the actual "More info"/"Buy Tickets"
# style buttons on gig cards, used to find them by what they say rather
# than by a guessed CSS class name (which we've never been able to
# confirm against the real page markup).
CTA_TEXT_KEYWORDS = (
    "more info", "info", "buy tickets", "get tickets", "tickets",
    "book now", "book tickets", "rsvp", "details", "learn more",
)


def _looks_like_cta_link(a_tag):
    text = a_tag.get_text(" ", strip=True).lower()
    if any(keyword in text for keyword in CTA_TEXT_KEYWORDS):
        return True
    # Fall back to a button-ish class on the link itself or its parent,
    # in case the button text varies more than expected.
    own_classes = " ".join(a_tag.get("class") or [])
    parent = a_tag.parent
    parent_classes = " ".join((parent.get("class") or [])) if parent else ""
    combined = f"{own_classes} {parent_classes}".lower()
    return any(marker in combined for marker in ("button", "btn", "cta", "buy", "ticket"))


def extract_cta_links(card):
    """Find "More info"/"Buy Tickets"-style call-to-action links anywhere
    on a gig card. Returns [(label, href), ...] in document order, using
    each link's own visible text as the label."""
    links = []
    seen_hrefs = set()
    for a in card.find_all("a", href=True):
        href = a.get("href")
        if not href or href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        if href in seen_hrefs:
            continue
        if not _looks_like_cta_link(a):
            continue
        label = a.get_text(" ", strip=True) or "Link"
        links.append((label, href))
        seen_hrefs.add(href)
    return links


def parse_event_from_card(card, source_url):
    def get_line_text(line_selector):
        line = card.select_one(line_selector)
        if not line:
            return ""

        # Prefer non-icon spans so text like "location_on" is excluded.
        value_spans = [
            s for s in line.select("span")
            if "material-symbols-outlined" not in (s.get("class") or [])
        ]
        if value_spans:
            return value_spans[-1].get_text(" ", strip=True)

        return line.get_text(" ", strip=True)

    title_el = card.select_one(".nlgg-title")
    if not title_el:
        return None

    title = title_el.get_text(" ", strip=True)
    date_text = get_line_text(".nlgg-line-date")
    date = parse_date(date_text)
    if not date:
        return None

    venue = get_line_text(".nlgg-line-venue")
    address = get_line_text(".nlgg-line-address")

    cta_links = extract_cta_links(card)

    details = []
    if address:
        details.append(f"Address: {address}")
    for label, href in cta_links:
        details.append(f"{label}: {href}")
    details.append(f"Source: {source_url}")

    summary = f"{title} @ {venue}" if venue else title

    return {
        "title": summary,
        "date": date,
        "venue": venue,
        "description": "\n".join(details)
    }

def fetch_ajax_events(config, page_num):
    payload = {
        "action": "nlgg_search",
        "nonce": config.get("nonce", ""),
        "q": "",
        "page": page_num,
        "current_url": BASE_URL.format(page_num),
        "venue_filter": config.get("venueFilter", ""),
        "date_filter": config.get("dateFilter", ""),
        "market_filter": config.get("marketFilter", "false"),
        "limit_filter": config.get("limitFilter", "")
    }

    r = session.post(AJAX_URL, data=payload, timeout=20)
    if not r.ok:
        return None, r.status_code, r.reason, r.text

    try:
        data = r.json()
    except ValueError:
        return None, r.status_code, "Invalid JSON", r.text

    if not data.get("success"):
        return None, r.status_code, "AJAX call returned success=false", r.text

    html = data.get("data", {}).get("html", "")
    return html, r.status_code, "OK", r.text

cal = Calendar()
cal.add("prodid", "-//NewcastleLiveGigScraper//newcastlelive//EN")
cal.add("version", "2.0")
seen = set()

first_page_html, url, is_ok, status_code, reason = fetch_page(1)
if not is_ok:
    print(f"Error: GET {url} returned {status_code} {reason}")
    save_debug_page("newcastlelive", "http_error_page1", first_page_html)
    raise SystemExit(1)

config = parse_nlgg_config(first_page_html)
if not config:
    print("Error: Could not parse window.NLGG config from page HTML")
    save_debug_page("newcastlelive", "no_nlgg_config", first_page_html)
    if looks_like_challenge_page(first_page_html):
        print(
            "The response looks like a bot-challenge/CAPTCHA page rather than the "
            "real site — the request was likely blocked. This is a common issue for "
            "shared CI runner IPs (e.g. GitHub Actions'), even when the exact same "
            "code works fine from a home connection. If this keeps happening, this "
            "scraper may need to run somewhere with a non-datacenter IP (e.g. a "
            "self-hosted runner), or via a JS-capable fetcher that can pass the "
            "challenge."
        )
    else:
        snippet = re.sub(r"\s+", " ", first_page_html[:300]).strip()
        print(f"First 300 chars of the response, for debugging: {snippet!r}")
    raise SystemExit(1)

page = 1
max_pages_safety = 200

while page <= max_pages_safety:
    html, status_code, reason, raw_response_text = fetch_ajax_events(config, page)

    if html is None:
        print(f"Warning: stopping scrape because AJAX page {page} returned {status_code} {reason}")
        save_debug_page("newcastlelive", f"ajax_error_page{page}", raw_response_text)
        break

    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("article.nlgg-card")
    if not cards:
        break

    page_events = 0

    for card in cards:
        event_info = parse_event_from_card(card, GUIDE_URL)
        if not event_info:
            continue

        key = (event_info["title"], event_info["date"])
        if key in seen:
            continue
        seen.add(key)

        e = Event()
        e.add("SUMMARY", event_info["title"])
        # event_info["date"] is a midnight-of-day datetime; convert to a
        # plain date so icalendar writes it as an all-day VALUE=DATE
        # property rather than a timed DATE-TIME.
        e.add("DTSTART", event_info["date"].date())
        e.add("DESCRIPTION", event_info["description"])
        if event_info["venue"]:
            e.add("LOCATION", event_info["venue"])
        e.add("DTSTAMP", datetime.now(timezone.utc))
        uid_seed = f"{event_info['title']}|{event_info['date'].date()}".encode("utf-8")
        e.add("UID", f"newcastlelive-{hashlib.md5(uid_seed).hexdigest()}@newcastlelive")

        cal.add_component(e)
        page_events += 1

    # Stop once pagination produces no new events.
    if page_events == 0:
        break

    page += 1

os.makedirs("calendars", exist_ok=True)

event_count = len(cal.walk("VEVENT"))
if event_count > 0:
    with open("calendars/newcastlelive.ics", "wb") as f:
        f.write(cal.to_ical())

print(f"Done: {event_count} events")