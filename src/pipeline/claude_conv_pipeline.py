"""
src/pipeline/claude_conv_pipeline.py

Multi-turn conversational Claude Vision pipeline.

Instead of dumping all 20 frames in one call, this pipeline splits them into
three street-focused turns so Claude can concentrate on one task at a time:

  Turn 1 (Preflop):    first ~35% of frames
  Turn 2 (Flop):       next ~25% of frames
  Turn 3 (Turn/River): remaining frames + JSON synthesis request

Claude carries full conversation history into each turn, so it has all prior
descriptions when it writes the final JSON.

Usage:
    python -m src.pipeline.claude_conv_pipeline --hand 8   # single hand
    python -m src.pipeline.claude_conv_pipeline            # full 10-hand run
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import winreg
from pathlib import Path
from typing import Optional

import anthropic

from src.session.session_config import SessionConfig
from src.vision.hand_feeder import feed_hand, FeedError
from src.export.pt4_formatter import format_hand
from src.validate.pt4_linter import lint_hand
from src.pipeline.claude_vision_pipeline import (
    _load_api_key,
    _CARD_RE,
    _FEW_SHOT,
    _ACTION_RULES,
    _CARD_RULES,
    _JSON_SCHEMA,
)


class ClaudeConvPipeline:
    MODEL      = "claude-sonnet-4-6"
    MAX_FRAMES = 20

    # Fraction of selected frames sent per turn
    PREFLOP_FRAC = 0.35
    FLOP_FRAC    = 0.25
    # Turn/River gets the remainder

    def __init__(
        self,
        youtube_url: str,
        session_config_path: str = "src/session/session_config.json",
    ):
        self.youtube_url = youtube_url
        self.video_id    = re.search(r"v=([A-Za-z0-9_-]{11})", youtube_url).group(1)
        self.video_path  = f"downloads/{self.video_id}_1080p.webm"
        self.session     = SessionConfig.from_json(session_config_path)
        _load_api_key("ANTHROPIC_API_KEY")
        self.client      = anthropic.Anthropic()

    # ── Video / frame helpers (same as ClaudeVisionPipeline) ─────────────────

    def _download_1080p(self) -> None:
        if Path(self.video_path).exists():
            print(f"  1080p already on disk: {self.video_path}")
            return
        print("  Downloading 1080p via yt-dlp …")
        subprocess.run([
            "yt-dlp",
            "-f", "bestvideo[height=1080][ext=webm]+bestaudio/bestvideo[height=1080]",
            "--merge-output-format", "webm",
            "-o", self.video_path,
            self.youtube_url,
        ], check=True)

    def _extract_frames(
        self, start_sec: int, end_sec: int, out_dir: str, fps: float = 2.0
    ) -> list[Path]:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        pattern = str(Path(out_dir) / "raw_%04d.jpg")
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start_sec), "-t", str(end_sec - start_sec),
            "-i", self.video_path, "-vf", f"fps={fps}", "-q:v", "2", pattern,
        ], check=True, capture_output=True)
        return sorted(Path(out_dir).glob("raw_*.jpg"), key=lambda p: p.name)

    def _select_frames(self, frame_paths: list[Path], threshold: int = 8) -> list[Path]:
        import cv2
        n = len(frame_paths)
        if n == 0:
            return []
        anchor_n = min(5, n // 2)
        forced: set[int] = set(range(anchor_n)) | set(range(n - anchor_n, n))
        diff_kept: set[int] = set()
        prev_gray = None
        for i, path in enumerate(frame_paths):
            img = cv2.imread(str(path))
            if img is None:
                continue
            h = img.shape[0]
            roi = cv2.cvtColor(img[int(h * 0.60):, :], cv2.COLOR_BGR2GRAY)
            if prev_gray is None or float(cv2.mean(cv2.absdiff(roi, prev_gray))[0]) > threshold:
                diff_kept.add(i)
                prev_gray = roi
        all_indices = sorted(diff_kept | forced)
        selected = [frame_paths[i] for i in all_indices]
        if len(selected) > self.MAX_FRAMES:
            forced_paths = {frame_paths[i] for i in forced if i < n}
            non_forced   = [p for p in selected if p not in forced_paths]
            budget = max(0, self.MAX_FRAMES - len(forced_paths))
            if non_forced and budget > 0:
                step = len(non_forced) / budget
                non_forced = [non_forced[int(j * step)] for j in range(budget)]
            path_to_i = {p: i for i, p in enumerate(frame_paths)}
            combined  = list(forced_paths) + non_forced
            selected  = sorted(set(combined), key=lambda p: path_to_i.get(p, 0))
        return selected

    def _number_frames(self, frame_paths: list[Path], out_dir: str) -> list[Path]:
        import cv2
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        numbered: list[Path] = []
        for i, src in enumerate(frame_paths, 1):
            img = cv2.imread(str(src))
            if img is None:
                continue
            label = f"Frame {i}"
            font  = cv2.FONT_HERSHEY_SIMPLEX
            cv2.putText(img, label, (20, 55), font, 1.8, (0, 0, 0), 6)
            cv2.putText(img, label, (20, 55), font, 1.8, (255, 255, 255), 2)
            out_path = Path(out_dir) / f"frame_{i:02d}.jpg"
            cv2.imwrite(str(out_path), img)
            numbered.append(out_path)
        return numbered

    # ── Card sanitiser ────────────────────────────────────────────────────────

    @staticmethod
    def _sanitize_cards(data: dict) -> dict:
        def _valid(cards: list) -> bool:
            return isinstance(cards, list) and all(
                isinstance(c, str) and _CARD_RE.match(c) for c in cards
            )
        for p in (data.get("players") or []):
            hc = p.get("hole_cards")
            if hc is not None and not _valid(hc):
                p["hole_cards"] = []
        for sd in (data.get("showdown") or []):
            hc = sd.get("hole_cards")
            if hc is not None and not _valid(hc):
                sd["hole_cards"] = []

        from collections import Counter
        card_owners: dict[str, list] = {}
        for p in (data.get("players") or []):
            for c in (p.get("hole_cards") or []):
                card_owners.setdefault(c, []).append(p)
        for card, owners in card_owners.items():
            if len(owners) > 1:
                for o in owners:
                    o["hole_cards"] = []

        board_cards: set[str] = set()
        b = data.get("board") or {}
        board_cards.update(c for c in (b.get("flop") or []) if isinstance(c, str) and _CARD_RE.match(c))
        for key in ("turn", "river"):
            c = b.get(key)
            if isinstance(c, str) and _CARD_RE.match(c):
                board_cards.add(c)
        if board_cards:
            for p in (data.get("players") or []):
                hc = p.get("hole_cards") or []
                if any(c in board_cards for c in hc):
                    p["hole_cards"] = []

        return data

    # ── Frame splitting ───────────────────────────────────────────────────────

    def _split_frames(
        self, frames: list[Path]
    ) -> tuple[list[Path], list[Path], list[Path]]:
        """Split into (preflop, flop, turn_river) by fixed proportions."""
        n  = len(frames)
        n1 = max(1, round(n * self.PREFLOP_FRAC))
        n2 = max(1, round(n * self.FLOP_FRAC))
        # Ensure we don't over-allocate
        n2 = min(n2, n - n1 - 1)
        n2 = max(n2, 1)
        return frames[:n1], frames[n1:n1 + n2], frames[n1 + n2:]

    # ── Per-turn prompts ──────────────────────────────────────────────────────

    def _turn1_prompt(
        self, hand_num: int, start_sec: int, end_sec: int, n_preflop: int
    ) -> str:
        btn_seat = self.session.get_button_seat(hand_num - 1)
        sb_seat  = self.session.get_sb_seat(hand_num - 1)
        bb_seat  = self.session.get_bb_seat(hand_num - 1)
        stakes   = self.session.stakes

        def _name(seat: int) -> str:
            try:
                return self.session.get_player_name(seat)
            except KeyError:
                return f"seat {seat}"

        m0, s0 = divmod(start_sec, 60)
        m1, s1 = divmod(end_sec,   60)
        player_list = "\n".join(
            f"  {p.name}" for p in sorted(self.session._players, key=lambda p: p.seat)
        )

        return f"""\
You are analyzing {n_preflop} sequential 1080p screenshots (Frames 1–{n_preflop}) from Hustler Casino Live.
Stream: {self.youtube_url}  |  Clip: {m0}:{s0:02d} – {m1}:{s1:02d}
Stakes: ${stakes} NL with $100 BB ante spread across all players.

Players at this table:
{player_list}

POSITION ASSIGNMENTS FOR THIS HAND (authoritative):
  Button:      {_name(btn_seat)}
  Small Blind: {_name(sb_seat)}
  Big Blind:   {_name(bb_seat)}

{_FEW_SHOT}
{_ACTION_RULES}
{_CARD_RULES}

These frames cover the PREFLOP street. Please describe:
1. Who is seated at the table? List each player's seat number, name, and chip stack.
2. What are each player's hole cards? Read rank and suit directly from the overlay.
   If you cannot clearly see a player's cards, say so — never guess.
3. What is the complete preflop action in clockwise seat order?
   Include antes, SB post, BB post, straddle (if any), then all voluntary actions."""

    @staticmethod
    def _turn2_prompt(n1: int, n2: int) -> str:
        return f"""\
These are Frames {n1 + 1}–{n1 + n2} covering the FLOP. Please describe:
1. What are the 3 flop cards? Read each card's rank and suit carefully.
2. What is the complete flop action in clockwise order starting from the first active player left of the button?
   Include all checks, bets, raises, calls, and folds."""

    @staticmethod
    def _turn3_prompt(n1: int, n2: int, n_total: int) -> str:
        return f"""\
These are Frames {n1 + n2 + 1}–{n_total} covering the TURN, RIVER, and SHOWDOWN. Please describe:
1. What is the turn card? (null if not dealt)
2. What is the river card? (null if not dealt)
3. What action happened on the turn? On the river?
4. Who showed cards at showdown? What were their hole cards and hand descriptions? Who won the pot?

Now synthesize everything from our conversation into a single JSON object representing the complete hand.
Return ONLY the JSON — no preamble, no markdown fences.

{_JSON_SCHEMA}"""

    # ── Content block helpers ─────────────────────────────────────────────────

    @staticmethod
    def _frames_to_content(frames: list[Path]) -> list[dict]:
        """Convert numbered frame paths to Anthropic content blocks."""
        content: list[dict] = []
        for path in frames:
            stem = path.stem  # e.g. "frame_07"
            try:
                n = int(stem.split("_")[1])
                label = f"Frame {n}:"
            except (IndexError, ValueError):
                label = f"{path.name}:"
            with open(path, "rb") as fh:
                b64 = base64.standard_b64encode(fh.read()).decode()
            content.append({"type": "text", "text": label})
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            })
        return content

    # ── Multi-turn conversation ────────────────────────────────────────────────

    def _run_conversation(
        self,
        preflop_frames: list[Path],
        flop_frames: list[Path],
        river_frames: list[Path],
        t1_prompt: str,
        t2_prompt: str,
        t3_prompt: str,
    ) -> str:
        """
        Run 3-turn conversation, returning the final assistant response text.
        """
        messages: list[dict] = []

        # Turn 1: preflop
        t1_content = self._frames_to_content(preflop_frames)
        t1_content.append({"type": "text", "text": t1_prompt})
        messages.append({"role": "user", "content": t1_content})
        resp1 = self.client.messages.create(
            model=self.MODEL, max_tokens=2048, messages=messages
        )
        messages.append({"role": "assistant", "content": resp1.content[0].text})

        # Turn 2: flop
        t2_content = self._frames_to_content(flop_frames)
        t2_content.append({"type": "text", "text": t2_prompt})
        messages.append({"role": "user", "content": t2_content})
        resp2 = self.client.messages.create(
            model=self.MODEL, max_tokens=2048, messages=messages
        )
        messages.append({"role": "assistant", "content": resp2.content[0].text})

        # Turn 3: turn/river/showdown + JSON synthesis
        t3_content = self._frames_to_content(river_frames)
        t3_content.append({"type": "text", "text": t3_prompt})
        messages.append({"role": "user", "content": t3_content})
        resp3 = self.client.messages.create(
            model=self.MODEL, max_tokens=4096, messages=messages
        )
        return resp3.content[0].text

    @staticmethod
    def _parse_json(text: str) -> dict:
        text = re.sub(r"```(?:json)?\s*", "", text).replace("```", "")
        start = text.find("{")
        if start == -1:
            raise ValueError("No JSON object in response")
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start: i + 1])
        raise ValueError("Unclosed JSON object in response")

    # ── Per-hand orchestration ─────────────────────────────────────────────────

    def process_hand(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        preextracted_frames: Optional[list[Path]] = None,
    ) -> tuple[Optional[str], Optional[dict], Optional[str]]:
        """
        Process one hand window through the conversational stack.
        Returns (hhtext, hand_data, error_string).
        """
        btn_seat = self.session.get_button_seat(hand_num - 1)
        print(f"\n{'─'*60}")
        print(f"Hand {hand_num}  [{start_sec}s – {end_sec}s]  BTN=seat {btn_seat}")
        print(f"{'─'*60}")

        work_dir = f"downloads/frames/conv_hand_{hand_num:02d}"

        if preextracted_frames is not None:
            selected = list(preextracted_frames)
            print(f"  Using {len(selected)} pre-extracted frames")
        else:
            print("  Extracting frames at 2fps …")
            raw = self._extract_frames(start_sec, end_sec, work_dir + "/raw")
            print(f"  {len(raw)} raw frames extracted")
            print("  Selecting changed-overlay frames …")
            selected = self._select_frames(raw)
            print(f"  {len(selected)} frames kept")

        if not selected:
            return None, None, "No frames selected"

        print(f"  Numbering {len(selected)} frames …")
        numbered = self._number_frames(selected, work_dir + "/numbered")

        preflop_f, flop_f, river_f = self._split_frames(numbered)
        n1, n2, n3 = len(preflop_f), len(flop_f), len(river_f)
        print(f"  Frame split: {n1} preflop / {n2} flop / {n3} turn+river")

        t1 = self._turn1_prompt(hand_num, start_sec, end_sec, n1)
        t2 = self._turn2_prompt(n1, n2)
        t3 = self._turn3_prompt(n1, n2, len(numbered))

        print(f"  Running 3-turn conversation with {self.MODEL} …")
        try:
            final_text = self._run_conversation(preflop_f, flop_f, river_f, t1, t2, t3)
        except Exception as exc:
            return None, None, f"Claude API error: {exc}"

        print("  Parsing JSON from final turn …")
        try:
            hand_data = self._parse_json(final_text)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"  [warn] JSON parse failed: {exc}")
            print(f"  Response (first 800 chars):\n{final_text[:800]}")
            return None, None, f"JSON parse error: {exc}"

        hand_data = self._sanitize_cards(hand_data)
        hand_data["button_seat"] = btn_seat

        print("  Feeding through state machine …")
        try:
            pt4 = feed_hand(
                hand_data,
                bb_ante=self.session.bb_ante,
                stakes=self.session.stakes,
                button_seat=btn_seat,
            )
        except (FeedError, ValueError) as exc:
            return None, hand_data, f"FeedError: {exc}"

        try:
            hhtext = format_hand(
                pt4,
                stream_url=self.youtube_url,
                hand_index=hand_num,
                start_sec=start_sec,
                end_sec=end_sec,
            )
        except Exception as exc:
            return None, hand_data, f"Formatter error: {exc}"

        result = lint_hand(hhtext, hand_num)
        status = "PASS" if result.passed else "FAIL"
        print(f"  Linter: [{status}]")
        for v in result.violations:
            print(f"    {v}")

        return hhtext, hand_data, None

    # ── Full run ──────────────────────────────────────────────────────────────

    def run(
        self,
        hand_windows: list[tuple[int, int]],
        output_path: str = "output/hands_conv.txt",
    ) -> list[str]:
        self._download_1080p()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        texts: list[str] = []
        for i, (start, end) in enumerate(hand_windows, 1):
            hhtext, _, err = self.process_hand(i, start, end)
            if hhtext:
                texts.append(hhtext)
            else:
                print(f"  ✗ Hand {i} error: {err}")
        combined = "\n\n".join(texts)
        Path(output_path).write_text(combined, encoding="utf-8")
        print(f"\nWrote {len(texts)} hand(s) to {output_path}")
        return texts


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config_path = "src/session/session_config.json"
    with open(config_path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    youtube_url = cfg.get("stream_url", "")
    if not youtube_url:
        print("stream_url not set in session_config.json")
        sys.exit(1)

    pipeline = ClaudeConvPipeline(youtube_url, config_path)

    hand_windows = [
        (2000, 2120), (2120, 2300), (2300, 2520), (2520, 2700),
        (2700, 2880), (2880, 3060), (3060, 3240), (3240, 3420),
        (3420, 3600), (3600, 3780),
    ]

    args = sys.argv[1:]

    if "--hand" in args:
        idx  = args.index("--hand")
        nums = []
        for tok in args[idx + 1:]:
            if tok.startswith("--"):
                break
            try:
                nums.append(int(tok))
            except ValueError:
                pass
        if not nums:
            print("--hand requires at least one hand number")
            sys.exit(1)
        pipeline._download_1080p()
        texts: list[str] = []
        for n in nums:
            if not (1 <= n <= len(hand_windows)):
                print(f"  Hand {n} out of range — skipping")
                continue
            start, end = hand_windows[n - 1]
            hhtext, _, err = pipeline.process_hand(n, start, end)
            if hhtext:
                texts.append(hhtext)
            else:
                print(f"  Error: {err}")
        if texts:
            out = Path("output/hands_conv.txt")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n\n".join(texts), encoding="utf-8")
            print(f"\nWrote {len(texts)} hand(s) to {out}")
        return

    pipeline.run(hand_windows)


if __name__ == "__main__":
    main()
