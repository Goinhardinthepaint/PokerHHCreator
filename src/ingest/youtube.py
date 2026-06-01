"""
YouTube Ingestion Module

Downloads transcripts and video from YouTube using yt-dlp.
Supports both auto-generated captions and manual subtitles.
"""

import subprocess
import json
import os
import sys
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

YT_DLP = [sys.executable, "-m", "yt_dlp"]


@dataclass
class IngestResult:
    """Result of ingesting a YouTube video."""
    video_path: Optional[str]    # Path to downloaded video (None if skip_video)
    transcript_path: Optional[str]  # Path to .vtt subtitle file
    video_id: str
    title: str
    duration: int                # Duration in seconds
    url: str


def ingest_youtube(
    url: str,
    output_dir: str = "downloads",
    skip_video: bool = False,
    video_quality: str = "240p",
    subtitle_lang: str = "en",
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> IngestResult:
    """Download transcript and optionally video from a YouTube URL.

    Args:
        url:          YouTube video URL
        output_dir:   Directory to save downloaded files
        skip_video:   If True, only download subtitles (fast, no video)
        video_quality: Video quality label (informational); download targets 240p
        subtitle_lang: Subtitle language code
        start_sec:    Start of the time window to download (seconds); None = beginning
        end_sec:      End of the time window to download (seconds); None = end

    Returns:
        IngestResult with paths to downloaded files
    """
    os.makedirs(output_dir, exist_ok=True)

    # First, get video metadata
    meta = _get_video_info(url)
    video_id = meta.get("id", "unknown")
    title = meta.get("title", "Unknown Stream")
    duration = meta.get("duration", 0)

    print(f"Video: {title}")
    print(f"Duration: {duration // 3600}h {(duration % 3600) // 60}m")
    print(f"Video ID: {video_id}")
    if start_sec is not None or end_sec is not None:
        lo = f"{start_sec}s" if start_sec is not None else "0s"
        hi = f"{end_sec}s" if end_sec is not None else "end"
        print(f"Time window: [{lo}, {hi}]")

    # Download subtitles (always full file; we filter segments in Python)
    transcript_path = _download_subtitles(
        url, output_dir, video_id, subtitle_lang
    )

    # Download video (if needed), clipped to the requested window
    video_path = None
    if not skip_video:
        try:
            video_path = _download_video(
                url, output_dir, video_id, video_quality, start_sec, end_sec
            )
        except RuntimeError as exc:
            print(f"WARNING: Video download failed — continuing transcript-only.")
            print(f"  Reason: {exc}")

    return IngestResult(
        video_path=video_path,
        transcript_path=transcript_path,
        video_id=video_id,
        title=title,
        duration=duration,
        url=url,
    )


def _get_video_info(url: str) -> dict:
    """Get video metadata without downloading."""
    cmd = YT_DLP + [
        "--dump-json",
        "--no-download",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get video info: {result.stderr}")
    return json.loads(result.stdout)


def _download_subtitles(
    url: str, output_dir: str, video_id: str, lang: str
) -> Optional[str]:
    """Download auto-generated or manual subtitles as .vtt."""
    output_template = os.path.join(output_dir, f"{video_id}")
    
    # Try auto-generated subs first (most livestreams have these)
    cmd = YT_DLP + [
        "--write-auto-sub",
        "--sub-lang", lang,
        "--sub-format", "vtt",
        "--skip-download",
        "-o", output_template,
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    # Check for the output file
    vtt_path = f"{output_template}.{lang}.vtt"
    if os.path.exists(vtt_path):
        print(f"Downloaded auto-generated subtitles: {vtt_path}")
        return vtt_path
    
    # Try manual subs as fallback
    cmd[len(YT_DLP)] = "--write-sub"
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if os.path.exists(vtt_path):
        print(f"Downloaded manual subtitles: {vtt_path}")
        return vtt_path
    
    print("WARNING: No subtitles found. Will need to use Whisper or video-only mode.")
    return None


def _download_video(
    url: str,
    output_dir: str,
    video_id: str,
    quality: str,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> str:
    """Download video at specified quality, optionally clipped to a time window."""
    # Encode window into the output filename so re-runs with different windows
    # don't overwrite each other.
    if start_sec is not None or end_sec is not None:
        lo = int(start_sec or 0)
        hi = int(end_sec or 0)
        output_template = os.path.join(output_dir, f"{video_id}_{lo}-{hi}.%(ext)s")
    else:
        output_template = os.path.join(output_dir, f"{video_id}.%(ext)s")

    # Determine the expected base filename so we can find the file reliably.
    if start_sec is not None or end_sec is not None:
        lo = int(start_sec or 0)
        hi = int(end_sec or 0)
        expected_base = f"{video_id}_{lo}-{hi}"
    else:
        expected_base = video_id

    _video_exts = {".mp4", ".webm", ".mkv", ".m4v"}

    # Return cached file if it already exists (skip re-download).
    for f in os.listdir(output_dir):
        stem, ext = os.path.splitext(f)
        if stem == expected_base and ext.lower() in _video_exts:
            path = os.path.join(output_dir, f)
            size_mb = os.path.getsize(path) / (1024 * 1024)
            print(f"Using cached video: {path} ({size_mb:.1f} MB)")
            return path

    # If no windowed clip cached but the full video is present, use it.
    # ffmpeg's time_offset + duration args in extract_frames will handle windowing.
    if expected_base != video_id:
        for f in os.listdir(output_dir):
            stem, ext = os.path.splitext(f)
            if stem == video_id and ext.lower() in _video_exts:
                path = os.path.join(output_dir, f)
                size_mb = os.path.getsize(path) / (1024 * 1024)
                print(f"Using full cached video (ffmpeg will seek to window): {path} ({size_mb:.1f} MB)")
                return path

    cmd = YT_DLP + [
        "-f", "best[height<=240][ext=mp4]/best[height<=240]",
        "-o", output_template,
    ]

    # Clip download to the requested window using yt-dlp's --download-sections
    if start_sec is not None or end_sec is not None:
        lo = start_sec or 0
        hi = end_sec or 9999999
        cmd += ["--download-sections", f"*{lo}-{hi}"]

    cmd.append(url)

    print("Downloading video clip (240p for frame extraction)...")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"Failed to download video: {result.stderr}")

    # Find the downloaded file by its expected base name.
    for f in os.listdir(output_dir):
        stem, ext = os.path.splitext(f)
        if stem == expected_base and ext.lower() in _video_exts:
            path = os.path.join(output_dir, f)
            size_mb = os.path.getsize(path) / (1024 * 1024)
            print(f"Downloaded video: {path} ({size_mb:.1f} MB)")
            return path

    raise RuntimeError(f"Video download completed but file not found (expected: {expected_base}.*)")
