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
         a multi-day market, festival, or exhibition listed as separate
         single-day entries by different sources, or even by one source
         that lists each day of a run separately). To avoid accidentally
         collapsing a whole year of a recurring weekly listing into one
         giant "event", dates are only merged into a span if they're
         contiguous (gaps of at most MAX_ALL_DAY_GAP_DAYS between
         consecutive dates) — a 3-week daily event has zero-day gaps and
         merges into one span; a weekly recurring listing has ~7-day gaps
         and stays as separate occurrences, regardless of how wide the
         earliest-to-latest spread is in either case.
     When two "same event" listings come from the *same* source file,
     they're treated as duplicate listings of one source (not
     independent confirmation) and only one is kept, rather than being
     merged into a description that just repeats that source's info
     twice.
  4. Optionally filters the merged result against one or more regex
     patterns (one per line in a filters/*.txt file), matched against the
     SUMMARY only. An event is kept if it matches ANY pattern in the file
     (union of matches) -- each line is an independent match rule, e.g.
     one venue's name-variants per line.

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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event
from dateutil.rrule import rrulestr

SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# A timed event with no explicit end is assumed to run this long.
DEFAULT_EVENT_DURATION = timedelta(hours=3)

# How similar (0-1, via difflib) two names/venues need to be to be
# treated as "the same", for fuzzy matching instead of exact matching.
NAME_SIMILARITY_THRESHOLD = 0.75
VENUE_SIMILARITY_THRESHOLD = 0.75

# All-day events with the same name+venue are merged into one spanning
# event only where their dates are contiguous — i.e. the gap between
# consecutive dates (once sorted) is at most this many days. This
# distinguishes a genuinely continuous multi-day run (gap = 1 day, or 0
# for overlapping reports) from a recurring weekly/monthly listing (gap
# ~= 7+ days), regardless of how wide the overall first-to-last spread
# is in either case. A small tolerance above 1 allows for a source
# missing a day here and there without splitting an otherwise-continuous
# run into fragments.
MAX_ALL_DAY_GAP_DAYS = 2

# Scraped events that are really just an un-recognized recurring series
# (a site without true recurrence support listing "every Sunday market"
# or "1st and 3rd Monday grunge night" as separate one-off entries each
# time) are auto-detected and collapsed into a single synthesized
# recurring event, PROVIDED:
#   - same exact SUMMARY, same exact LOCATION, same weekday every time
#     (deliberately exact, not fuzzy -- this feature actively discards
#     data if it fires incorrectly, so it's held to a higher bar than
#     the regular fuzzy merge).
#   - at least this many distinct occurrences...
MIN_OCCURRENCES_FOR_AUTO_RECURRENCE = 3
#   - ...spanning at least this many days between the earliest and
#     latest occurrence seen (i.e. at least ~4 weeks of regularity).
MIN_SPAN_DAYS_FOR_AUTO_RECURRENCE = 28

WEEKDAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]

# For the "1-2 weekdays, every week" pattern (e.g. every Friday and
# Saturday), dates are grouped into contiguous runs first (same idea as
# the all-day span-merge's MAX_ALL_DAY_GAP_DAYS, just scaled up for a
# weekly cadence): a gap this large or smaller between consecutive
# dates keeps them in the same run, tolerating an occasional skipped
# week without breaking continuity. A gap larger than this (e.g. a
# sporadic one-off special weeks or months later, under the same name)
# starts a new run instead -- so a strong, long recurring block can be
# detected even alongside unrelated sporadic outliers.
MAX_RECURRENCE_GAP_DAYS = 15


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


_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def title_case_text(text):
    """Title-case `text` so each word has only its first letter
    capitalised (e.g. "MY EVENTS @ THE PUB" -> "My Events @ The Pub").

    Words are split on spaces, hyphens, and other punctuation (so
    "AREA-51" -> "Area-51"), but NOT on apostrophes -- unlike Python's
    built-in str.title(), which wrongly re-capitalizes the letter right
    after an apostrophe (e.g. "O'BRIEN'S".title() -> "O'Brien'S" instead
    of the correct-looking "O'brien's").

    Known tradeoff: this also flattens genuine acronyms found in
    ALL-CAPS source data (e.g. "DJ" -> "Dj"), since there's no reliable
    way to tell an intentional acronym apart from a site's habit of
    just writing everything in capitals.
    """
    if not text:
        return text

    def fix_word(match):
        word = match.group(0)
        return word[0].upper() + word[1:].lower()

    return _WORD_RE.sub(fix_word, text)


def apply_title_case(vevent):
    """Rewrite SUMMARY and LOCATION on `vevent` in place via
    title_case_text(). Deliberately doesn't touch DESCRIPTION, since
    that's free text (sentences, addresses, URLs) where word-by-word
    title-casing would do more harm than good."""
    for field in ("SUMMARY", "LOCATION"):
        value = vevent.get(field)
        if value is None:
            continue
        new_value = title_case_text(str(value))
        del vevent[field]
        vevent.add(field, new_value)


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
            apply_title_case(component)
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


def is_recurring_event(vevent):
    """True if `vevent` has an RRULE (or RDATE) -- i.e. it's a recurring
    master event, not a single dated occurrence. These are deliberately
    never merged with anything: merge_group()/merge_allday_group() build
    a brand-new Event() and don't carry RRULE/RDATE over, so merging a
    recurring event would silently turn it into a single one-off
    occurrence and destroy its recurrence rule."""
    return vevent.get("RRULE") is not None or vevent.get("RDATE") is not None


def recurring_event_occurs_on(recurring_vevent, target_date):
    """True if `recurring_vevent`'s RRULE actually produces an occurrence
    landing on `target_date` (compared in whatever zone its DTSTART is
    in). Returns False (rather than raising) if the RRULE can't be
    parsed, so one malformed recurring event can't crash the whole run."""
    dtstart_prop = recurring_vevent.get("DTSTART")
    rrule_prop = recurring_vevent.get("RRULE")
    if dtstart_prop is None or rrule_prop is None:
        return False

    dtstart_dt = dtstart_prop.dt
    if not isinstance(dtstart_dt, datetime):
        dtstart_dt = datetime(dtstart_dt.year, dtstart_dt.month, dtstart_dt.day)

    try:
        rrule_text = rrule_prop.to_ical().decode("utf-8")
        rule = rrulestr(f"RRULE:{rrule_text}", dtstart=dtstart_dt)
    except Exception as e:
        print(f"Warning: could not parse RRULE on '{event_name(recurring_vevent)}': {e}", file=sys.stderr)
        return False

    range_start = datetime.combine(target_date, datetime.min.time())
    range_end = datetime.combine(target_date, datetime.max.time())
    if dtstart_dt.tzinfo is not None:
        range_start = range_start.replace(tzinfo=dtstart_dt.tzinfo)
        range_end = range_end.replace(tzinfo=dtstart_dt.tzinfo)

    try:
        return len(rule.between(range_start, range_end, inc=True)) > 0
    except Exception as e:
        print(f"Warning: could not evaluate RRULE on '{event_name(recurring_vevent)}': {e}", file=sys.stderr)
        return False


def _parse_byday_list(rrule_prop):
    """Extract the BYDAY value list (e.g. ["1MO", "3MO"], or ["SU"] for
    a plain "every Sunday" rule) from an RRULE property, or None if it
    doesn't have a BYDAY / can't be parsed."""
    try:
        rrule_text = rrule_prop.to_ical().decode("utf-8")
    except Exception:
        return None
    match = re.search(r"BYDAY=([^;]+)", rrule_text)
    if not match:
        return None
    return match.group(1).split(",")


def _recurrence_weekday_matcher(recurring_vevent):
    """Return a function date -> bool testing whether a date is
    consistent with `recurring_vevent`'s recurrence pattern (same
    weekday, and same ordinal-in-month if the rule specifies particular
    ones), or None if there's no usable RRULE/DTSTART to go on. Handles
    both "1MO"-style (specific ordinal) and plain "MO"-style (any
    occurrence, e.g. from a hand-written FREQ=WEEKLY;BYDAY=MO) BYDAY
    entries, and falls back to "same weekday as DTSTART" for a bare
    FREQ=WEEKLY with no BYDAY at all."""
    rrule_prop = recurring_vevent.get("RRULE")
    dtstart_prop = recurring_vevent.get("DTSTART")
    if rrule_prop is None or dtstart_prop is None:
        return None

    byday_list = _parse_byday_list(rrule_prop)
    if byday_list:
        def matcher(d):
            weekday_code = WEEKDAY_CODES[d.weekday()]
            ordinal_code = f"{_nth_weekday_of_month(d)}{weekday_code}"
            return ordinal_code in byday_list or weekday_code in byday_list
        return matcher

    anchor_weekday = dtstart_prop.dt.weekday()

    def fallback_matcher(d):
        return d.weekday() == anchor_weekday
    return fallback_matcher


def rebase_recurring_event_anchor(recurring_vevent, candidate_dates):
    """If any of `candidate_dates` predates `recurring_vevent`'s current
    DTSTART anchor AND genuinely fits its recurrence pattern (checked
    via _recurrence_weekday_matcher), move DTSTART back to the earliest
    such date (keeping the same time-of-day).

    This matters because dateutil's rrule never generates occurrences
    before a series' own DTSTART -- so without this, a hand-authored
    recurring event anchored on, say, its 3rd occurrence would never
    recognize a scraped duplicate for its 1st occurrence as the same
    series, purely because of which date happened to get typed into
    recurring_events.ics, not because of anything actually wrong with
    the match."""
    dtstart_prop = recurring_vevent.get("DTSTART")
    if dtstart_prop is None:
        return

    matcher = _recurrence_weekday_matcher(recurring_vevent)
    if matcher is None:
        return

    current_dtstart = dtstart_prop.dt
    current_date = current_dtstart.date() if isinstance(current_dtstart, datetime) else current_dtstart

    valid_earlier_dates = [d for d in candidate_dates if d < current_date and matcher(d)]
    if not valid_earlier_dates:
        return

    new_anchor_date = min(valid_earlier_dates)
    if isinstance(current_dtstart, datetime):
        new_dtstart = datetime.combine(new_anchor_date, current_dtstart.timetz())
    else:
        new_dtstart = new_anchor_date

    del recurring_vevent["DTSTART"]
    recurring_vevent.add("DTSTART", new_dtstart)
    print(f"Rebased recurring event '{event_name(recurring_vevent)}' anchor from "
          f"{current_date.isoformat()} to {new_anchor_date.isoformat()} (earlier matching "
          f"occurrence found in this run's scraped data).")


def _nth_weekday_of_month(d):
    """Return the 1-based ordinal of date `d`'s weekday within its
    month (e.g. 3 for the 3rd Wednesday)."""
    return (d.day - 1) // 7 + 1


def detect_monthly_nth_weekday_pattern(dates):
    """Given a list of dates for events sharing an exact SUMMARY and
    LOCATION, look for a confident "same Nth weekday(s) of the month"
    pattern -- e.g. every Sunday (all/most ordinals present) or every
    1st and 3rd Monday (just {1, 3}). Returns an RRULE dict suitable for
    Event.add("RRULE", ...), or None if there's nothing confident to
    report.

    Requires every date to fall on the same weekday -- which, as a side
    effect, already guarantees every pair of dates is a whole multiple
    of 7 days apart, so there's no separate gap check needed -- plus at
    least MIN_OCCURRENCES_FOR_AUTO_RECURRENCE dates spanning at least
    MIN_SPAN_DAYS_FOR_AUTO_RECURRENCE days."""
    sorted_dates = sorted(set(dates))
    if len(sorted_dates) < MIN_OCCURRENCES_FOR_AUTO_RECURRENCE:
        return None
    if (sorted_dates[-1] - sorted_dates[0]).days < MIN_SPAN_DAYS_FOR_AUTO_RECURRENCE:
        return None

    weekdays = {d.weekday() for d in sorted_dates}
    if len(weekdays) != 1:
        return None
    weekday = next(iter(weekdays))

    ordinals = sorted({_nth_weekday_of_month(d) for d in sorted_dates})
    byday = [f"{n}{WEEKDAY_CODES[weekday]}" for n in ordinals]
    return {"FREQ": "MONTHLY", "BYDAY": byday}


def detect_weekly_multi_weekday_pattern(dates):
    """Detect a "same 1-2 weekdays, every week" pattern (e.g. every
    Friday and Saturday) that holds for a confident, sufficiently long
    contiguous block of dates -- even if there are unrelated sporadic
    outlier dates before/after it (e.g. one-off holiday specials under
    the same name that don't belong to the regular weekly schedule).

    Unlike detect_monthly_nth_weekday_pattern(), this allows more than
    one weekday and doesn't require every date supplied to fit the
    pattern -- only a strong, sufficiently long contiguous run needs to
    (dates are split into runs first using the same gap-tolerant
    contiguity idea as the all-day span-merge, just scaled up for a
    weekly cadence via MAX_RECURRENCE_GAP_DAYS).

    Returns (rrule_dict, matched_dates) for the best qualifying run, or
    None if nothing confident was found. `matched_dates` is the subset
    of `dates` absorbed into the pattern -- any not in that set should
    be left by the caller as standalone events, not swept in."""
    sorted_dates = sorted(set(dates))
    if len(sorted_dates) < MIN_OCCURRENCES_FOR_AUTO_RECURRENCE:
        return None

    runs = _group_into_contiguous_runs(sorted_dates, MAX_RECURRENCE_GAP_DAYS)

    best_run = None
    for run in runs:
        if len(run) < MIN_OCCURRENCES_FOR_AUTO_RECURRENCE:
            continue
        if (run[-1] - run[0]).days < MIN_SPAN_DAYS_FOR_AUTO_RECURRENCE:
            continue
        weekdays_in_run = {d.weekday() for d in run}
        if len(weekdays_in_run) not in (1, 2):
            continue
        if best_run is None or len(run) > len(best_run):
            best_run = run

    if best_run is None:
        return None

    weekdays_in_run = sorted({d.weekday() for d in best_run})
    byday = [WEEKDAY_CODES[w] for w in weekdays_in_run]
    return {"FREQ": "WEEKLY", "BYDAY": byday}, set(best_run)


def synthesize_recurring_event(items, rrule_dict):
    """items: list of (source, vevent) all sharing the exact same
    SUMMARY and LOCATION, confidently detected as recurring on the same
    Nth weekday(s) of the month. Builds ONE recurring master event
    (never expanded into per-date copies) anchored on the earliest
    occurrence, carrying over its time-of-day if it had one."""
    sorted_items = sorted(items, key=lambda item: event_day(item[1]))
    representative_source, representative = sorted_items[0]

    name = event_name(representative)
    venue = event_venue_raw(representative)
    anchor_date = event_day(representative)

    merged = Event()
    merged.add("SUMMARY", name)
    if venue:
        merged.add("LOCATION", venue)

    dtstart_prop = representative.get("DTSTART")
    if dtstart_prop is not None and is_datetime_value(dtstart_prop):
        anchor_time = dtstart_prop.dt.timetz()
        merged.add("DTSTART", datetime.combine(anchor_date, anchor_time))
    else:
        merged.add("DTSTART", anchor_date)

    merged.add("RRULE", rrule_dict)

    # The representative event's own description is used as-is -- no
    # disclaimer or detection metadata gets added here. Subscribers see
    # this in their calendar app; they don't care how it was assembled,
    # only devs/maintainers might. That audit info (occurrence count,
    # contributing sources) goes in COMMENT instead, which calendar apps
    # don't surface to end users, consistent with how merge_group() and
    # merge_allday_group() already record their own "merged from"
    # metadata. It's also already printed to the pipeline log when this
    # detection fires, for anyone actively watching a run.
    original_desc = str(representative.get("DESCRIPTION") or "")
    merged.add("DESCRIPTION", original_desc)

    dates_seen = sorted({event_day(v) for _, v in items})
    sources_seen = sorted({s for s, _ in items})

    merged.add("DTSTAMP", datetime.now(timezone.utc))
    uid_seed = f"{name}|{venue}".encode("utf-8")
    merged.add("UID", f"autorecur-{hashlib.md5(uid_seed).hexdigest()}@merge-ics")
    merged.add("COMMENT", f"Auto-detected recurring event from {len(dates_seen)} occurrence(s); "
               f"sources: {', '.join(sources_seen)}")

    return merged


def extract_new_info(scraped_vevent, recurring_vevent):
    """Return a short string describing info found in `scraped_vevent`'s
    DESCRIPTION that isn't already present in `recurring_vevent`'s, or
    None if there's nothing new worth keeping. Deliberately narrow (just
    a price mention and any URLs not already present) rather than trying
    to diff full descriptions -- easy to apply safely as a short
    addendum, unlike trying to merge two free-text descriptions."""
    scraped_desc = str(scraped_vevent.get("DESCRIPTION") or "")
    recurring_desc = str(recurring_vevent.get("DESCRIPTION") or "")

    new_bits = []

    price_match = re.search(r"\$\s?\d+(?:\.\d{2})?", scraped_desc)
    if price_match and price_match.group(0) not in recurring_desc:
        new_bits.append(f"Price seen: {price_match.group(0)}")

    scraped_urls = re.findall(r"https?://\S+", scraped_desc)
    new_urls = []
    seen_urls = set()
    for url in scraped_urls:
        url = url.rstrip(".,)")
        if url not in recurring_desc and url not in seen_urls:
            seen_urls.add(url)
            new_urls.append(url)
    if new_urls:
        new_bits.append("Link(s): " + ", ".join(new_urls))

    if not new_bits:
        return None
    return "; ".join(new_bits)


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

def _group_into_contiguous_runs(sorted_dates, max_gap_days):
    """Split a sorted list of unique dates into runs where consecutive
    dates are at most `max_gap_days` apart. Returns a list of date lists."""
    runs = []
    current_run = [sorted_dates[0]]
    for d in sorted_dates[1:]:
        if (d - current_run[-1]).days <= max_gap_days:
            current_run.append(d)
        else:
            runs.append(current_run)
            current_run = [d]
    runs.append(current_run)
    return runs


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
    a candidate for the multi-day all-day span-merge instead. Recurring
    events (anything with an RRULE/RDATE) are passed straight through
    untouched, never merged with anything -- see is_recurring_event()."""
    cal = Calendar()
    cal.add("prodid", "-//merge-ics//EN")
    cal.add("version", "2.0")

    merged_count = 0
    passthrough_count = 0

    recurring_events = [(s, v) for s, v in events if is_recurring_event(v)]
    single_occurrence_events = [(s, v) for s, v in events if not is_recurring_event(v)]

    # Before matching scraped duplicates against each recurring event,
    # first rebase that recurring event's anchor to the earliest
    # matching occurrence found in this run's data, if any predate its
    # current DTSTART -- see rebase_recurring_event_anchor() for why
    # this matters (dateutil's rrule never generates occurrences before
    # a series' own start date).
    for _, recurring_vevent in recurring_events:
        candidate_dates = []
        for _, vevent in single_occurrence_events:
            if not _similar(event_title_only(vevent), event_title_only(recurring_vevent), NAME_SIMILARITY_THRESHOLD):
                continue
            if not _similar(event_venue_raw(vevent), event_venue_raw(recurring_vevent), VENUE_SIMILARITY_THRESHOLD):
                continue
            day = event_day(vevent)
            if day is not None:
                candidate_dates.append(day)
        rebase_recurring_event_anchor(recurring_vevent, candidate_dates)

    # A single-occurrence listing (e.g. a scraped one-off) that's really
    # just this run's instance of a recurring event (same name, same
    # venue, and its date matches one of the recurring event's actual
    # RRULE-generated occurrences -- not just a coincidental same
    # DTSTART) is never merged in or kept alongside it: the recurring
    # event always wins, unexpanded, as the single surviving event.
    # Different scraped duplicates are checked independently (a site
    # without true recurrence support will list each month's occurrence
    # separately, possibly with different ticket links each time), and
    # if one has info the recurring listing doesn't (a price, a link),
    # that's appended to the recurring event's own DESCRIPTION in place
    # before the scraped duplicate is discarded -- never the other way
    # around, and the recurring event is never split into per-date
    # copies.
    filtered_single_events = []
    for source, vevent in single_occurrence_events:
        day = event_day(vevent)
        matched_recurring = None
        if day is not None:
            for _, recurring_vevent in recurring_events:
                if not _similar(event_title_only(vevent), event_title_only(recurring_vevent), NAME_SIMILARITY_THRESHOLD):
                    continue
                if not _similar(event_venue_raw(vevent), event_venue_raw(recurring_vevent), VENUE_SIMILARITY_THRESHOLD):
                    continue
                if recurring_event_occurs_on(recurring_vevent, day):
                    matched_recurring = recurring_vevent
                    break

        if matched_recurring is None:
            filtered_single_events.append((source, vevent))
            continue

        new_info = extract_new_info(vevent, matched_recurring)
        if new_info:
            existing_desc = str(matched_recurring.get("DESCRIPTION") or "")
            addition = f"[Additional info from {source}]: {new_info}"
            if addition not in existing_desc:
                new_desc = f"{existing_desc}\n\n{addition}" if existing_desc else addition
                if "DESCRIPTION" in matched_recurring:
                    del matched_recurring["DESCRIPTION"]
                matched_recurring.add("DESCRIPTION", new_desc)
            print(f"Enriched recurring event '{event_name(matched_recurring)}' with new info from "
                  f"'{event_name(vevent)}' ({source}, {day}); discarding the scraped duplicate.")
        else:
            print(f"Discarding '{event_name(vevent)}' from {source} on {day}: duplicates recurring "
                  f"event '{event_name(matched_recurring)}' with no new info to add.")

    single_occurrence_events = filtered_single_events

    # Some scraped events are really just an un-recognized recurring
    # series -- a site with no true recurrence support listing "every
    # Sunday market" or "1st and 3rd Monday grunge night" as separate
    # one-off entries every time it's scraped. Detected via an EXACT
    # (not fuzzy) match on SUMMARY + LOCATION, since collapsing several
    # events into one synthesized recurring event throws away their
    # individual detail, so this is held to a much higher bar than the
    # regular merge. See detect_monthly_nth_weekday_pattern().
    exact_match_groups = defaultdict(list)
    for source, vevent in single_occurrence_events:
        exact_match_groups[(event_name(vevent), event_venue_raw(vevent))].append((source, vevent))

    auto_recurring_events = []
    remaining_single_events = []
    for (name, venue), items in exact_match_groups.items():
        dates = [event_day(v) for _, v in items]

        rrule_dict = detect_monthly_nth_weekday_pattern(dates)
        if rrule_dict:
            auto_recurring_events.append(synthesize_recurring_event(items, rrule_dict))
            print(f"Detected recurring pattern for '{name}' @ '{venue}': {rrule_dict} "
                  f"from {len(set(dates))} occurrence(s); synthesizing one recurring event "
                  f"instead of keeping them separate.")
            continue

        multi_result = detect_weekly_multi_weekday_pattern(dates)
        if multi_result:
            multi_rrule_dict, matched_dates = multi_result
            matched_items = [item for item in items if event_day(item[1]) in matched_dates]
            leftover_items = [item for item in items if event_day(item[1]) not in matched_dates]
            auto_recurring_events.append(synthesize_recurring_event(matched_items, multi_rrule_dict))
            print(f"Detected recurring pattern for '{name}' @ '{venue}': {multi_rrule_dict} "
                  f"from {len(matched_dates)} occurrence(s) in a confident run "
                  f"({min(matched_dates).isoformat()} to {max(matched_dates).isoformat()}); "
                  f"synthesizing one recurring event. {len(leftover_items)} other date(s) for "
                  f"this same name+venue didn't fit and are being kept as standalone events.")
            remaining_single_events.extend(leftover_items)
            continue

        remaining_single_events.extend(items)

    single_occurrence_events = remaining_single_events

    for vevent in auto_recurring_events:
        cal.add_component(vevent)
        merged_count += 1

    for _source, vevent in recurring_events:
        cal.add_component(vevent)
        passthrough_count += 1

    for cluster_items in _cluster_by_name_and_venue(single_occurrence_events):
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
            sorted_days = sorted(set(allday_only_days))
            for run in _group_into_contiguous_runs(sorted_days, MAX_ALL_DAY_GAP_DAYS):
                if len(run) > 1:
                    raw_items = [item for d in run for item in by_day[d]]
                    output_items.append((merge_allday_group(raw_items), True))
                else:
                    d = run[0]
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
    """Return {filter_name: [regex_pattern_str, ...]} for every
    filters/*.txt file, one regex pattern per line."""
    filters = {}
    for filter_path in sorted(glob.glob(os.path.join(filters_folder, "*.txt"))):
        filter_name = os.path.splitext(os.path.basename(filter_path))[0]
        with open(filter_path, encoding="utf-8") as fh:
            patterns = [line.strip() for line in fh if line.strip()]
        if patterns:
            filters[filter_name] = patterns
    return filters


def filter_calendar(cal, patterns):
    """Return a new icalendar.Calendar containing only VEVENTs from `cal`
    whose SUMMARY (title) matches at least one regex in `patterns`
    (case-insensitive).

    Each line/pattern in a filter file is its own independent match rule
    (e.g. one venue's name-variants per line); the output is the union of
    everything any of them matched, so a filter file's lines don't
    depend on or constrain each other.
    """
    compiled_patterns = [re.compile(p, re.IGNORECASE) for p in patterns]

    filtered = Calendar()
    filtered.add("prodid", "-//merge-ics//EN")
    filtered.add("version", "2.0")

    for component in cal.walk("VEVENT"):
        summary = event_name(component)
        if any(pattern.search(summary) for pattern in compiled_patterns):
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