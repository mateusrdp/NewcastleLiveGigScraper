"""
Scrapes MusicK's regional gig guide for Newcastle and writes calendars/musick.ics.

This script is run by src/main.py as one of several scrapers. It is
expected to be executed with the project root as the working directory
(src/main.py takes care of that), so the "calendars/..." path below
resolves to <project_root>/calendars/musick.ics.

Filtering by keyword happens once, centrally, in src/main.py after all
scrapers' calendars have been merged — this script only needs to produce
its own raw calendar.
"""

import hashlib
import os
import re
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scraper_debug import save_debug_page, save_request_debug_page

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from icalendar import Calendar, Event

BASE_URL = "https://musick.com.au/regional/newcastle/"
VENUE_NAME = "MusicK Newcastle"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://musick.com.au/",
    # A plain `requests` GET never sends these by default, but a real
    # browser always does for a top-level page load -- some bot/WAF
    # checks (this site returned a bare 403 on GitHub Actions' shared
    # runner IPs, while working fine from a home connection) key off
    # their absence as a bot signal.
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
})

# Common substrings seen on bot-challenge / block pages (Cloudflare and
# similar). Used only to make failures easier to diagnose from CI logs.
CHALLENGE_PAGE_MARKERS = [
    "just a moment", "cf-browser-verification", "cf_chl", "cf-chl",
    "attention required", "access denied", "captcha", "are you a robot",
]


def looks_like_challenge_page(html_text):
    lowered = html_text.lower()
    return any(marker in lowered for marker in CHALLENGE_PAGE_MARKERS)


def fetch_page(url, retries=3, backoff_seconds=2):
    """GET `url` with a few retries and backoff, in case of a transient
    block. Returns the response object (even on a non-2xx status, so the
    caller can inspect/debug it) -- raises only if every attempt fails
    to even get a response (e.g. connection errors)."""
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=20)
            return r
        except requests.RequestException as e:
            last_exc = e
            print(f"Warning: request to {url} failed (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(backoff_seconds * attempt)
    raise last_exc

def guess_year(month, today=None):
    """The guide lists dates without a year. If the month has already
    passed relative to today, assume the listing is for next year."""
    today = today or datetime.now()
    year = today.year
    if month < today.month:
        year += 1
    return year


def parse_date(day_text, month_text):
    """Parse day and month text into a date object."""
    try:
        day = int(day_text)
        # Map month abbreviations to numbers
        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
            "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
            "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
        }
        month = months.get(month_text.upper())
        if not month:
            return None

        year = guess_year(month)

        return datetime(year, month, day)
    except (ValueError, TypeError):
        return None

def extract_events_from_page(html_content):
    """Extract events from the page HTML."""
    soup = BeautifulSoup(html_content, "html.parser")
    
    # Find all gig cards
    gigs = []
    
    # Find all regional-day sections
    days = soup.select(".regional-day")
    
    for day in days:
        # Extract date information
        day_num = day.select_one(".regional-day__num")
        day_mon = day.select_one(".regional-day__mon")
        
        if not day_num or not day_mon:
            continue
            
        date_obj = parse_date(day_num.get_text(strip=True), day_mon.get_text(strip=True))
        if not date_obj:
            continue
            
        # Find all events for this day
        gig_cards = day.select(".regional-gig-card__body")
        
        for card in gig_cards:
            # Extract title (summary)
            title_elem = card.select_one(".regional-gig-card__title")
            if not title_elem:
                continue
                
            title = title_elem.get_text(strip=True)
            
            # Extract venue
            venue_elem = card.select_one(".regional-gig-card__venue")
            if venue_elem:
                # The venue text is inside the <a> tag, after the SVG icon.
                # We need to get the text content but exclude any icon text.
                # The venue name is the last text node after stripping whitespace
                venue = ""
                for child in venue_elem.children:
                    if isinstance(child, str):
                        text = child.strip()
                        if text and not text.startswith('<svg') and not text.startswith('svg'):
                            venue += text + " "
                venue = venue.strip()
            else:
                venue = ""
            
            # Extract price
            price_elem = card.select_one(".regional-gig-card__price")
            price = price_elem.get_text(strip=True) if price_elem else ""
            
            # Extract links
            tickets_link = None
            details_link = None
            
            links = card.find_all("a", href=True)
            for link in links:
                href = link.get("href")
                text = link.get_text(strip=True).lower()
                
                if "tickets" in text or "buy" in text or "ticket" in text:
                    tickets_link = href
                elif "details" in text or "more info" in text:
                    details_link = href
                    
            # Build description
            details = []
            if price:
                details.append(f"Price: {price}")
            if tickets_link:
                details.append(f"Tickets: {tickets_link}")
            if details_link:
                details.append(f"Details: {details_link}")
            details.append("Source: https://musick.com.au/regional/newcastle/")
            
            summary = f"{title} @ {venue}" if venue else title
            
            gigs.append({
                "title": summary,
                "date": date_obj.date(),  # Use date part only for all-day events
                "description": "\n".join(details),
                "venue": venue,
                "price": price,
                "tickets_link": tickets_link,
                "details_link": details_link
            })
    
    return gigs

def build_calendar(events):
    """Build an iCalendar from the extracted events."""
    cal = Calendar()
    cal.add("prodid", "-//NewcastleLiveGigScraper//musick//EN")
    cal.add("version", "2.0")
    seen = set()

    for event_info in events:
        # Use title and date as key to prevent duplicates
        key = (event_info["title"], event_info["date"])
        if key in seen:
            continue
        seen.add(key)

        e = Event()
        e.add("SUMMARY", event_info["title"])
        e.add("DTSTART", event_info["date"])
        e.add("DESCRIPTION", event_info["description"])
        if event_info["venue"]:
            e.add("LOCATION", event_info["venue"])
        e.add("DTSTAMP", datetime.now(timezone.utc))
        
        # Create UID using title and date
        uid_seed = f"{event_info['title']}|{event_info['date']}".encode("utf-8")
        e.add("UID", f"musick-{hashlib.md5(uid_seed).hexdigest()}@musick")

        cal.add_component(e)

    return cal

def main():
    r = fetch_page(BASE_URL)
    if not r.ok:
        print(f"Error: GET {BASE_URL} returned {r.status_code} {r.reason}")
        if looks_like_challenge_page(r.text):
            print(
                "The response looks like a bot-challenge/block page rather than the "
                "real site -- the request was likely blocked. This is a common issue "
                "for shared CI runner IPs (e.g. GitHub Actions'), even when the exact "
                "same code works fine from a home connection. If this keeps happening, "
                "this scraper may need to run somewhere with a non-datacenter IP (e.g. "
                "a self-hosted runner), or via a JS-capable fetcher that can pass the "
                "challenge."
            )
        save_request_debug_page("musick", "http_error", r)
        raise SystemExit(1)

    # Check for bot challenge (can still happen on a 200 response)
    if looks_like_challenge_page(r.text):
        print("Warning: The response looks like a bot-challenge page")
        save_request_debug_page("musick", "bot_challenge", r)
        raise SystemExit(1)

    events = extract_events_from_page(r.text)
    cal = build_calendar(events)

    os.makedirs("calendars", exist_ok=True)
    event_count = len(cal.walk("VEVENT"))
    if event_count > 0:
        with open("calendars/musick.ics", "wb") as f:
            f.write(cal.to_ical())

    print(f"Done: {event_count} events")

if __name__ == "__main__":
    main()