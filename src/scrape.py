import requests
from bs4 import BeautifulSoup
from ics import Calendar, Event
from datetime import datetime
import re

URL = "https://newcastlelive.com.au/gig-guide-event-calendar/"

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
    day = match.group(2)

    if mon not in MONTHS:
        return None

    return datetime(datetime.now().year, MONTHS[mon], int(day))

r = requests.get(URL, headers={"User-Agent": "Mozilla/5.0"})
soup = BeautifulSoup(r.text, "html.parser")

cal = Calendar()

# 🔥 KEY FIX: only consider meaningful content blocks
blocks = soup.find_all(["p", "div", "h2", "h3", "span"])

seen = set()

for b in blocks:
    text = b.get_text(" ", strip=True)

    if not text:
        continue

    date = parse_date(text)
    if not date:
        continue

    # find a nearby title (usually in same or next block)
    parent = b.parent
    title = None
    location = None

    if parent:
        title_tag = parent.find(["a", "strong", "h3"])
        if title_tag:
            title = title_tag.get_text(" ", strip=True)

        # fallback: next sibling text
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
    e.description = location or ""

    cal.events.add(e)

with open("gigs.ics", "w") as f:
    f.writelines(cal)

print(f"Generated {len(cal.events)} events")
