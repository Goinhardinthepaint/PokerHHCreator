"""
Frame extractor.

Uses ffmpeg to pull JPEG frames from a downloaded video clip at a given FPS.
Timestamps returned are offset to reflect position in the original stream,
not the local file (important when the clip was downloaded with --download-sections).
"""

import os
import subprocess
from pathlib import Path


def extract_frames(
    video_path: str,
    output_dir: str,
    fps: float = 1.0,
    time_offset_sec: float = 0.0,
    duration_sec: float | None = None,
    seek_sec: float = 0.0,
) -> list[tuple[str, str]]:
    """Extract frames from a video file using ffmpeg.

    Args:
        video_path:       Path to the .mp4 file.
        output_dir:       Directory to write frame JPEGs into (created if absent).
        fps:              Frames to extract per second of video content.
        time_offset_sec:  Stream-time (seconds) of the first frame in the file.
                          When the file was downloaded with yt-dlp
                          --download-sections, it starts at local t=0 but
                          represents stream time `time_offset_sec`. Pass
                          start_sec here so returned timestamps are correct.
        duration_sec:     If set, stop extraction after this many seconds of
                          video content (i.e. ffmpeg -t). Use this when the
                          video file covers more content than the desired window.

    Returns:
        Sorted list of (image_path, "HH:MM:SS.mmm") pairs where the timestamp
        reflects position in the original stream.

    Raises:
        RuntimeError: if ffmpeg fails or produces no output frames.
    """
    os.makedirs(output_dir, exist_ok=True)
    output_pattern = os.path.join(output_dir, "frame_%06d.jpg")

    cmd = ["ffmpeg"]
    if seek_sec > 0:
        cmd += ["-ss", str(seek_sec)]   # fast seek before input
    cmd += [
        "-i", video_path,
        "-vf", f"fps={fps}",
        "-q:v", "2",   # high-quality JPEG (1=best, 31=worst)
        "-y",          # overwrite if re-running
    ]
    if duration_sec is not None:
        cmd += ["-t", str(duration_sec)]
    cmd.append(output_pattern)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg frame extraction failed:\n{result.stderr[-2000:]}"
        )

    frame_paths = sorted(Path(output_dir).glob("frame_*.jpg"))
    if not frame_paths:
        raise RuntimeError(f"ffmpeg ran successfully but produced no frames in {output_dir}")

    interval = 1.0 / fps
    pairs: list[tuple[str, str]] = []
    for i, path in enumerate(frame_paths):
        stream_sec = time_offset_sec + i * interval
        h = int(stream_sec // 3600)
        m = int((stream_sec % 3600) // 60)
        s = stream_sec % 60
        ts = f"{h:02d}:{m:02d}:{s:06.3f}"
        pairs.append((str(path), ts))

    return pairs
