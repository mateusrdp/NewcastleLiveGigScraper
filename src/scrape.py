import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event
from datetime import datetime
import re

BASE_URL = "https://newcastlelive.com.au/gig-guide-event-calendar/page/{}/"

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,
    "May": 5, "Jun": 6, "Jul": 7, "Aug": 8,
    "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12
}

def parse_date(text):
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
    return r.text, url

cal = Calendar()
seen = set()

page = 1
max_pages_safety = 200

while page <= max_pages_safety:
    html, url = fetch_page(page)

    if "No Events are found" in html:
        break

    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.find_all(["p", "div", "h2", "h3", "span"])

    page_events = 0

    for b in blocks:
        text = b.get_text(" ", strip=True)
        if not text:
            continue

        date = parse_date(text)
        if not date:
            continue

        parent = b.parent
        title = None
        location = None

        if parent:
            title_tag = parent.find(["a", "strong", "h3"])
            if title_tag:
                title = title_tag.get_text(" ", strip=True)

            sib = b.find_next_sibling()
            if sib:
                location = sib.get_text(" ", strip=True)

        if not title:
            continue

        key = (title, date)
        if key in seen:
            continue
        seen.add(key)

        e = Event()
        e.name = title
        e.begin = date
        e.description = location or f"Source: {url}"

        cal.events.add(e)
        page_events += 1

    if page_events == 0:
        break

    page += 1

with open("gigs.ics", "w") as f:
    f.writelines(cal)

print(f"Done: {len(cal.events)} events")
