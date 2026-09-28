"""
HCL stream catalog — incremental refresh.

Runs on a timer inside the web server (see _stream_refresher in server.py) so new
Hustler Casino Live streams reach the calendar without anyone re-running the
scraper by hand.

Two ways to read the channel, picked by whether YOUTUBE_API_KEY is set:

* The Data API (preferred). Railway's IP is bot-checked by YouTube, so scraping
  from the server returns nothing at all — "Sign in to confirm you're not a
  bot". The official API answers the same questions from any IP, and the two
  calls per cycle cost 2 units out of a 10,000/day quota.
* yt-dlp (fallback, used locally). The channel's /streams tab is newest-first,
  so only the top of it matters: flat-list the first SCAN_DEPTH entries, drop
  every id the catalog already has, and full-extract just the unknown ones.

Both filter the same way as scripts/scrape_hcl_streams.py — 2h+ streams only,
~one hand every 3 minutes for the estimate — and both date a stream the way
yt-dlp's upload_date does, which is the UTC date the stream ENDED. That's the
convention the existing catalog was built on: a stream that ran 4:29pm-10pm PT
on the 21st is dated the 22nd. It's arguably a day late, but changing it now
would shift every new row against the hundreds already stored.
"""

import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CHANNEL_STREAMS = "https://www.youtube.com/@HustlerCasinoLive/streams"
CHANNEL_ID = "UCQe7wB0o_cZgv1miyYB9TMA"
# YouTube's auto-generated per-channel playlists: UU… is every upload, UULV… is
# just the live streams. The live one skips the clips the channel posts daily.
LIVE_PLAYLIST = "UULV" + CHANNEL_ID[2:]
API = "https://www.googleapis.com/youtube/v3/"
SCAN_DEPTH = 30               # ~4-6 weeks of the tab; history comes from the committed scrape
MIN_DURATION_SEC = 7200       # 2 hours — livestreams only
HANDS_PER_MINUTE = 1 / 3

# Videos already checked and ruled out (finished, but under 2h). Remembered for
# the life of the process so every cycle doesn't re-extract the same short
# sessions. Upcoming / live / still-processing videos are NOT remembered: they
# need checking again once the stream has ended.
_rejected: set = set()

_NOT_READY = {"is_live", "is_upcoming", "post_live"}


class _Collect:
    """yt-dlp logger that keeps error text, so a bot-check block can be reported
    plainly instead of looking like "no new streams"."""

    def __init__(self):
        self.errors = []

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        pass

    def error(self, msg):
        self.errors.append(str(msg))


def _stream_row(vid, info):
    upload_date = info["upload_date"]
    minutes = round(info["duration"] / 60)
    return {
        "id": vid,
        "youtubeUrl": f"https://youtube.com/watch?v={vid}",
        "title": info.get("title") or "HCL Stream",
        "date": f"{upload_date[0:4]}-{upload_date[4:6]}-{upload_date[6:8]}",
        "durationMinutes": minutes,
        "handsEstimated": max(1, round(minutes * HANDS_PER_MINUTE)),
    }


def _iso_duration_seconds(text):
    """PT5H33M12S -> 20 or so thousand seconds. Returns None if unparseable."""
    m = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", text or "")
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _api_get(endpoint, params, api_key):
    url = API + endpoint + "?" + urllib.parse.urlencode({**params, "key": api_key})
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.load(r)


def _end_date(item, seconds):
    """The stream's end, as a UTC date string — matching yt-dlp's upload_date."""
    live = item.get("liveStreamingDetails") or {}
    ended = live.get("actualEndTime")
    if ended:
        return ended[:10]
    started = live.get("actualStartTime") or item["snippet"]["publishedAt"]
    start = datetime.strptime(started[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return (start + timedelta(seconds=seconds)).strftime("%Y-%m-%d")


def _fetch_via_api(known_ids, api_key, report):
    listing = _api_get("playlistItems",
                       {"part": "contentDetails", "playlistId": LIVE_PLAYLIST,
                        "maxResults": SCAN_DEPTH}, api_key)
    ids = [i["contentDetails"]["videoId"] for i in listing.get("items", [])]
    report["scanned"] = len(ids)

    todo = [v for v in ids if v not in known_ids and v not in _rejected]
    streams = []
    for chunk in (todo[i:i + 50] for i in range(0, len(todo), 50)):
        detail = _api_get("videos",
                          {"part": "snippet,contentDetails,liveStreamingDetails",
                           "id": ",".join(chunk)}, api_key)
        for item in detail.get("items", []):
            report["extracted"] += 1
            vid = item["id"]
            seconds = _iso_duration_seconds((item.get("contentDetails") or {}).get("duration"))
            live = item.get("liveStreamingDetails") or {}
            # Still running, or scheduled and not started: check again next cycle.
            if not seconds or (live and not live.get("actualEndTime")):
                report["not_ready"] += 1
                continue
            if seconds <= MIN_DURATION_SEC:
                _rejected.add(vid)
                report["rejected"] += 1
                continue
            minutes = round(seconds / 60)
            streams.append({
                "id": vid,
                "youtubeUrl": f"https://youtube.com/watch?v={vid}",
                "title": item["snippet"].get("title") or "HCL Stream",
                "date": _end_date(item, seconds),
                "durationMinutes": minutes,
                "handsEstimated": max(1, round(minutes * HANDS_PER_MINUTE)),
            })
    return streams


def fetch_new_streams(known_ids, api_key=None):
    """New qualifying streams not in `known_ids`, plus a short report.

    Returns (streams, report) where report is
    {"scanned", "extracted", "not_ready", "rejected", "errors", "blocked", "via"}.
    """
    api_key = api_key if api_key is not None else os.environ.get("YOUTUBE_API_KEY", "")
    if api_key:
        report = {"scanned": 0, "extracted": 0, "not_ready": 0, "rejected": 0,
                  "errors": [], "blocked": False, "via": "api"}
        try:
            return _fetch_via_api(known_ids, api_key, report), report
        except Exception as e:  # bad key, quota, network — say so and stop
            detail = ""
            if hasattr(e, "read"):
                try:
                    detail = ": " + e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
            report["errors"].append(f"{type(e).__name__}: {e}{detail}")
            return [], report

    import yt_dlp  # heavy; only loaded when a refresh actually runs

    report = {"scanned": 0, "extracted": 0, "not_ready": 0, "rejected": 0,
              "errors": [], "blocked": False, "via": "yt-dlp"}
    log = _Collect()
    base = {"quiet": True, "no_warnings": True, "skip_download": True,
            "ignoreerrors": True, "socket_timeout": 30, "logger": log}

    with yt_dlp.YoutubeDL({**base, "extract_flat": "in_playlist", "playlistend": SCAN_DEPTH}) as ydl:
        tab = ydl.extract_info(CHANNEL_STREAMS, download=False) or {}
    ids = [e["id"] for e in (tab.get("entries") or []) if e and e.get("id")]
    report["scanned"] = len(ids)

    streams = []
    todo = [v for v in ids if v not in known_ids and v not in _rejected]
    with yt_dlp.YoutubeDL({**base, "noplaylist": True, "retries": 3,
                           "youtube_include_dash_manifest": False}) as ydl:
        for vid in todo:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            report["extracted"] += 1
            if not info:
                continue  # errored (upcoming / private / members-only) — logged below
            if info.get("live_status") in _NOT_READY or not info.get("upload_date") or not info.get("duration"):
                report["not_ready"] += 1
                continue  # live now or VOD still processing — try again next cycle
            if info["duration"] <= MIN_DURATION_SEC:
                _rejected.add(vid)
                report["rejected"] += 1
                continue
            streams.append(_stream_row(vid, info))

    report["errors"] = log.errors
    # YouTube's anti-bot wall reads "Sign in to confirm you're not a bot". If the
    # tab itself came back empty with that error, the server's IP is blocked.
    report["blocked"] = any("not a bot" in e or "confirm you" in e for e in log.errors)
    return streams, report
