#!/usr/bin/env python3
"""
Scrape Hustler Casino Live stream URLs from the last ~2 years.

Outputs (next to this script):
  - hcl_streams.csv   columns: date, youtube_url, title, duration_minutes
  - hcl_streams.json  calendar-import format (see Calendar.jsx data model)

Why this isn't a one-liner
--------------------------
The natural command,

    yt-dlp --flat-playlist --print "%(id)s|%(title)s|%(upload_date)s|%(duration)s" \
           --dateafter 20240601 "https://www.youtube.com/@HustlerCasinoLive/streams"

lists the channel fast, BUT for this channel yt-dlp returns `NA` for both
`duration` and `upload_date` in --flat-playlist mode, so neither the 2-hour
duration filter nor the date floor can be applied from flat output. `--dateafter`
likewise needs upload_date, which flat mode doesn't provide.

So we do it in two passes:
  1. Flat-list the /streams tab (fast) to get every video id + title, newest first.
  2. Full-extract each video (yt-dlp, in-process) to read its real duration and
     upload_date, stopping once we cross the 2-year floor (the tab is strictly
     reverse-chronological). Videos that error (upcoming/live/members-only) are
     skipped.

Filter kept:  duration > 7200s (2h+)  AND  upload_date >= 20240601.
Hands estimate:  duration_minutes / 3  (~one hand every 3 minutes).

Usage:
  python scripts/scrape_hcl_streams.py          # full run (~hundreds of videos)
  python scripts/scrape_hcl_streams.py --limit 10   # quick test (first 10 enriched)
"""

import csv
import json
import os
import sys

try:
    import yt_dlp
except ImportError:
    sys.exit("yt-dlp is not installed. Install it with:  python -m pip install yt-dlp")

CHANNEL_STREAMS = "https://www.youtube.com/@HustlerCasinoLive/streams"
FLOOR_DATE = "20240601"          # last ~2 years (today is 2026-06-01)
MIN_DURATION_SEC = 7200          # 2 hours — livestreams only
HANDS_PER_MINUTE = 1 / 3         # ~one hand every 3 minutes

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "hcl_streams.csv")
JSON_PATH = os.path.join(HERE, "hcl_streams.json")


def log(msg):
    """Progress to stderr so it never pollutes the data files / stdout summary."""
    print(msg, file=sys.stderr, flush=True)


def flat_list_ids(url):
    """Pass 1 — newest-first list of {id, title} for every entry in the tab."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    entries = (info or {}).get("entries") or []
    out = []
    for e in entries:
        if not e:
            continue
        vid = e.get("id")
        if vid:
            out.append({"id": vid, "title": e.get("title") or ""})
    return out


def make_extractor():
    """A single reusable yt-dlp instance for full per-video metadata extraction."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "ignoreerrors": True,
        "noplaylist": True,
        "retries": 3,
        "socket_timeout": 30,
        "youtube_include_dash_manifest": False,
    }
    return yt_dlp.YoutubeDL(opts)


def scrape(limit=None):
    log(f"Pass 1: flat-listing {CHANNEL_STREAMS} …")
    candidates = flat_list_ids(CHANNEL_STREAMS)
    log(f"  {len(candidates)} entries in the streams tab (newest first).")

    ydl = make_extractor()
    streams = []
    seen_real = 0
    consecutive_old = 0

    log(f"Pass 2: enriching with duration/date until upload_date < {FLOOR_DATE} …")
    for i, c in enumerate(candidates):
        if limit is not None and seen_real >= limit:
            log(f"  --limit {limit} reached; stopping enrichment.")
            break

        url = f"https://www.youtube.com/watch?v={c['id']}"
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001 — yt-dlp can raise many extractor errors
            log(f"  [skip] {c['id']}: {type(e).__name__}: {e}")
            info = None
        if not info:
            continue  # upcoming / live / private / members-only / removed

        upload_date = info.get("upload_date")        # 'YYYYMMDD' or None
        duration = info.get("duration")              # seconds or None
        title = info.get("title") or c["title"]

        if not upload_date:
            continue  # can't place it on the calendar without a date

        # The tab is reverse-chronological: once we hit the floor we're done.
        if upload_date < FLOOR_DATE:
            consecutive_old += 1
            if consecutive_old >= 3:                 # small tolerance for ordering quirks
                log(f"  Crossed the 2-year floor at {upload_date}; stopping.")
                break
            continue
        consecutive_old = 0
        seen_real += 1

        if not duration or duration <= MIN_DURATION_SEC:
            continue  # not a 2h+ livestream (clip, recap, short side session)

        duration_minutes = round(duration / 60)
        date_iso = f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        streams.append({
            "id": c["id"],
            "youtubeUrl": f"https://youtube.com/watch?v={c['id']}",
            "title": title,
            "date": date_iso,
            "durationMinutes": duration_minutes,
            "handsCompleted": 0,
            "handsEstimated": max(1, round(duration_minutes * HANDS_PER_MINUTE)),
            "isComplete": False,
        })

        if seen_real % 25 == 0:
            log(f"  …{seen_real} videos scanned, {len(streams)} qualifying streams so far "
                f"(latest seen: {date_iso})")

    # Newest first for display / import.
    streams.sort(key=lambda s: s["date"], reverse=True)
    return streams


def write_csv(streams):
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "youtube_url", "title", "duration_minutes"])
        for s in streams:
            w.writerow([s["date"], s["youtubeUrl"], s["title"], s["durationMinutes"]])


def write_json(streams):
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(streams, f, indent=2, ensure_ascii=False)


def main():
    limit = None
    if "--limit" in sys.argv:
        try:
            limit = int(sys.argv[sys.argv.index("--limit") + 1])
        except (IndexError, ValueError):
            sys.exit("--limit requires an integer, e.g. --limit 10")

    streams = scrape(limit=limit)
    write_csv(streams)
    write_json(streams)

    total_hands = sum(s["handsEstimated"] for s in streams)
    log("")
    log(f"Wrote {CSV_PATH}")
    log(f"Wrote {JSON_PATH}")
    if streams:
        dates = [s["date"] for s in streams]
        log(f"Date range: {min(dates)} … {max(dates)}")
    # The required summary line goes to stdout.
    print(f"Found {len(streams)} streams, estimated {total_hands:,} total hands")


if __name__ == "__main__":
    main()
