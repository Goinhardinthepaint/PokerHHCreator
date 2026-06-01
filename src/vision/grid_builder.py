"""
Grid builder for one-call-per-hand architecture.

Selects key frames from a hand and stitches full frames into a single
JPEG grid for a single model vision call.  No cropping — the full frame
is scaled to fit each grid cell so the HCL overlay (bottom ~40%) is
always visible.
"""

import os
from PIL import Image


def build_hand_grid(
    frame_paths: list[str],
    output_path: str,
    max_frames: int = 16,
    thumb_width: int = 320,
    thumb_height: int = 180,
    cols: int = 4,
) -> str:
    """Select key frames and stitch full frames into a grid JPEG.

    Each frame is resized to (thumb_width × thumb_height) with no
    cropping, preserving the full HCL overlay at the bottom of the frame.
    Default 320×180 keeps the 16:9 aspect ratio and produces 1280×720
    grids — large enough for GPT to read overlays, small enough for the
    API to handle multiple grids per call.

    Args:
        frame_paths:  Ordered list of JPEG paths for the hand.
        output_path:  Where to save the grid JPEG.
        max_frames:   Maximum thumbnails to include (evenly sampled).
        thumb_width:  Width of each grid cell (default 640).
        thumb_height: Height of each grid cell (default 360).
        cols:         Number of columns in the grid.

    Returns:
        output_path (the saved grid file).
    """
    existing = [p for p in frame_paths if os.path.exists(p)]
    if not existing:
        raise ValueError(f"No frame files found among {len(frame_paths)} paths")

    if len(existing) > max_frames:
        indices = [int(i * len(existing) / max_frames) for i in range(max_frames)]
        selected = [existing[i] for i in indices]
    else:
        selected = existing

    rows = (len(selected) + cols - 1) // cols
    grid = Image.new("RGB", (thumb_width * cols, thumb_height * rows), (0, 0, 0))

    for i, path in enumerate(selected):
        try:
            img = Image.open(path)
        except Exception:
            continue

        thumb = img.resize((thumb_width, thumb_height), Image.LANCZOS)
        col = i % cols
        row = i // cols
        grid.paste(thumb, (col * thumb_width, row * thumb_height))

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    grid.save(output_path, "JPEG", quality=85)
    return output_path
