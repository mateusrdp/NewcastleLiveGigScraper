#!/usr/bin/env python3
"""
merge_ics.py

Merge events that share the same name (SUMMARY) across multiple ICS
calendar files in a folder, and optionally filter the merged result by
keyword.

Designed to be used two ways:
  1. Imported by src/main.py, which calls load_events(), build_merged_calendar(),
     and filter_calendar() directly as part of the scrape -> merge -> filter
     pipeline.
  2. Run standalone for testing:
         python merge_ics.py [calendars_folder] [-o output.ics]

Requires: pip install icalendar
"""

import argparse
import glob
import hashlib
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from collections import defaultdict

from icalendar import Calendar, Event

SYDNEY_TZ = ZoneInfo("Australia/Sydney")


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def to_sydney_local(value):
    """Given a date or datetime value from an ICS DTSTART/DTEND, return
    the Australia/Sydney-local equivalent.

    - Plain dates (all-day events) are returned unchanged; there's no
      timezone concept for a bare date.
    - A naive datetime (no tzinfo) is assumed to already represent
      Sydney local time and is just labeled as such.
    - A timezone-aware datetime (e.g. UTC, as some libraries produce
      when serializing) is converted to Sydney local time, correctly
      handling the AEST/AEDT daylight-saving offset for that date.
    """
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is not None:
        return value.astimezone(SYDNEY_TZ)
    return value.replace(tzinfo=SYDNEY_TZ)


def normalize_timezone(vevent):
    """Rewrite DTSTART/DTEND on `vevent` in place so both are expressed
    in Australia/Sydney local time rather than UTC or any other zone."""
    for field in ("DTSTART", "DTEND"):
        prop = vevent.get(field)
        if prop is None:
            continue
        new_value = to_sydney_local(prop.dt)
        if field in vevent:
            del vevent[field]
        vevent.add(field, new_value)
    return vevent


def load_events(folder):
    """Return a list of (source_filename, vevent) for every VEVENT found
    in every .ics file inside `folder`. Auto-detects all .ics files.
    Every event's DTSTART/DTEND is normalized to Australia/Sydney local
    time as it's loaded, so grouping by day and the final output are
    always in local time regardless of what a given source calendar
    used."""
    events = []
    ics_files = sorted(glob.glob(os.path.join(folder, "*.ics")))

    if not ics_files:
        print(f"No .ics files found in '{folder}'.", file=sys.stderr)
        return events

    print(f"Found {len(ics_files)} ICS file(s): {', '.join(os.path.basename(p) for p in ics_files)}")

    for path in ics_files:
        fname = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                cal = Calendar.from_ical(f.read())
        except Exception as e:
            print(f"  Could not parse {fname}: {e}", file=sys.stderr)
            continue

        count = 0
        for component in cal.walk("VEVENT"):
            normalize_timezone(component)
            events.append((fname, component))
            count += 1
        print(f"  {fname}: {count} event(s)")

    return events


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------

def event_name(vevent):
    summary = vevent.get("SUMMARY")
    return str(summary).strip() if summary else "(no title)"


def is_datetime_value(dt_field):
    """True if the property holds a real datetime (has a time component)
    rather than a plain date."""
    if dt_field is None:
        return False
    return isinstance(dt_field.dt, datetime)


def event_day(vevent):
    """Return the calendar date (a date, never a datetime) an event falls
    on, or None if it has no DTSTART. Used to group events so that
    recurring events with the same name on different days are treated as
    separate events, not merged together."""
    dtstart = vevent.get("DTSTART")
    if dtstart is None:
        return None
    value = dtstart.dt
    if isinstance(value, datetime):
        return value.date()
    return value


def event_venue(vevent):
    """Return a normalized (lowercased, trimmed) venue string from the
    LOCATION property, or "" if none is set. Normalized only for use as a
    grouping key — the original casing is preserved in the output event."""
    loc = vevent.get("LOCATION")
    if not loc:
        return ""
    return str(loc).strip().lower()


def specificity_score(vevent):
    """Higher score = more specific timing information available."""
    dtstart = vevent.get("DTSTART")
    if dtstart is None:
        return 0
    return 2 if is_datetime_value(dtstart) else 1


def format_dt(dt_field):
    if dt_field is None:
        return "no time given"
    value = dt_field.dt
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M %Z").strip()
    return value.strftime("%Y-%m-%d") + " (date only)"


def merge_group(name, group):
    """
    group: list of (source_filename, vevent) sharing the same SUMMARY.
    Returns a single merged icalendar Event.
    """
    # Most specific date/time info wins as the "base" for DTSTART/DTEND.
    group_sorted = sorted(group, key=lambda item: specificity_score(item[1]), reverse=True)
    base_source, base_event = group_sorted[0]

    merged = Event()
    merged.add("SUMMARY", name)

    dtstart = base_event.get("DTSTART")
    dtend = base_event.get("DTEND")
    if dtstart is not None:
        merged.add("DTSTART", dtstart.dt)
    if dtend is not None:
        merged.add("DTEND", dtend.dt)

    # First non-empty location found, in specificity order.
    for _, ev in group_sorted:
        loc = ev.get("LOCATION")
        if loc:
            merged.add("LOCATION", str(loc))
            break

    # Combine descriptions, labeling each source, plus a note on what
    # timing info each source reported.
    timing_notes = []
    desc_blocks = []
    for source, ev in group_sorted:
        desc = ev.get("DESCRIPTION")
        desc_text = str(desc).strip() if desc else "(no description)"
        timing_notes.append(f"{source}: {format_dt(ev.get('DTSTART'))}")
        desc_blocks.append(f"[From {source}]\n{desc_text}")

    full_description = (
        "Timing reported by each source:\n"
        + "\n".join(timing_notes)
        + "\n\n"
        + "\n\n".join(desc_blocks)
    )
    merged.add("DESCRIPTION", full_description)

    sources_joined = ",".join(sorted({s for s, _ in group_sorted}))
    uid_seed = f"{name}|{sources_joined}".encode("utf-8")
    uid_hash = hashlib.md5(uid_seed).hexdigest()
    merged.add("UID", f"merged-{uid_hash}@merge-ics")
    merged.add("COMMENT", f"Merged from: {sources_joined}")

    return merged


def build_merged_calendar(events):
    """events: list of (source_filename, vevent) as returned by load_events().
    Returns a single icalendar.Calendar with same-named events on the same
    day AND at the same venue merged. Events with the same name but a
    different day, or the same name/day at a different venue (e.g. two
    unrelated venues both hosting a "Trivia Night" on the same evening),
    are kept separate."""
    groups = defaultdict(list)
    for source, vevent in events:
        key = (event_name(vevent), event_day(vevent), event_venue(vevent))
        groups[key].append((source, vevent))

    cal = Calendar()
    cal.add("prodid", "-//merge-ics//EN")
    cal.add("version", "2.0")

    merged_count = 0
    passthrough_count = 0

    for (name, _day, _venue), group in groups.items():
        if len(group) > 1:
            cal.add_component(merge_group(name, group))
            merged_count += 1
        else:
            _, vevent = group[0]
            cal.add_component(vevent)
            passthrough_count += 1

    print(f"Merged {merged_count} event group(s) that shared a name, date, and venue; "
          f"passed through {passthrough_count} unique event(s).")
    return cal


# --------------------------------------------------------------------------
# Filtering
# --------------------------------------------------------------------------

def load_keyword_filters(filters_folder):
    """Return {filter_name: [keyword, ...]} for every filters/*.txt file."""
    filters = {}
    for filter_path in sorted(glob.glob(os.path.join(filters_folder, "*.txt"))):
        filter_name = os.path.splitext(os.path.basename(filter_path))[0]
        with open(filter_path, encoding="utf-8") as fh:
            keywords = [line.strip() for line in fh if line.strip()]
        if keywords:
            filters[filter_name] = keywords
    return filters


def filter_calendar(cal, keywords):
    """Return a new icalendar.Calendar containing only VEVENTs from `cal`
    whose SUMMARY matches at least one of `keywords` (whole-word,
    case-insensitive)."""
    patterns = [
        re.compile(rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", re.IGNORECASE)
        for kw in keywords
    ]

    filtered = Calendar()
    filtered.add("prodid", "-//merge-ics//EN")
    filtered.add("version", "2.0")

    for component in cal.walk("VEVENT"):
        summary = event_name(component)
        if any(p.search(summary) for p in patterns):
            filtered.add_component(component)

    return filtered


# --------------------------------------------------------------------------
# I/O helper
# --------------------------------------------------------------------------

def write_calendar(cal, path):
    with open(path, "wb") as f:
        f.write(cal.to_ical())


# --------------------------------------------------------------------------
# Standalone CLI (kept for quick manual testing of merging in isolation)
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Merge same-named events across all ICS files in a folder."
    )
    parser.add_argument(
        "folder", nargs="?", default="calendars",
        help="Folder containing .ics files (default: calendars)"
    )
    parser.add_argument(
        "-o", "--output", default="merged.ics",
        help="Output .ics filename (default: merged.ics)"
    )
    args = parser.parse_args()

    events = load_events(args.folder)
    if not events:
        sys.exit(1)

    cal = build_merged_calendar(events)
    write_calendar(cal, args.output)
    print(f"Wrote merged calendar to '{args.output}'.")


if __name__ == "__main__":
    main()
