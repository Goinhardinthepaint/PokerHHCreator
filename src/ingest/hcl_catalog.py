"""
HCL stream catalog — incremental refresh.

Runs on a timer inside the web server (see _stream_refresher in server.py) so new
Hustler Casino Live streams reach the calendar without anyone re-running the
scraper by hand.

The channel's /streams tab is newest-first, so only the top of it matters: flat-
list the first SCAN_DEPTH entries (one or two requests), drop every id the
catalog already has, and full-extract just the unknown ones for their date and
duration. On a normal day that's zero or one extraction, which keeps the request
count low enough not to draw YouTube's bot check onto the server's IP.

Same filter as scripts/scrape_hcl_streams.py: 2h+ streams only, ~one hand every
3 minutes for the estimate.
"""

CHANNEL_STREAMS = "https://www.youtube.com/@HustlerCasinoLive/streams"
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


def fetch_new_streams(known_ids):
    """New qualifying streams not in `known_ids`, plus a short report.

    Returns (streams, report) where report is
    {"scanned", "extracted", "not_ready", "rejected", "errors", "blocked"}.
    """
    import yt_dlp  # heavy; only loaded when a refresh actually runs

    report = {"scanned": 0, "extracted": 0, "not_ready": 0, "rejected": 0, "errors": [], "blocked": False}
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
