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
import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event
from datetime import datetime
import re

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
    "User-Agent": "Mozilla/5.0"
})

def fetch_page(page_num):
    url = BASE_URL.format(page_num)
    r = session.get(url)
    return r.text, url, r.ok, r.status_code, r.reason

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

    cta_links = [
        a.get("href")
        for a in card.select(".nlgg-buttons a[href]")
        if a.get("href")
    ]

    details = []
    if address:
        details.append(f"Address: {address}")
    if cta_links:
        details.append("Links: " + " | ".join(cta_links))
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

    r = session.post(AJAX_URL, data=payload)
    if not r.ok:
        return None, r.status_code, r.reason

    try:
        data = r.json()
    except ValueError:
        return None, r.status_code, "Invalid JSON"

    if not data.get("success"):
        return None, r.status_code, "AJAX call returned success=false"

    html = data.get("data", {}).get("html", "")
    return html, r.status_code, "OK"

cal = Calendar()
seen = set()

first_page_html, url, is_ok, status_code, reason = fetch_page(1)
if not is_ok:
    print(f"Error: GET {url} returned {status_code} {reason}")
    raise SystemExit(1)

config = parse_nlgg_config(first_page_html)
if not config:
    print("Error: Could not parse window.NLGG config from page HTML")
    raise SystemExit(1)

page = 1
max_pages_safety = 200

while page <= max_pages_safety:
    html, status_code, reason = fetch_ajax_events(config, page)

    if html is None:
        print(f"Warning: stopping scrape because AJAX page {page} returned {status_code} {reason}")
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
        e.name = event_info["title"]
        e.begin = event_info["date"]
        e.make_all_day()
        e.description = event_info["description"]
        if event_info["venue"]:
            e.location = event_info["venue"]

        cal.events.add(e)
        page_events += 1

    # Stop once pagination produces no new events.
    if page_events == 0:
        break

    page += 1

os.makedirs("calendars", exist_ok=True)

if len(cal.events) > 0:
    with open("calendars/newcastlelive.ics", "w", encoding="utf-8") as f:
        f.writelines(cal)

print(f"Done: {len(cal.events)} events")
