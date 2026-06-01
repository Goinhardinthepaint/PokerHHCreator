# Two-Pass Segmenter — Complete Specification

## Overview

Process an entire 8-hour poker livestream efficiently by splitting the work 
into two passes: a cheap boundary scan and a parallel hand analysis.

## Pass 1 — Boundary Scanner

### Purpose
Find every hand boundary in the stream and flag side games. Output a manifest 
JSON that Pass 2 uses to process only the relevant segments.

### Input
- YouTube URL
- Overlay profile (optional — for future multi-stream support)
- Transcript .vtt file (downloaded by yt-dlp)

### Process

1. Download video at 240p (lowest readable quality)
2. Extract frames at 0.2fps (one every 5 seconds)
3. For each frame:
   a. Crop to bottom-left + bottom-right strips (same as regular pipeline)
   b. Send to GPT-4o mini with a MINIMAL prompt (not the full hand analysis prompt):
   
   ```
   Look at this poker livestream overlay and answer:
   1. How many community board cards are visible? (0-5)
   2. How many player boxes are visible?
   3. Does any player have 4 hole cards? (yes/no)
   4. What is the pot amount shown? (number or null)
   Answer as JSON: {"board_count": N, "player_count": N, "is_plo": bool, "pot": N}
   ```
   
   c. This returns a tiny JSON — no full player/card/action parsing
   
4. Detect hand boundaries from the sequence:
   - board_count goes from >0 to 0 for 3+ consecutive reads = boundary
   - pot drops from large to small = boundary
   - Each boundary gets a timestamp
   
5. Scan transcript for side game markers:
   - "bomb pot", "squid game", "stand up game", "bounty", "plo", "double board"
   - Flag timestamps where these appear
   
6. Classify each hand segment:
   - NORMAL: standard NL hold'em hand
   - PLO_BOMB_POT: any frame had is_plo=true
   - SIDE_GAME: transcript flagged a side game within 120 seconds
   - BREAK: extended period with no players visible

### Output — Manifest JSON

```json
{
  "stream_url": "https://youtube.com/watch?v=...",
  "video_id": "p0Q7LW64ecM",
  "video_path": "downloads/p0Q7LW64ecM.mp4",
  "transcript_path": "downloads/p0Q7LW64ecM.en.vtt",
  "total_duration_sec": 26700,
  "scan_timestamp": "2026-05-15T18:00:00",
  "stakes": "50/100",
  "bb_ante": 100,
  
  "hands": [
    {
      "hand_index": 1,
      "start_sec": 2000,
      "end_sec": 2110,
      "duration_sec": 110,
      "type": "normal",
      "peak_pot": 51450,
      "max_players": 6
    },
    {
      "hand_index": 2,
      "start_sec": 2115,
      "end_sec": 2140,
      "duration_sec": 25,
      "type": "plo_bomb_pot",
      "peak_pot": 3000,
      "max_players": 6
    },
    {
      "hand_index": 3,
      "start_sec": 2145,
      "end_sec": 2280,
      "duration_sec": 135,
      "type": "normal",
      "peak_pot": 3500,
      "max_players": 6
    }
  ],
  
  "segments": [
    {
      "segment_id": 1,
      "start_sec": 2000,
      "end_sec": 2300,
      "hand_indices": [1, 2, 3],
      "normal_hand_count": 2,
      "type": "mixed"
    }
  ],
  
  "summary": {
    "total_hands": 147,
    "normal_hands": 128,
    "plo_bomb_pots": 8,
    "side_games": 6,
    "breaks": 5,
    "estimated_process_time_min": 15,
    "estimated_cost_usd": 12
  }
}
```

### Cost
- 8 hours at 0.2fps = 5,760 frames
- GPT-4o mini at ~$0.001/frame = ~$6
- Processing time: ~20 minutes (parallel-friendly)


## Pass 2 — Parallel Hand Processor

### Purpose
Process each normal hand at full detail and export PT4-compatible hand histories.

### Input
- Manifest JSON from Pass 1
- Video file (already downloaded)
- Transcript .vtt file

### Process

1. Read manifest, filter to type="normal" hands only
2. Group hands into chunks of 5-10 consecutive hands
3. For each chunk, create a worker that:
   a. Extracts frames at 1fps for the chunk's time window
   b. Crops frames to overlay strips
   c. For each hand in the chunk:
      - Select 10-15 key frames (spread across streets)
      - Stitch into a 4x4 grid image
      - Slice transcript to the hand's time window
      - Send grid + transcript to vision model in ONE call
      - Parse the returned hand history text
   d. Validate each hand (pot math, card uniqueness, action legality)
   e. Output validated hands to a per-chunk results file

4. Merge all chunk results in chronological order
5. Validate cross-hand consistency:
   - Button moves clockwise
   - Player stacks are consistent between hands (end of hand N ≈ start of hand N+1)
   - No duplicate hand IDs
6. Export final PT4 .txt file

### Parallelism

```python
from concurrent.futures import ThreadPoolExecutor
import math

def process_stream(manifest_path, max_workers=8):
    manifest = load_manifest(manifest_path)
    normal_hands = [h for h in manifest["hands"] if h["type"] == "normal"]
    
    # Group into chunks of 5 hands
    chunk_size = 5
    chunks = []
    for i in range(0, len(normal_hands), chunk_size):
        chunk = normal_hands[i:i+chunk_size]
        chunks.append({
            "chunk_id": i // chunk_size,
            "hands": chunk,
            "start_sec": chunk[0]["start_sec"],
            "end_sec": chunk[-1]["end_sec"],
        })
    
    # Process chunks in parallel
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(process_chunk, chunk, manifest): chunk
            for chunk in chunks
        }
        for future in as_completed(futures):
            results.append(future.result())
    
    # Merge in chronological order
    results.sort(key=lambda r: r["start_sec"])
    all_hands = []
    for r in results:
        all_hands.extend(r["hands"])
    
    # Final validation + export
    validate_session(all_hands)
    export_pt4(all_hands, manifest["stream_url"])
```

### One-Call-Per-Hand Detail

For each hand, the worker:

1. Extracts all frames for that hand's time window
2. Selects ~15 key frames:
   - Frames 1, 3, 5 of preflop (board=0)
   - Frames 1, 3, 5 of flop (board=3)
   - Frames 1, 3 of turn (board=4)
   - Frames 1, 3 of river (board=5)
   - Last 2 frames (showdown/summary)
3. Resizes each to 400x225 (small but readable for overlay text)
4. Stitches into a 4x4 grid (1600x900 total)
5. Encodes as JPEG
6. Slices transcript to hand's timestamp range
7. Sends to vision model with the hand history prompt
8. Parses the returned text
9. Runs validation

### Cost
- 128 normal hands × 1 vision call = 128 calls
- Grid image ~800x450 effective = small token count
- GPT-4o mini: ~$0.01/hand = $1.28
- Claude Sonnet: ~$0.05/hand = $6.40
- Total with Pass 1: $7-12


## CLI Interface

```bash
# Full pipeline
python main.py <url> --full-stream

# Pass 1 only (generate manifest)
python main.py <url> --scan-only

# Pass 2 only (process from existing manifest)
python main.py --manifest output/manifest.json

# Pass 2 with specific chunk (for testing)
python main.py --manifest output/manifest.json --chunk 0

# Control parallelism
python main.py <url> --full-stream --workers 4
```


## File Structure (new files)

```
src/
  segmenter/
    __init__.py
    scanner.py          # Pass 1 — boundary scanning
    manifest.py         # Manifest JSON read/write
  parallel/
    __init__.py
    worker.py           # Per-chunk processing worker
    merger.py           # Merge chunk results + cross-hand validation
  vision/
    openai_analyzer.py  # GPT-4o mini integration
    grid_builder.py     # Frame grid stitching
```


## Migration Path

1. Build scanner.py + manifest.py (Pass 1)
2. Test: run scan-only on full 7-hour stream, verify hand count
3. Build grid_builder.py + one-call-per-hand prompt
4. Test: process 5 hands with grid approach, compare to frame-by-frame
5. Build openai_analyzer.py
6. Test: same 5 hands through GPT-4o mini, compare accuracy
7. Build worker.py + merger.py (parallelism)
8. Test: full stream end-to-end
9. Optimize: tune frame selection, grid size, prompt
