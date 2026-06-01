"""
src/pipeline/claude_vision_pipeline.py

Claude Vision-powered hand history pipeline.

Extracts 1080p frames from a poker livestream, uses absdiff to keep only
frames where the HCL overlay changed, numbers them, and sends them to Claude
Sonnet for structured extraction.  The JSON response is fed through the same
PokerStateMachine → PT4 formatter → linter stack as the Gemini pipeline.

Usage:
    python -m src.pipeline.claude_vision_pipeline              # full 10-hand run
    python -m src.pipeline.claude_vision_pipeline --test-scan  # Steve/Airball flop test
    python -m src.pipeline.claude_vision_pipeline --hand 8     # single hand
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


# ── API key bootstrap ─────────────────────────────────────────────────────────

def _load_api_key(name: str) -> str:
    val = os.environ.get(name)
    if val:
        return val
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            val, _ = winreg.QueryValueEx(key, name)
        os.environ[name] = val
        return val
    except (FileNotFoundError, OSError):
        raise RuntimeError(f"{name} not set in environment or Windows registry")


# ── Constants ─────────────────────────────────────────────────────────────────

# Valid card token: rank [2-9TJQKA] + suit [cdhs]
_CARD_RE = re.compile(r'^[2-9TJQKA][cdhs]$')

_BLIND_TYPES = frozenset({"posts_ante", "posts_sb", "posts_bb", "posts_straddle"})


# ── Prompt components ─────────────────────────────────────────────────────────

_FEW_SHOT = """\
FEW-SHOT EXAMPLE — Human-verified extraction from this same stream (33:20–35:20):
  Table: Airball seat 2, Greedo seat 3, Francisco seat 4, Dylan seat 5, Otto seat 6, Alex seat 9
  Button: seat 3 (Greedo)
  Straddle: Airball (CO) $200
  Preflop : Greedo raises $600 | Francisco folds | Dylan folds | Otto calls $600
            | Alex raises $3000 | Airball folds | Greedo calls | Otto calls
  Flop    : Kc 9c 2h
  Postflop: Otto checks | Alex bets $3000 | Greedo folds | Otto raises all-in $21000 | Alex calls
  Run it twice — Board 1 turn+river: Kd 7c | Board 2 turn+river: Jc 5d
  Result  : Otto wins both boards with flush (Tc 8c)
  Hole cards (read directly from overlay — note suit carefully):
    Airball:   8h 2d   (eight of HEARTS red, two of DIAMONDS red)
    Greedo:    Ad 4d   (ace of DIAMONDS red, four of DIAMONDS red)
    Francisco: Qd 7d   (queen of DIAMONDS red, seven of DIAMONDS red)
    Dylan:     Ts 5c   (ten of SPADES black, five of CLUBS black)
    Otto:      Tc 8c   (ten of CLUBS black, eight of CLUBS black — NOT spades)
    Alex:      Ah 5s   (ace of HEARTS red, five of SPADES black)
"""

_ACTION_RULES = """\
CRITICAL — ACTION ORDER RULES:
- Preflop WITH straddle: action starts LEFT of the straddler going clockwise.
- Preflop WITHOUT straddle: action starts LEFT of the BB (UTG), going clockwise
  around to SB then BB last.
- Postflop: action starts with the first active player LEFT of the button.
- List EVERY player's action on EVERY street in strict seat order. Include folds.
- A player who already called CANNOT fold unless someone raises after their call.
- ALL-IN RULE: When players are all-in, board cards are still dealt face-up.
  Include turn and river cards even when no betting occurs — they are shown on screen.
"""

_CARD_RULES = """\
CARD READING — be exact about suits:
  Red suits:   hearts ♥ (rounded pip) vs diamonds ♦ (pointy/angular pip)
  Black suits: spades ♠ (curved pip with stem) vs clubs ♣ (three-bubble pip)
  Read rank and suit directly from the on-screen graphic. Never guess.
  IMPORTANT: If you cannot clearly read a player's hole cards, set hole_cards to []
  (empty array). NEVER use X, ?, placeholders, or invented cards.

RUN-IT-TWICE: If the hand runs out more than one board, report ONLY the first board
  in the "board" fields (flop/turn/river). Describe both results in "notes".
"""

_JSON_SCHEMA = """\
Return ONLY a single JSON object — no markdown fences, no preamble, no trailing text.

{
  "players": [
    {
      "seat": <int — use the seat number shown in the overlay>,
      "name": "<string>",
      "stack": <int chips at start of hand>,
      "position": "<BTN|SB|BB|UTG|UTG+1|LJ|HJ|CO|STR>",
      "hole_cards": ["<card>", "<card>"]
    }
  ],
  "action": {
    "preflop": [
      {"player": "<name>", "action": "<posts_ante|posts_sb|posts_bb|posts_straddle|folds|checks|calls|bets|raises|all_in>", "amount": <int or 0>}
    ],
    "flop":  [...same format...],
    "turn":  [...],
    "river": [...]
  },
  "board": {
    "flop":  ["<card>", "<card>", "<card>"],
    "turn":  "<card or null>",
    "river": "<card or null>"
  },
  "showdown": [
    {"player": "<name>", "hole_cards": ["<card>", "<card>"], "hand_description": "<string>", "result": "<wins|loses>"}
  ],
  "pot": <int — final pot>,
  "notes": "<optional observations>"
}

Card format: rank (2–9, T, J, Q, K, A) + suit (c=clubs, d=diamonds, h=hearts, s=spades)
Examples: Ac=ace of clubs, Td=ten of diamonds, 9h=nine of hearts, 2s=two of spades
"""


# ── Pipeline ──────────────────────────────────────────────────────────────────

class ClaudeVisionPipeline:
    MODEL = "claude-sonnet-4-6"
    # Max frames per API call (stay well under Anthropic's per-request limit)
    MAX_FRAMES = 20

    def __init__(
        self,
        youtube_url: str,
        session_config_path: str = "src/session/session_config.json",
    ):
        self.youtube_url    = youtube_url
        self.video_id       = re.search(r"v=([A-Za-z0-9_-]{11})", youtube_url).group(1)
        self.video_path     = f"downloads/{self.video_id}_1080p.webm"
        self.session        = SessionConfig.from_json(session_config_path)
        _load_api_key("ANTHROPIC_API_KEY")
        self.client         = anthropic.Anthropic()

    # ── Video / frame helpers ─────────────────────────────────────────────────

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
        self,
        start_sec: int,
        end_sec: int,
        out_dir: str,
        fps: float = 2.0,
    ) -> list[Path]:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        pattern = str(Path(out_dir) / "raw_%04d.jpg")
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start_sec),
            "-t",  str(end_sec - start_sec),
            "-i",  self.video_path,
            "-vf", f"fps={fps}",
            "-q:v", "2",
            pattern,
        ], check=True, capture_output=True)
        return sorted(Path(out_dir).glob("raw_*.jpg"), key=lambda p: p.name)

    def _select_frames(
        self,
        frame_paths: list[Path],
        threshold: int = 8,
    ) -> list[Path]:
        """
        Keep frames where the bottom 40% changed significantly vs the previous
        kept frame (threshold lowered to 8 to catch position-label frames).

        Always force-include the first 5 and last 5 frames so that position
        labels (shown at hand start) and showdown cards (shown at end) are
        never dropped.  Caps total at MAX_FRAMES.
        """
        import cv2  # noqa: PLC0415

        n = len(frame_paths)
        if n == 0:
            return []

        # Force-include first and last 5 (or n//2 each to avoid overlap)
        anchor_n = min(5, n // 2)
        forced: set[int] = set(range(anchor_n)) | set(range(n - anchor_n, n))

        # absdiff scan — compare bottom 40% of each frame to the previous kept frame
        diff_kept: set[int] = set()
        prev_gray = None

        for i, path in enumerate(frame_paths):
            img = cv2.imread(str(path))
            if img is None:
                continue
            h = img.shape[0]
            roi_gray = cv2.cvtColor(img[int(h * 0.60):, :], cv2.COLOR_BGR2GRAY)

            if prev_gray is None or float(cv2.mean(cv2.absdiff(roi_gray, prev_gray))[0]) > threshold:
                diff_kept.add(i)
                prev_gray = roi_gray

        all_indices = sorted(diff_kept | forced)
        selected = [frame_paths[i] for i in all_indices]

        # Cap at MAX_FRAMES: keep all forced anchors + evenly-spaced non-anchors
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
        """Burn 'Frame N' label into top-left of each frame; return numbered paths."""
        import cv2  # noqa: PLC0415

        Path(out_dir).mkdir(parents=True, exist_ok=True)
        numbered: list[Path] = []
        for i, src in enumerate(frame_paths, 1):
            img = cv2.imread(str(src))
            if img is None:
                continue
            label = f"Frame {i}"
            font  = cv2.FONT_HERSHEY_SIMPLEX
            # Black outline then white fill for visibility on any background
            cv2.putText(img, label, (20, 55), font, 1.8, (0, 0, 0), 6)
            cv2.putText(img, label, (20, 55), font, 1.8, (255, 255, 255), 2)
            out_path = Path(out_dir) / f"frame_{i:02d}.jpg"
            cv2.imwrite(str(out_path), img)
            numbered.append(out_path)
        return numbered

    # ── Prompt ────────────────────────────────────────────────────────────────

    def _build_prompt(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        n_frames: int,
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

        btn_name = _name(btn_seat)
        sb_name  = _name(sb_seat)
        bb_name  = _name(bb_seat)

        m0, s0 = divmod(start_sec, 60)
        m1, s1 = divmod(end_sec,   60)

        player_list = "\n".join(
            f"  {p.name}" for p in sorted(self.session._players, key=lambda p: p.seat)
        )

        return f"""\
You are analyzing {n_frames} sequential 1080p screenshots from Hustler Casino Live.
Stream: {self.youtube_url}  |  Clip: {m0}:{s0:02d} – {m1}:{s1:02d}

Stakes: ${stakes} NL with $100 BB ante spread across all players.

Players at this table:
{player_list}

POSITION ASSIGNMENTS FOR THIS HAND (authoritative — do not override from overlay):
  Button:      {btn_name}
  Small Blind: {sb_name}
  Big Blind:   {bb_name}

Do not guess SB/BB from the overlay graphic. Use exactly the assignments above.
SB is always the first occupied seat clockwise from the button; BB is next.

{_FEW_SHOT}
{_ACTION_RULES}
{_CARD_RULES}
Extract the complete hand — hole cards, all streets, winner.
Use seat numbers as shown in the HCL overlay graphic for the "seat" field.

{_JSON_SCHEMA}"""

    # ── Claude API ────────────────────────────────────────────────────────────

    def _call_claude(self, frame_paths: list[Path], prompt: str) -> str:
        content: list[dict] = []
        for i, path in enumerate(frame_paths, 1):
            with open(path, "rb") as fh:
                b64 = base64.standard_b64encode(fh.read()).decode()
            content.append({"type": "text", "text": f"Frame {i}:"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": b64,
                },
            })
        content.append({"type": "text", "text": prompt})

        resp = self.client.messages.create(
            model=self.MODEL,
            max_tokens=4096,
            messages=[{"role": "user", "content": content}],
        )
        return resp.content[0].text

    # ── JSON parsing ──────────────────────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict:
        """Extract the first complete JSON object from Claude's response."""
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
                    return json.loads(text[start : i + 1])
        raise ValueError("Unclosed JSON object in response")

    # ── Fix 1: card sanitizer ─────────────────────────────────────────────────

    @staticmethod
    def _sanitize_cards(data: dict) -> dict:
        """
        Strip hole_cards arrays containing any non-standard token (X, ?, etc.).
        Applied to both players and showdown entries so invalid cards never reach
        the state machine or the linter.
        """
        def _valid(cards: list) -> bool:
            return isinstance(cards, list) and all(
                isinstance(c, str) and _CARD_RE.match(c) for c in cards
            )

        for p in (data.get("players") or []):
            hc = p.get("hole_cards")
            if hc is not None and not _valid(hc):
                print(f"  [sanitize] Dropping invalid hole_cards for {p.get('name')!r}: {hc}")
                p["hole_cards"] = []

        for sd in (data.get("showdown") or []):
            hc = sd.get("hole_cards")
            if hc is not None and not _valid(hc):
                print(f"  [sanitize] Dropping invalid showdown cards for {sd.get('player')!r}: {hc}")
                sd["hole_cards"] = []

        # Duplicate card check: if the same card appears in two players' hands,
        # clear both — a hallucinated duplicate is worse than unknown cards.
        from collections import Counter
        card_owners: dict[str, list] = {}
        for p in (data.get("players") or []):
            for c in (p.get("hole_cards") or []):
                card_owners.setdefault(c, []).append(p)
        for card, owners in card_owners.items():
            if len(owners) > 1:
                names = [o.get("name") for o in owners]
                print(f"  [sanitize] Duplicate card {card!r} in {names} — clearing all")
                for o in owners:
                    o["hole_cards"] = []

        # Board-vs-hand duplicate check
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
                dupes = [c for c in hc if c in board_cards]
                if dupes:
                    print(f"  [sanitize] Card(s) {dupes} in {p.get('name')!r} also in board — clearing")
                    p["hole_cards"] = []

        return data

    # ── Fix 4: preflop action sort ────────────────────────────────────────────

    def _sort_preflop_actions(self, data: dict, hand_num: int) -> dict:
        """
        Re-sort voluntary preflop actions into correct clockwise seat order
        starting from UTG (or left of straddle if one exists).

        Only applied when each player has at most ONE voluntary action (i.e. a
        simple single-raise-or-less preflop).  When any player appears twice
        (multi-raise sub-rounds) we preserve Claude's original ordering so we
        don't produce consecutive duplicate-player lines.
        """
        preflop: list[dict] = list(data.get("action", {}).get("preflop") or [])
        if not preflop:
            return data

        btn_seat      = self.session.get_button_seat(hand_num - 1)
        session_seats = self.session._seats()
        if btn_seat not in session_seats:
            return data

        btn_idx = session_seats.index(btn_seat)
        n       = len(session_seats)

        name_to_seat = {p.name.casefold(): p.seat for p in self.session._players}

        blind_acts = [a for a in preflop if a.get("action") in _BLIND_TYPES]
        voluntary  = [a for a in preflop if a.get("action") not in _BLIND_TYPES]

        # Skip sort if any player acts more than once — multi-raise sequences
        # must keep Claude's original interleaving to avoid consecutive duplicates.
        from collections import Counter
        if voluntary and max(Counter((a.get("player") or "").casefold() for a in voluntary).values()) > 1:
            return data

        straddle = next((a for a in blind_acts if a.get("action") == "posts_straddle"), None)
        if straddle:
            str_seat = name_to_seat.get((straddle.get("player") or "").casefold())
            start_idx = (
                (session_seats.index(str_seat) + 1) % n
                if str_seat and str_seat in session_seats
                else (btn_idx + 3) % n
            )
        else:
            start_idx = (btn_idx + 3) % n

        seat_priority = {session_seats[(start_idx + i) % n]: i for i in range(n)}

        def _key(a: dict) -> int:
            seat = name_to_seat.get((a.get("player") or "").casefold(), 999)
            return seat_priority.get(seat, 999)

        voluntary.sort(key=_key)
        data["action"]["preflop"] = blind_acts + voluntary
        return data

    # ── Per-hand orchestration ────────────────────────────────────────────────

    def _single_attempt(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        numbered: list[Path],
        prompt: str,
        btn_seat: int,
    ) -> tuple[Optional[str], Optional[dict], Optional[str], Optional[object]]:
        """
        One Claude call through the full stack.
        Returns (hhtext, hand_data, error_string, lint_result).
        lint_result is None on any error before linting.
        """
        print(f"  Calling Claude Vision ({self.MODEL}) …")
        try:
            raw_text = self._call_claude(numbered, prompt)
        except Exception as exc:
            return None, None, f"Claude API error: {exc}", None

        print("  Parsing JSON …")
        try:
            hand_data = self._parse_json(raw_text)
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"  [warn] JSON parse failed: {exc}")
            return None, None, f"JSON parse error: {exc}", None

        hand_data = self._sanitize_cards(hand_data)
        hand_data = self._sort_preflop_actions(hand_data, hand_num)
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
            return None, hand_data, f"FeedError: {exc}", None

        try:
            hhtext = format_hand(
                pt4,
                stream_url=self.youtube_url,
                hand_index=hand_num,
                start_sec=start_sec,
                end_sec=end_sec,
            )
        except Exception as exc:
            return None, hand_data, f"Formatter error: {exc}", None

        result = lint_hand(hhtext, hand_num)
        return hhtext, hand_data, None, result

    def process_hand(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        preextracted_frames: Optional[list[Path]] = None,
    ) -> tuple[Optional[str], Optional[dict], Optional[str]]:
        """
        Process one hand window through the full stack.
        Returns (hhtext, hand_data, error_string).
        preextracted_frames bypasses extraction+selection (for testing).
        """
        btn_seat = self.session.get_button_seat(hand_num - 1)
        print(f"\n{'─'*60}")
        print(f"Hand {hand_num}  [{start_sec}s – {end_sec}s]  BTN=seat {btn_seat}")
        print(f"{'─'*60}")

        work_dir = f"downloads/frames/cv_hand_{hand_num:02d}"

        if preextracted_frames is not None:
            selected = list(preextracted_frames)
            print(f"  Using {len(selected)} pre-extracted frames")
        else:
            print("  Extracting frames at 2fps …")
            raw = self._extract_frames(start_sec, end_sec, work_dir + "/raw")
            print(f"  {len(raw)} raw frames extracted")

            print("  Selecting changed-overlay frames (threshold=8, anchors=first/last 5) …")
            selected = self._select_frames(raw)
            print(f"  {len(selected)} frames kept")

        if not selected:
            return None, None, "No frames selected"

        print(f"  Numbering {len(selected)} frames …")
        numbered = self._number_frames(selected, work_dir + "/numbered")

        prompt = self._build_prompt(hand_num, start_sec, end_sec, len(numbered))

        hhtext, hand_data, err, lint_result = self._single_attempt(
            hand_num, start_sec, end_sec, numbered, prompt, btn_seat
        )

        if err is not None or (lint_result is not None and not lint_result.passed):
            label = err if err else "linter FAIL"
            print(f"  [attempt 1: {label}] — retrying …")
            hhtext2, hand_data2, err2, lint_result2 = self._single_attempt(
                hand_num, start_sec, end_sec, numbered, prompt, btn_seat
            )
            if err2 is None:
                hhtext, hand_data, err, lint_result = hhtext2, hand_data2, err2, lint_result2

        if err is not None:
            return None, hand_data, err

        status = "PASS" if lint_result.passed else "FAIL"
        print(f"  Linter: [{status}]")
        for v in lint_result.violations:
            print(f"    {v}")

        return hhtext, hand_data, None

    # ── Full run ──────────────────────────────────────────────────────────────

    def run(
        self,
        hand_windows: list[tuple[int, int]],
        output_path: str = "output/hands_claude_vision.txt",
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

    pipeline = ClaudeVisionPipeline(youtube_url, config_path)

    args = sys.argv[1:]

    # ── --test-scan: use the pre-extracted scan_022–032 frames ───────────────
    if "--test-scan" in args:
        frames_dir = Path("downloads/frames/test_greedo_kk")
        scan_frames = sorted(
            p for p in frames_dir.glob("scan_0*.jpg")
            if 22 <= int(p.stem.split("_")[1]) <= 32
        )
        if not scan_frames:
            print(f"No scan frames found in {frames_dir}")
            sys.exit(1)
        print(f"Test mode: {len(scan_frames)} scan frames from {frames_dir}")

        hhtext, hand_data, err = pipeline.process_hand(
            hand_num=8,
            start_sec=3240,
            end_sec=3420,
            preextracted_frames=scan_frames,
        )

        out = Path("output/test_claude_vision.txt")
        out.parent.mkdir(parents=True, exist_ok=True)

        if hhtext:
            out.write_text(hhtext, encoding="utf-8")
            print(f"\n{'='*60}\nPT4 OUTPUT:\n{'='*60}")
            print(hhtext)
            print(f"\nWritten to {out}")
        else:
            print(f"\nError: {err}")
            if hand_data:
                print("\nHand data Claude returned:")
                print(json.dumps(hand_data, indent=2))
        return

    # ── --hand N [N ...]: one or more specific hands ──────────────────────────
    hand_windows = [
        (2000, 2120), (2120, 2300), (2300, 2520), (2520, 2700),
        (2700, 2880), (2880, 3060), (3060, 3240), (3240, 3420),
        (3420, 3600), (3600, 3780),
    ]

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
            out = Path("output/hands_claude_vision.txt")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("\n\n".join(texts), encoding="utf-8")
            print(f"\nWrote {len(texts)} hand(s) to {out}")
        return

    # ── Full run ──────────────────────────────────────────────────────────────
    pipeline.run(hand_windows)


if __name__ == "__main__":
    main()
