#!/usr/bin/env python3
"""
Odyssey seat watcher.

Polls Cinemark's seat maps for "The Odyssey" (IMAX 70mm) at Cinemark Dallas XD
and IMAX, and alerts via Telegram when a seat matching your criteria opens up.

How it works (all plain HTTP, no browser needed):
  1. Discover which ShowtimeId belongs to each target date/time by reading the
     theater's showtimes page. This result is CACHED (it rarely changes), so most
     5-minute runs skip straight to step 2.
  2. Fetch each matching seat map. The raw HTML contains, per seat,
     available="True|False" and info="Row,SeatNum,...".
  3. Keep only AVAILABLE seats in the wanted rows / seat-number range.
  4. If any seat is NEW since the last run, send a Telegram message.

Config is at the top of this file. Secrets come from environment variables:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   (see README.md)

Run locally:
  python watch.py            # normal run (needs the env vars to actually text)
  python watch.py --list     # list the showtimes it discovers, then exit
  python watch.py --dry-run  # do everything but print the alert instead of send
  python watch.py --fresh    # ignore cached discovery and re-discover showtimes
"""

import os
import re
import sys
import time
import json
import html
import math
import random
import datetime as dt
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

# ----------------------------------------------------------------------------
# CONFIG  — edit these to change what is watched
# ----------------------------------------------------------------------------
THEATER_ID       = "207"
# Cinemark's theater showtimes page (slug -> theater 207). Used only to discover
# which ShowtimeId belongs to each date/time.
THEATER_SLUG_URL = "https://www.cinemark.com/theatres/tx-dallas/cinemark-dallas-xd-and-imax"

MOVIE_ID     = "104867"                    # The Odyssey — IMAX 70mm (from your URL)
TARGET_TIMES = {"11:30:00", "15:15:00"}    # 11:30 am and 3:15 pm

# The theater is in Dallas (Central Time). "Today" and "already started" are
# judged in this zone, NOT the GitHub runner's UTC.
THEATER_TZ   = ZoneInfo("America/Chicago")

# The date window is fully dynamic. It starts on the CURRENT date (so we never
# search past days; SEASON_START is just a floor so we don't probe dates before
# the movie opened) and has NO fixed end: discovery walks forward until it passes
# how far Cinemark has opened ticket sales, so newly-added dates are picked up
# automatically. See advance_discovery().
SEASON_START = dt.date(2026, 7, 21)        # movie's first day (floor)

# Discovery walks forward until the theater has NO showtimes for ANY movie for
# this many consecutive days (i.e. we've passed the booking horizon), then stops.
# Using "any movie" — not just ours — means days our showtime doesn't play but
# other films do are correctly walked past, not mistaken for the end of the run.
STOP_AFTER_EMPTY_DAYS = 4
MAX_LOOKAHEAD_DAYS    = 120                 # hard safety cap on how far to probe

# Seat filter: rows E through J, seat numbers 7 through 21.
WANTED_ROWS = {"E", "F", "G", "H", "I", "J"}
SEAT_MIN, SEAT_MAX = 7, 21

# Which seat types count as a real, bookable seat. "wheelchair" is a wheelchair
# SPACE (no fixed seat) and "companion" is reserved beside it — excluded by
# default. Add "companion" here if you'd take one.
WANTED_SEAT_TYPES = {"seat"}

# Cinemark throttles at ~30-35 requests per ~90s window (per IP), so we cap how
# many requests a single run makes. Two knobs below keep every run well under it:
#
# Incremental discovery: rather than sweeping the whole horizon every run (which
# blew past the throttle AND the job timeout), a persistent cursor probes at most
# this many dates per run, advancing through the horizon over successive runs. The
# full horizon (~40+ days) refreshes every ~(horizon / DISCO_BATCH_DATES) runs, so
# newly-added dates are picked up within one sweep (~30 min).
DISCO_BATCH_DATES = 8

# Adaptive sharding: seat maps are split into shards and one shard is checked per
# run, alternating each 5-min tick. The shard COUNT is chosen automatically so a
# run never fetches more than this many seat maps, no matter how many dates exist.
# More dates -> more shards -> each showtime checked every (shards * 5) minutes.
MAX_SEATMAPS_PER_RUN = 16

# Send a "still alive, no matching seats yet" heartbeat at most this often
# (hours), so you know the watcher is running even when there's nothing to alert.
# Any real seat alert also resets this timer. Set to 0 to disable heartbeats.
HEARTBEAT_EVERY_HOURS = 6

STATE_FILE = "state.json"                  # cached across runs (see workflow)
REQUEST_PAUSE = (1.1, 1.8)                 # random sleep range between requests
MAX_RETRIES = 4                            # per request, on 429 / 5xx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ----------------------------------------------------------------------------

SEATMAP_URL = ("https://www.cinemark.com/TicketSeatMap/?TheaterId={theater}"
               "&ShowtimeId={sid}&CinemarkMovieId={movie}&Showtime={when}")

LINK_RE = re.compile(
    r'TicketSeatMap/\?TheaterId=(\d+)&ShowtimeId=(\d+)'
    r'&CinemarkMovieId=(\d+)&Showtime=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)')

# Does this theater have ANY showtime (any movie) on the page? Used to detect the
# end of Cinemark's booking window during discovery.
ANY_SHOWTIME_RE = re.compile(r'TicketSeatMap/\?TheaterId=\d+&ShowtimeId=\d+')

BUTTON_RE = re.compile(r'<button\b([^>]*)>')

SESSION = requests.Session()
SESSION.headers.update(HEADERS)


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def now_local():
    return dt.datetime.now(THEATER_TZ)


def showtime_dt(show):
    """Timezone-aware start datetime of a showtime dict."""
    return dt.datetime.fromisoformat(show["showtime_iso"]).replace(tzinfo=THEATER_TZ)


def start_date():
    """First date to search: today, or the movie's opening day if that's later.

    Also rolls to tomorrow once today's last target showtime (e.g. 15:15) has
    already started, so we don't keep scanning a day whose showings are over.
    """
    now = now_local()
    d = max(SEASON_START, now.date())
    if d == now.date() and now.strftime("%H:%M:%S") > max(TARGET_TIMES):
        d += dt.timedelta(days=1)
    return d


def upcoming(shows):
    """Drop showtimes that have already started (e.g. today's 11:30 at 1pm)."""
    now = now_local()
    return [s for s in shows if showtime_dt(s) > now]


def get(url):
    """GET with polite pacing + retry/backoff on 429 and 5xx."""
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code == 429 or 500 <= r.status_code < 600:
                wait = min(60, 4 * attempt * attempt)  # 4s, 16s, 36s, 60s
                log(f"  {r.status_code} on attempt {attempt}; backing off {wait}s")
                time.sleep(wait)
                last_exc = requests.HTTPError(f"{r.status_code}")
                continue
            r.raise_for_status()
            time.sleep(random.uniform(*REQUEST_PAUSE))
            return r.text
        except requests.RequestException as e:
            last_exc = e
            time.sleep(4 * attempt)
    raise last_exc


# ---- showtime discovery (incremental, cached in state) ---------------------

def parse_day(page, iso):
    """Matching target showtimes on one date's theater page -> list of dicts."""
    out, seen = [], set()
    for theater, sid, movie, when in LINK_RE.findall(page):
        if theater != THEATER_ID or movie != MOVIE_ID:
            continue
        day, clock = when.split("T")
        if day != iso or clock not in TARGET_TIMES or sid in seen:
            continue
        seen.add(sid)
        out.append({
            "date": iso, "time": clock, "showtime_iso": when, "showtime_id": sid,
            "url": SEATMAP_URL.format(theater=theater, sid=sid,
                                      movie=movie, when=quote(when)),
        })
    return out


def _load_map(state):
    """The cached {date -> [showtimes]} map, migrating the old flat-list format."""
    smap = state.get("showtimes_map")
    if smap is None and state.get("showtimes"):     # migrate pre-incremental cache
        smap = {}
        for s in state["showtimes"]:
            smap.setdefault(s["date"], []).append(s)
    return smap or {}


def _flatten_upcoming(smap):
    return upcoming([s for day in smap.values() for s in day])


def advance_discovery(state):
    """Probe up to DISCO_BATCH_DATES dates this run via a persistent cursor.

    The cursor walks forward through the booking horizon across successive runs and
    loops back to the start once it passes the horizon (STOP_AFTER_EMPTY_DAYS with
    no theater showtimes), so the map stays fresh and new dates get picked up while
    each run makes only a handful of discovery requests.
    """
    smap = _load_map(state)
    start = start_date()
    hard_end = now_local().date() + dt.timedelta(days=MAX_LOOKAHEAD_DAYS)

    for k in [k for k in smap if dt.date.fromisoformat(k) < start]:
        smap.pop(k)                                 # drop dates now in the past

    cur = dt.date.fromisoformat(state["disco_cursor"]) if state.get("disco_cursor") else start
    if not (start <= cur <= hard_end):
        cur, state["disco_streak"] = start, 0
    streak = state.get("disco_streak", 0)

    for _ in range(DISCO_BATCH_DATES):
        if cur > hard_end:
            cur, streak = start, 0
            break
        iso = cur.isoformat()
        try:
            page = html.unescape(get(f"{THEATER_SLUG_URL}?showDate={iso}"))
        except Exception as e:
            log(f"  ! failed to load showtimes for {iso}: {e}")
            cur += dt.timedelta(days=1)
            continue

        if not ANY_SHOWTIME_RE.search(page):        # no movie at all this day
            smap.pop(iso, None)
            streak += 1
            if streak >= STOP_AFTER_EMPTY_DAYS:
                log(f"  booking horizon reached near {iso}; sweep loops to {start}.")
                cur, streak = start, 0
                break
        else:
            streak = 0
            day_shows = parse_day(page, iso)
            if day_shows:
                smap[iso] = day_shows
                log(f"  {iso}: showtimes "
                    f"{sorted(s['showtime_id'] for s in day_shows)}")
            else:
                smap.pop(iso, None)
        cur += dt.timedelta(days=1)

    state["showtimes_map"] = smap
    state["disco_cursor"] = cur.isoformat()
    state["disco_streak"] = streak
    return _flatten_upcoming(smap)


def full_discovery():
    """One uncapped sweep of the whole horizon (used by --list; not the schedule)."""
    found, streak = [], 0
    cur = start_date()
    hard_end = now_local().date() + dt.timedelta(days=MAX_LOOKAHEAD_DAYS)
    while cur <= hard_end:
        iso = cur.isoformat()
        try:
            page = html.unescape(get(f"{THEATER_SLUG_URL}?showDate={iso}"))
        except Exception as e:
            log(f"  ! failed to load showtimes for {iso}: {e}")
            cur += dt.timedelta(days=1)
            continue
        if not ANY_SHOWTIME_RE.search(page):
            streak += 1
            if streak >= STOP_AFTER_EMPTY_DAYS:
                break
            cur += dt.timedelta(days=1)
            continue
        streak = 0
        day_shows = parse_day(page, iso)
        found.extend(day_shows)
        if day_shows:
            log(f"  {iso}: showtimes {sorted(s['showtime_id'] for s in day_shows)}")
        cur += dt.timedelta(days=1)
    return found


def get_showtimes(state, force=False):
    """Advance the incremental sweep (or do a full re-seed when forced)."""
    if force:
        log("Full re-discovery (fresh)...")
        shows = full_discovery()
        smap = {}
        for s in shows:
            smap.setdefault(s["date"], []).append(s)
        state["showtimes_map"] = smap
        state["disco_cursor"] = start_date().isoformat()
        state["disco_streak"] = 0
        state.pop("showtimes", None)                # retire old-format key
        return upcoming(shows)
    return advance_discovery(state)


# ---- sharding ---------------------------------------------------------------

def shard_count_for(n):
    """How many shards to keep a run at <= MAX_SEATMAPS_PER_RUN seat maps."""
    return max(1, math.ceil(n / MAX_SEATMAPS_PER_RUN))


def select_shard(shows, args, shard_count):
    """Pick the subset of showtimes to check this run.

    Showtimes are ordered and dealt round-robin into `shard_count` groups (so each
    group spans the whole date range, not one contiguous block). Which group runs
    is chosen from the current 5-minute clock tick, so consecutive scheduled runs
    alternate through the shards. Override with --shard=N for testing.
    """
    ordered = sorted(shows, key=lambda s: s["showtime_iso"])
    if shard_count <= 1:
        return ordered, 0

    bucket = int(time.time() // 300) % shard_count   # which 5-min tick we're on
    for a in args:
        if a.startswith("--shard="):
            bucket = int(a.split("=", 1)[1]) % shard_count
    shard = [s for i, s in enumerate(ordered) if i % shard_count == bucket]
    return shard, bucket


# ---- seat parsing -----------------------------------------------------------

def parse_available_seats(seatmap_html):
    """Yield (row, seat_num, seat_type) for AVAILABLE seats matching the filter."""
    for m in BUTTON_RE.finditer(seatmap_html):
        attrs = m.group(1)
        av = re.search(r'available="([^"]*)"', attrs, re.I)
        info = re.search(r'info="([^"]*)"', attrs, re.I)
        stype = re.search(r'seatType="([^"]*)"', attrs, re.I)
        if not (av and info) or av.group(1).lower() != "true":
            continue
        seat_type = (stype.group(1).lower() if stype else "seat")
        if seat_type not in WANTED_SEAT_TYPES:
            continue
        parts = info.group(1).split(",")
        if len(parts) < 2:
            continue
        row = parts[0].strip().upper()
        try:
            num = int(parts[1])
        except ValueError:
            continue
        if row in WANTED_ROWS and SEAT_MIN <= num <= SEAT_MAX:
            yield row, num, seat_type


def scan(shows):
    """Return (hits, failed_sids).

    hits: {seat_key -> detail}  for seats parsed as available this run.
    failed_sids: set of ShowtimeIds whose seat map could not be read.
    """
    hits, failed = {}, set()
    for s in shows:
        try:
            page = get(s["url"])
        except Exception as e:
            log(f"  ! seat map {s['showtime_id']} ({s['date']} {s['time']}) failed: {e}")
            failed.add(s["showtime_id"])
            continue
        for row, num, seat_type in parse_available_seats(page):
            key = f"{s['showtime_id']}:{row}{num}"
            hits[key] = {**s, "row": row, "num": num, "seat_type": seat_type,
                         "seat": f"{row}{num}"}
    return hits, failed


# ---- state ------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=0)


# ---- alerting ---------------------------------------------------------------

def format_message(hits, new_keys):
    lines = ["\U0001F3AC The Odyssey (IMAX 70mm) — seat(s) available!\n"]
    by_show = {}
    for key, h in hits.items():
        by_show.setdefault((h["date"], h["time"], h["url"]), []).append((key, h))
    for (date, clock, url), seats in sorted(by_show.items()):
        t12 = dt.datetime.strptime(clock, "%H:%M:%S").strftime("%-I:%M %p")
        seat_strs = [
            f"{h['seat']}{' NEW' if key in new_keys else ''}"
            for key, h in sorted(seats, key=lambda x: (x[1]["row"], x[1]["num"]))
        ]
        lines.append(f"{date}  {t12}: {', '.join(seat_strs)}")
        lines.append(url)
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram(text, dry_run=False):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if dry_run or not (token and chat):
        if not (token and chat) and not dry_run:
            log("  ! TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — printing instead.")
        print("\n----- ALERT (not sent) -----\n" + text + "\n----------------------------\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    if r.ok:
        log("  Telegram alert sent.")
    else:
        log(f"  ! Telegram send failed: {r.status_code} {r.text[:200]}")


def heartbeat_due(state):
    if HEARTBEAT_EVERY_HOURS <= 0:
        return False
    last = state.get("heartbeat_ts", 0)
    return (time.time() - last) >= HEARTBEAT_EVERY_HOURS * 3600


def heartbeat_message(all_shows, available_count):
    now = now_local().strftime("%Y-%m-%d %-I:%M %p %Z")
    if available_count:
        seat_note = (f"{available_count} matching seat(s) currently open "
                     f"(already alerted).")
    else:
        seat_note = "No matching seats open yet."
    return (f"\U0001F440 Odyssey watcher is alive — {now}\n"
            f"Watching {len(all_shows)} upcoming showtime(s). {seat_note}")


# ---- main -------------------------------------------------------------------

def main():
    args = set(sys.argv[1:])
    state = load_state()

    if "--list" in args:
        for s in upcoming(full_discovery()):
            print(f"{s['showtime_iso']}  id={s['showtime_id']}  {s['url']}")
        return

    dry_run = "--dry-run" in args
    all_shows = get_showtimes(state, force="--fresh" in args)
    shard_count = shard_count_for(len(all_shows))
    shows, bucket = select_shard(all_shows, args, shard_count)
    log(f"Shard {bucket + 1}/{shard_count}: checking {len(shows)} "
        f"of {len(all_shows)} showtime(s).")

    hits, failed = scan(shows)
    parsed_keys = set(hits)
    previous = set(state.get("available", []))

    # Only showtimes we actually fetched OK this run give authoritative results.
    # Carry forward known seats for showtimes we skipped (other shard) or that
    # failed to load, so sharding/transient errors never drop state or trigger a
    # false re-alert when the seat is "rediscovered" next cycle.
    checked_ok = {s["showtime_id"] for s in shows} - failed
    carried = {k for k in previous if k.split(":")[0] not in checked_ok}
    new_keys = parsed_keys - previous
    state["available"] = sorted(parsed_keys | carried)

    if parsed_keys:
        log(f"Available now: {sorted(parsed_keys)}")
    else:
        log("No matching seats available right now.")

    sent = False
    if new_keys:
        log(f"NEW since last run: {sorted(new_keys)} — alerting.")
        send_telegram(format_message(hits, new_keys), dry_run=dry_run)
        sent = True
    elif parsed_keys:
        log("Seats available but nothing new since last run — no alert.")

    # Heartbeat: reassure that the watcher is running. Skipped if we already sent
    # a real alert this run (you just heard from it), or if not yet due.
    if not sent and heartbeat_due(state):
        log("Heartbeat due — sending status message.")
        send_telegram(heartbeat_message(all_shows, len(state["available"])),
                      dry_run=dry_run)
        sent = True

    if sent and not dry_run:
        state["heartbeat_ts"] = time.time()

    save_state(state)


if __name__ == "__main__":
    main()
