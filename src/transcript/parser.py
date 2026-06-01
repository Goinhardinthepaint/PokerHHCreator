"""
Transcript Parser Module

Parses .vtt subtitle files from YouTube, chunks them by hand boundaries,
and sends each chunk to Claude API for structured hand extraction.
"""

import json
import re
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import anthropic

from src.prompts import HAND_PARSE_SYSTEM_PROMPT, HAND_PARSE_USER_PROMPT


@dataclass
class TranscriptSegment:
    """A single captioned segment with timestamp."""
    start: str        # "00:14:32.120"
    end: str          # "00:14:35.440"
    text: str         # The spoken words


def parse_vtt(vtt_path: str) -> list[TranscriptSegment]:
    """Parse a .vtt file into a list of timestamped segments."""
    segments = []
    content = Path(vtt_path).read_text(encoding="utf-8")
    
    # Remove WEBVTT header and any metadata
    content = re.sub(r"^WEBVTT.*?\n\n", "", content, flags=re.DOTALL)
    
    # Split into cue blocks
    blocks = content.strip().split("\n\n")
    
    for block in blocks:
        lines = block.strip().split("\n")
        if not lines:
            continue
        
        # Find the timestamp line
        timestamp_line = None
        text_lines = []
        for line in lines:
            if "-->" in line:
                timestamp_line = line
            elif timestamp_line is not None:
                text_lines.append(line)
        
        if timestamp_line and text_lines:
            parts = timestamp_line.split(" --> ")
            segments.append(TranscriptSegment(
                start=parts[0].strip(),
                end=parts[1].strip(),
                text=" ".join(text_lines).strip()
            ))
    
    return segments


def extract_actions_from_segments(
    segments: list[TranscriptSegment],
    player_names: list[str],
    start_sec: float,
    end_sec: float,
) -> list[dict]:
    """Scan transcript segments in [start_sec, end_sec] for player action mentions.

    Looks for patterns like "Dylan folds", "Francisco calls", "Otto checks" in
    commentary text.  Returns a list of dicts:
        {"player": str, "action": str, "seg_sec": float}

    Action types returned match the pipeline's action_type strings:
    checks / folds / calls / bets / raises / all_in
    """
    _ACTION_MAP: dict[str, str] = {
        "check": "checks", "checks": "checks",
        "fold":  "folds",  "folds":  "folds",
        "call":  "calls",  "calls":  "calls",
        "bet":   "bets",   "bets":   "bets",
        "raise": "raises", "raises": "raises",
        "allin": "all_in", "all-in": "all_in", "all in": "all_in",
    }

    if not player_names:
        return []

    # Build pattern: player name (optionally followed by "is"/"has"/"will")
    # then an action word within 25 chars.
    name_re = "|".join(re.escape(n) for n in player_names)
    pattern = re.compile(
        rf"\b({name_re})\b.{{0,25}}\b(check|fold|call|bet|raise|all[\s\-]?in)s?\b",
        re.IGNORECASE,
    )

    results: list[dict] = []
    for seg in segments:
        seg_sec = _ts_to_seconds(seg.start)
        if not (start_sec <= seg_sec <= end_sec):
            continue
        # Strip HTML tags sometimes present in VTT
        text = re.sub(r"<[^>]+>", "", seg.text)
        for m in pattern.finditer(text):
            raw = m.group(2).lower().replace(" ", "").replace("-", "")
            action = _ACTION_MAP.get(raw) or _ACTION_MAP.get(raw.rstrip("s")) or raw
            results.append({
                "player": m.group(1),
                "action": action,
                "seg_sec": seg_sec,
            })

    return results


def segments_to_text(segments: list[TranscriptSegment]) -> str:
    """Convert segments to a single text block with timestamps."""
    lines = []
    for seg in segments:
        # Truncate to MM:SS for readability
        start_short = seg.start[:8] if len(seg.start) > 8 else seg.start
        lines.append(f"[{start_short}] {seg.text}")
    return "\n".join(lines)


def chunk_by_hands(segments: list[TranscriptSegment]) -> list[list[TranscriptSegment]]:
    """Split transcript segments into chunks, one per hand.
    
    Detects hand boundaries by looking for phrases like:
    - "new hand"
    - "button moves"
    - "brand new hand"
    - "here we go" (often signals new hand)
    - "posts small blind" / "posts the blind" at start of action
    
    Returns list of segment groups, each representing one hand.
    """
    hand_boundary_patterns = [
        r"new hand",
        r"brand new hand",
        r"button moves",
        r"here we go.*button",
        r"another hand",
        r"next hand",
        r"hand number",
    ]
    boundary_regex = re.compile(
        "|".join(hand_boundary_patterns), re.IGNORECASE
    )
    
    chunks = []
    current_chunk = []
    
    for seg in segments:
        if boundary_regex.search(seg.text) and current_chunk:
            # This segment starts a new hand - save the current chunk
            chunks.append(current_chunk)
            current_chunk = [seg]
        else:
            current_chunk.append(seg)
    
    # Don't forget the last chunk
    if current_chunk:
        chunks.append(current_chunk)
    
    return chunks


def _ts_to_seconds(ts: str) -> float:
    """Convert 'HH:MM:SS.mmm' or 'HH:MM:SS' to fractional seconds."""
    parts = ts.split(":")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def parse_hands_from_transcript(
    vtt_path: str,
    model: str = "claude-sonnet-4-6",
    batch_size: int = 3,
    start_sec: float | None = None,
    end_sec: float | None = None,
) -> list[dict]:
    """Full pipeline: VTT file -> parsed hand dicts.

    Args:
        vtt_path:   Path to .vtt subtitle file
        model:      Claude model to use for parsing
        batch_size: Number of hands to send per API call (saves cost)
        start_sec:  Only process segments at or after this offset (seconds)
        end_sec:    Only process segments at or before this offset (seconds)

    Returns:
        List of parsed hand dicts ready for PT4 formatting
    """
    client = anthropic.Anthropic()

    # Parse VTT
    segments = parse_vtt(vtt_path)
    print(f"Parsed {len(segments)} transcript segments")

    # Time-window filter
    if start_sec is not None or end_sec is not None:
        segments = [
            s for s in segments
            if (start_sec is None or _ts_to_seconds(s.start) >= start_sec)
            and (end_sec is None or _ts_to_seconds(s.start) <= end_sec)
        ]
        lo = f"{start_sec}s" if start_sec is not None else "start"
        hi = f"{end_sec}s" if end_sec is not None else "end"
        print(f"  Filtered to {len(segments)} segments in window [{lo}, {hi}]")
    
    # Chunk by hand boundaries
    hand_chunks = chunk_by_hands(segments)
    print(f"Found {len(hand_chunks)} hand boundaries")
    
    all_hands = []
    
    # Process in batches
    for i in range(0, len(hand_chunks), batch_size):
        batch = hand_chunks[i:i + batch_size]
        
        # Combine batch segments into one text block
        batch_segments = []
        for chunk in batch:
            batch_segments.extend(chunk)
        
        transcript_text = segments_to_text(batch_segments)
        
        print(f"Processing hands {i+1}-{min(i+batch_size, len(hand_chunks))}...")
        
        # Call Claude API
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=HAND_PARSE_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": HAND_PARSE_USER_PROMPT.format(
                    transcript_chunk=transcript_text
                )
            }]
        )
        
        # Parse response
        response_text = response.content[0].text
        
        # Strip any markdown fencing
        response_text = re.sub(r"```json\s*", "", response_text)
        response_text = re.sub(r"```\s*$", "", response_text)
        
        try:
            result = json.loads(response_text.strip())
            hands = result.get("hands", [])
            all_hands.extend(hands)
            print(f"  -> Extracted {len(hands)} hands")
        except json.JSONDecodeError as e:
            print(f"  -> JSON parse error: {e}")
            print(f"  -> Raw response: {response_text[:500]}")
    
    return all_hands


def get_verification_timestamps(hands: list[dict]) -> list[dict]:
    """Extract timestamps that need video frame verification.
    
    Returns list of {timestamp, reason, hand_index} for frames to pull.
    Prioritizes:
    1. Low confidence hands
    2. Flagged ambiguities (missing card suits, unclear amounts)
    3. Hand start (for stack verification)
    4. Showdown (for card confirmation)
    """
    verification_points = []
    
    for i, hand in enumerate(hands):
        confidence = hand.get("confidence", {})
        overall = confidence.get("overall", 1.0)
        flags = confidence.get("flags", [])
        
        ts_start = hand.get("timestamp_start", "00:00:00")
        ts_end = hand.get("timestamp_end", "00:00:00")
        
        # Always verify hand start for stacks
        verification_points.append({
            "timestamp": ts_start,
            "reason": "hand_start_stacks",
            "hand_index": i,
            "priority": 1,
        })
        
        # Verify showdown if there is one
        if hand.get("showdown"):
            verification_points.append({
                "timestamp": ts_end,
                "reason": "showdown_cards",
                "hand_index": i,
                "priority": 1,
            })
        
        # Low confidence hands get extra frame pulls
        if overall < 0.9:
            verification_points.append({
                "timestamp": ts_start,
                "reason": f"low_confidence_{overall}",
                "hand_index": i,
                "priority": 0,
            })
        
        # Specific flags
        for flag in flags:
            if "amount" in flag.lower() or "bet" in flag.lower():
                # Need to verify bet amounts - pull mid-hand frame
                verification_points.append({
                    "timestamp": _midpoint_timestamp(ts_start, ts_end),
                    "reason": f"flag: {flag}",
                    "hand_index": i,
                    "priority": 0,
                })
    
    # Sort by priority (0 = highest) then timestamp
    verification_points.sort(key=lambda x: (x["priority"], x["timestamp"]))
    
    return verification_points


def _midpoint_timestamp(start: str, end: str) -> str:
    """Calculate the midpoint between two HH:MM:SS timestamps."""
    def to_seconds(ts: str) -> int:
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2].split(".")[0])
    
    def from_seconds(s: int) -> str:
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}"
    
    mid = (to_seconds(start) + to_seconds(end)) // 2
    return from_seconds(mid)
