#!/usr/bin/env python3
"""
merge_ics.py

Calendar post-processing script. Given a folder of per-source .ics files,
this:

  1. Normalizes every event's timezone to Australia/Sydney.
  2. Fills in a default 3-hour duration for any timed event that has a
     start but no end (moved here from the individual scrapers, so it's
     one shared rule instead of duplicated per-scraper logic).
  3. Merges events that are almost certainly the same real-world event:
       - Timed events: same calendar day, similar name, similar venue
         (fuzzy-matched, not exact — different sites word the same gig
         slightly differently, e.g. "Music Bingo With Bonnie Anne" vs
         "Vinyl Music Bingo w Bonnie Anne").
       - All-day events: similar name + similar venue, no day
         requirement — instead, all the days seen are collapsed into one
         event spanning from the earliest to the latest date seen (e.g.
         a multi-day market or exhibition listed as separate single-day
         entries by different sources). To avoid accidentally collapsing
         a whole year of a recurring weekly listing into one giant
         "event", this span-merge only applies when the earliest and
         latest dates are within MAX_ALL_DAY_MERGE_SPAN_DAYS of each
         other; beyond that, it falls back to grouping by exact day
         instead (recurring instances stay separate).
     When two "same event" listings come from the *same* source file,
     they're treated as duplicate listings of one source (not
     independent confirmation) and only one is kept, rather than being
     merged into a description that just repeats that source's info
     twice.
  4. Optionally filters the merged result by keyword.

Designed to be used two ways:
  1. Imported by src/main.py, which calls load_events(),
     build_merged_calendar(), and filter_calendar() directly as part of
     the scrape -> merge -> filter pipeline.
  2. Run standalone for testing:
         python merge_ics.py [calendars_folder] [-o output.ics]

Requires: pip install icalendar
"""

import argparse
import difflib
import glob
import hashlib
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# A timed event with no explicit end is assumed to run this long.
DEFAULT_EVENT_DURATION = timedelta(hours=3)

# How similar (0-1, via difflib) two names/venues need to be to be
# treated as "the same", for fuzzy matching instead of exact matching.
NAME_SIMILARITY_THRESHOLD = 0.75
VENUE_SIMILARITY_THRESHOLD = 0.75

# All-day events with the same name+venue but dates further apart than
# this are treated as separate (likely recurring) occurrences instead of
# being collapsed into one multi-day spanning event.
MAX_ALL_DAY_MERGE_SPAN_DAYS = 14


# --------------------------------------------------------------------------
# Timezone normalization
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


def apply_default_duration(vevent):
    """If `vevent` has a timed (non-all-day) DTSTART and no DTEND, give
    it a default DEFAULT_EVENT_DURATION-long end. All-day events (a bare
    date DTSTART) are left alone — those are handled by the separate
    all-day span-merge instead, which doesn't use DTEND the same way."""
    dtstart = vevent.get("DTSTART")
    if dtstart is None or not is_datetime_value(dtstart):
        return
    if vevent.get("DTEND") is not None:
        return
    vevent.add("DTEND", dtstart.dt + DEFAULT_EVENT_DURATION)


# Some ICS writers emit a bare 8-digit date like "DTSTART:20260717"
# without the ";VALUE=DATE" parameter RFC 5545 requires to disambiguate
# it from a (malformed) DATE-TIME. The `icalendar` library correctly
# refuses to guess in that case and raises "Expected datetime, date, or
# time." Patch these up before parsing rather than depend on the writer
# being fixed upstream.
BARE_DATE_RE = re.compile(rb"^(DTSTART|DTEND):(\d{8})(\r?)$", re.MULTILINE)


def _fix_bare_date_values(raw_bytes):
    return BARE_DATE_RE.sub(rb"\1;VALUE=DATE:\2\3", raw_bytes)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load_events(folder):
    """Return a list of (source_filename, vevent) for every VEVENT found
    in every .ics file inside `folder`. Auto-detects all .ics files.
    Every event's DTSTART/DTEND is normalized to Australia/Sydney local
    time, and given a default 3-hour duration if it has a start time but
    no end, as it's loaded."""
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
                raw = _fix_bare_date_values(f.read())
                cal = Calendar.from_ical(raw)
        except Exception as e:
            print(f"  Could not parse {fname}: {e}", file=sys.stderr)
            continue

        count = 0
        for component in cal.walk("VEVENT"):
            normalize_timezone(component)
            apply_default_duration(component)
            events.append((fname, component))
            count += 1
        print(f"  {fname}: {count} event(s)")

    return events


# --------------------------------------------------------------------------
# Shared event helpers
# --------------------------------------------------------------------------

def event_name(vevent):
    summary = vevent.get("SUMMARY")
    return str(summary).strip() if summary else "(no title)"


def event_venue_raw(vevent):
    """Original-case venue string from LOCATION, or "" if none is set."""
    loc = vevent.get("LOCATION")
    return str(loc).strip() if loc else ""


def event_title_only(vevent):
    """SUMMARY with a trailing " @ <venue>" suffix stripped, if present
    (all three current scrapers format SUMMARY as "Title @ Venue" while
    also setting LOCATION=Venue separately). Comparing just the title
    avoids the venue text skewing name-similarity scores."""
    summary = event_name(vevent)
    venue = event_venue_raw(vevent)
    if venue:
        suffix = f" @ {venue}"
        if summary.endswith(suffix):
            return summary[: -len(suffix)].strip()
    return summary


def is_datetime_value(dt_field):
    """True if the property holds a real datetime (has a time component)
    rather than a plain date."""
    if dt_field is None:
        return False
    return isinstance(dt_field.dt, datetime)


def is_all_day_event(vevent):
    dtstart = vevent.get("DTSTART")
    return dtstart is not None and not is_datetime_value(dtstart)


def event_day(vevent):
    """Return the calendar date (a date, never a datetime) an event falls
    on, or None if it has no DTSTART."""
    dtstart = vevent.get("DTSTART")
    if dtstart is None:
        return None
    value = dtstart.dt
    if isinstance(value, datetime):
        return value.date()
    return value


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


# --------------------------------------------------------------------------
# Fuzzy name/venue similarity
# --------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _normalize_for_similarity(text):
    text = text.lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return text.strip()


def _similar(a, b, threshold):
    """Fuzzy string match via difflib, on normalized (lowercased,
    punctuation-stripped) text. Two empty strings are considered equal;
    an empty vs non-empty string never matches."""
    a_norm, b_norm = _normalize_for_similarity(a), _normalize_for_similarity(b)
    if not a_norm or not b_norm:
        return a_norm == b_norm
    if a_norm == b_norm:
        return True
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio() >= threshold


# --------------------------------------------------------------------------
# Same-source deduplication
# --------------------------------------------------------------------------

def _dedupe_same_source(items):
    """Given [(source, vevent), ...] all judged similar enough to be the
    same real-world event, keep at most one per source. Same-source
    "duplicates" are almost always one listing worded slightly
    differently within the same site, not independent confirmation —
    merging them would just duplicate that one source's info in the
    description. Prefers the most time-specific entry when a source has
    more than one."""
    best_by_source = {}
    order = []
    for source, vevent in items:
        if source not in best_by_source:
            order.append(source)
            best_by_source[source] = (source, vevent)
        else:
            _, existing_vevent = best_by_source[source]
            if specificity_score(vevent) > specificity_score(existing_vevent):
                best_by_source[source] = (source, vevent)
    return [best_by_source[s] for s in order]


# --------------------------------------------------------------------------
# Fuzzy clustering (shared by both the timed and all-day passes)
# --------------------------------------------------------------------------

def _cluster_by_name_and_venue(items):
    """Greedily group [(source, vevent), ...] into clusters where every
    member has a similar title and similar venue to the cluster's first
    (representative) member. Returns a list of item-lists."""
    clusters = []  # list of {"title": str, "venue": str, "items": [...]}
    for source, vevent in items:
        title = event_title_only(vevent)
        venue = event_venue_raw(vevent)
        for cluster in clusters:
            if _similar(title, cluster["title"], NAME_SIMILARITY_THRESHOLD) and \
               _similar(venue, cluster["venue"], VENUE_SIMILARITY_THRESHOLD):
                cluster["items"].append((source, vevent))
                break
        else:
            clusters.append({"title": title, "venue": venue, "items": [(source, vevent)]})
    return [cluster["items"] for cluster in clusters]


# --------------------------------------------------------------------------
# Merging: timed events
# --------------------------------------------------------------------------

def merge_group(group):
    """group: list of (source_filename, vevent) judged to be the same
    event. Returns a single merged icalendar Event, using the most
    time-specific member as the base for SUMMARY/DTSTART/DTEND/LOCATION."""
    group_sorted = sorted(group, key=lambda item: specificity_score(item[1]), reverse=True)
    base_source, base_event = group_sorted[0]
    name = event_name(base_event)

    merged = Event()
    merged.add("SUMMARY", name)

    dtstart = base_event.get("DTSTART")
    dtend = base_event.get("DTEND")
    if dtstart is not None:
        merged.add("DTSTART", dtstart.dt)
    if dtend is not None:
        merged.add("DTEND", dtend.dt)

    for _, ev in group_sorted:
        loc = ev.get("LOCATION")
        if loc:
            merged.add("LOCATION", str(loc))
            break

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
    uid_seed = f"{name}|{sources_joined}|{format_dt(dtstart)}".encode("utf-8")
    uid_hash = hashlib.md5(uid_seed).hexdigest()
    merged.add("UID", f"merged-{uid_hash}@merge-ics")
    merged.add("COMMENT", f"Merged from: {sources_joined}")

    return merged


# --------------------------------------------------------------------------
# Merging: all-day span-merge
# --------------------------------------------------------------------------

def merge_allday_group(items):
    """items: list of (source, vevent) for one fuzzy-matched name+venue
    cluster of all-day-only events, spanning possibly many dates. Builds
    one all-day event from the earliest to the latest date seen (ICS
    all-day DTEND is exclusive, so it's set to latest-date + 1 day)."""
    dates_by_source = defaultdict(set)
    for source, vevent in items:
        dates_by_source[source].add(event_day(vevent))

    all_dates = [d for dates in dates_by_source.values() for d in dates]
    min_date, max_date = min(all_dates), max(all_dates)

    deduped = _dedupe_same_source(items)
    base_source, base_event = max(deduped, key=lambda item: specificity_score(item[1]))
    name = event_name(base_event)

    merged = Event()
    merged.add("SUMMARY", name)
    merged.add("DTSTART", min_date)
    merged.add("DTEND", max_date + timedelta(days=1))

    for _, ev in deduped:
        loc = ev.get("LOCATION")
        if loc:
            merged.add("LOCATION", str(loc))
            break

    date_notes = []
    desc_blocks = []
    for source, ev in deduped:
        seen_dates = ", ".join(d.isoformat() for d in sorted(dates_by_source[source]))
        date_notes.append(f"{source}: seen on {seen_dates}")
        desc = ev.get("DESCRIPTION")
        desc_text = str(desc).strip() if desc else "(no description)"
        desc_blocks.append(f"[From {source}]\n{desc_text}")

    full_description = (
        f"Multi-day event, merged span {min_date.isoformat()} to {max_date.isoformat()}.\n"
        "Dates reported by each source:\n" + "\n".join(date_notes)
        + "\n\n" + "\n\n".join(desc_blocks)
    )
    merged.add("DESCRIPTION", full_description)

    sources_joined = ",".join(sorted(dates_by_source.keys()))
    uid_seed = f"{name}|{sources_joined}|{min_date}|{max_date}".encode("utf-8")
    merged.add("UID", f"merged-allday-{hashlib.md5(uid_seed).hexdigest()}@merge-ics")
    merged.add("COMMENT", f"Merged from: {sources_joined}")

    return merged


# --------------------------------------------------------------------------
# Top-level merge
# --------------------------------------------------------------------------

def build_merged_calendar(events):
    """events: list of (source_filename, vevent) as returned by
    load_events(). Returns a single icalendar.Calendar with duplicate/
    same-event listings merged, per the rules described in this module's
    docstring.

    Events are first fuzzy-clustered by name+venue regardless of whether
    they're timed or all-day — this matters because the same real-world
    event is often reported as a precise timed listing by one source and
    a bare all-day placeholder by another (e.g. NewcastleLive only ever
    gives a date, while Whirlwind gives an exact time); those need to
    end up as one merged event, not two. Within a cluster, items are then
    grouped by exact day: a day with any timed item merges everything on
    that day via merge_group() (which picks the most time-specific item
    as the base); a day where every item is all-day-only is set aside as
    a candidate for the multi-day all-day span-merge instead."""
    cal = Calendar()
    cal.add("prodid", "-//merge-ics//EN")
    cal.add("version", "2.0")

    merged_count = 0
    passthrough_count = 0

    for cluster_items in _cluster_by_name_and_venue(events):
        by_day = defaultdict(list)
        for source, vevent in cluster_items:
            by_day[event_day(vevent)].append((source, vevent))

        output_items = []  # list of (vevent, was_merged)
        allday_only_days = []

        for day, day_items in by_day.items():
            if all(is_all_day_event(v) for _, v in day_items):
                # No timed item for this day in this cluster -- might
                # still get folded into a multi-day span below.
                allday_only_days.append(day)
                continue
            deduped = _dedupe_same_source(day_items)
            if len(deduped) > 1:
                output_items.append((merge_group(deduped), True))
            else:
                output_items.append((deduped[0][1], False))

        if allday_only_days:
            span_days = (max(allday_only_days) - min(allday_only_days)).days
            if len(allday_only_days) > 1 and span_days <= MAX_ALL_DAY_MERGE_SPAN_DAYS:
                raw_items = [item for d in allday_only_days for item in by_day[d]]
                output_items.append((merge_allday_group(raw_items), True))
            else:
                # Either a single all-day-only day, or a spread too wide
                # to be one continuous event (e.g. a weekly recurring
                # listing) -- keep each day separate instead.
                for d in allday_only_days:
                    deduped = _dedupe_same_source(by_day[d])
                    if len(deduped) > 1:
                        output_items.append((merge_group(deduped), True))
                    else:
                        output_items.append((deduped[0][1], False))

        for vevent, was_merged in output_items:
            cal.add_component(vevent)
            merged_count += was_merged
            passthrough_count += not was_merged

    print(f"Merged {merged_count} event group(s); passed through {passthrough_count} unique event(s).")
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
        description="Merge same-event listings across all ICS files in a folder."
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