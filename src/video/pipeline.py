"""
Video pipeline.

Coordinates frame-level vision results into discrete hands, filters side games,
and converts the result into the hand-dict format consumed by the export layer.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from src.video.detector import classify_boundary, is_side_game
from src.vision.analyzer import names_match
from src.export.hand_evaluator import evaluate_best_hand
from src.transcript.parser import extract_actions_from_segments, parse_vtt, _ts_to_seconds


# ── Hand state ─────────────────────────────────────────────────────────────────

@dataclass
class HandState:
    """Accumulates frame reads for a single hand while it is in progress."""
    hand_index: int
    timestamp_start: str
    frames: list[dict] = field(default_factory=list)
    is_side_game: bool = False
    side_game_type: Optional[str] = None
    transcript_segments: list = field(default_factory=list)  # TranscriptSegment list

    def add_frame(self, frame_result: dict) -> None:
        self.frames.append(frame_result)
        if frame_result.get("is_side_game"):
            self.is_side_game = True
            self.side_game_type = frame_result.get("side_game_type", "side_game")

    def all_hole_cards(self) -> list[list[str]]:
        seen = []
        for frame in self.frames:
            for player in frame.get("players") or []:
                cards = player.get("hole_cards")
                if cards:
                    seen.append(cards)
        return seen


# ── Boundary detection ─────────────────────────────────────────────────────────
#
# Board-clear debounce: a single empty-board frame is often a camera-angle
# change or overlay transition, not a genuine hand reset.  We require
# BOARD_EMPTY_THRESHOLD consecutive empty-board frames before declaring a
# new-hand boundary.  Using exact equality (== threshold) means the boundary
# fires exactly once per clearance and never re-triggers during a long stretch
# of empty frames.
#
# Pot-reset: a pot collapsing to near-zero after being large is unambiguous
# (the winner collected); this fires immediately without debouncing.

_BOARD_EMPTY_THRESHOLD = 3

# PLO detection thresholds: require ≥2 frames above minimum confidence to
# confirm PLO, so a single low-quality overhead shot (conf≈0.05) from the
# tail of a previous PLO hand doesn't false-positive the current hand.
_PLO_CONFIDENCE_MIN = 0.5
_PLO_FRAME_MIN = 2
_MIN_FRAMES = 8           # fewer frames → likely an overlay fragment, not a real hand
_MIN_PLAYERS_WITH_CARDS = 2  # single-player overlay fragments have no opponent


# ── Side-game filtering ────────────────────────────────────────────────────────

def _has_plo_hole_cards(hand: HandState) -> bool:
    plo_frames = 0
    for frame in hand.frames:
        if (frame.get("confidence") or 0) < _PLO_CONFIDENCE_MIN:
            continue
        for player in frame.get("players") or []:
            cards = player.get("hole_cards") or []
            count = player.get("hole_card_count") or len(cards)
            if count > 2:
                plo_frames += 1
                break  # one PLO player per frame is sufficient
    return plo_frames >= _PLO_FRAME_MIN


_PLACEHOLDER_NAMES_GLOBAL = {"unknown", "player", "hero", "villain", ""}


def _board_card_set(hand: HandState) -> frozenset[str]:
    cards: set[str] = set()
    for f in hand.frames:
        for c in (f.get("board") or []):
            if c:
                cards.add(c.upper())
    return frozenset(cards)


def _player_name_set(hand: HandState) -> frozenset[str]:
    names: set[str] = set()
    for f in hand.frames:
        for p in (f.get("players") or []):
            name = (p.get("name") or "").strip().casefold()
            if name and name not in _PLACEHOLDER_NAMES_GLOBAL:
                names.add(name)
    return frozenset(names)


def _handle_completed_hand(
    hand: HandState,
    completed: list[HandState],
    skipped: list[HandState],
) -> None:
    """Keep or discard a completed hand.

    Skips when:
    - Hand has board cards but no preflop frames (replay/overlay fragment), OR
    - Vision analyzer flagged is_side_game on any frame, OR
    - Any player held more than 2 hole cards (PLO detection).
    """
    # A sequence that shows board cards but never showed an empty board is almost
    # certainly a replay graphic that played after the real hand concluded.
    has_preflop = any(len(f.get("board") or []) == 0 for f in hand.frames)
    has_board_cards = any(len(f.get("board") or []) > 0 for f in hand.frames)
    if not has_preflop and has_board_cards:
        print(
            f"  [skip] Hand {hand.hand_index + 1} has no preflop frames "
            f"— likely a replay overlay, skipping"
        )
        skipped.append(hand)
        return

    if hand.is_side_game:
        print(
            f"  [skip] Hand {hand.hand_index + 1} flagged as side game "
            f"({hand.side_game_type}) by vision analyzer"
        )
        skipped.append(hand)
        return

    if _has_plo_hole_cards(hand):
        hand.is_side_game = True
        hand.side_game_type = "plo_bomb_pot"
        print(
            f"  [skip] Hand {hand.hand_index + 1} has >2 hole cards per player "
            f"— marking as plo_bomb_pot and skipping"
        )
        skipped.append(hand)
        return

    # Filter: too few frames → overlay fragment
    if len(hand.frames) < _MIN_FRAMES:
        print(
            f"  [skip] Hand {hand.hand_index + 1} has only {len(hand.frames)} frame(s) "
            f"— below {_MIN_FRAMES}-frame minimum, skipping"
        )
        skipped.append(hand)
        return

    # Filter: fewer than 2 distinct players with hole cards → single-overlay fragment
    players_with_cards: set[str] = set()
    for f in hand.frames:
        if (f.get("confidence") or 0) < 0.5:
            continue
        for p in (f.get("players") or []):
            name = (p.get("name") or "").strip().casefold()
            hc = p.get("hole_card_count") or len(p.get("hole_cards") or [])
            if name and name not in _PLACEHOLDER_NAMES_GLOBAL and hc > 0:
                players_with_cards.add(name)
    if len(players_with_cards) < _MIN_PLAYERS_WITH_CARDS:
        print(
            f"  [skip] Hand {hand.hand_index + 1} has only {len(players_with_cards)} "
            f"player(s) with hole cards — single-overlay fragment, skipping"
        )
        skipped.append(hand)
        return

    # Filter: zero pot → no real action captured
    max_pot = max((f.get("pot") or 0 for f in hand.frames), default=0)
    if max_pot <= 0:
        print(
            f"  [skip] Hand {hand.hand_index + 1} has zero pot — fragment, skipping"
        )
        skipped.append(hand)
        return

    completed.append(hand)


# ── Pipeline ───────────────────────────────────────────────────────────────────

class VideoPipeline:
    """Segments frame-analysis results into discrete hands and filters side games."""

    def process(
        self,
        frame_results: list[dict],
        timestamps: list[str],
        transcript_side_game_sec: set[int] | None = None,
        transcript_segments: list | None = None,
    ) -> tuple[list[HandState], list[HandState]]:
        """Process pre-analyzed frame dicts into completed hands.

        Boundary detection uses two signals in priority order:
        1. Visual state change (board cleared, pot reset) between consecutive frames.
        2. Text cues embedded in the frame's "notes" field by Claude Vision.

        Args:
            frame_results:            List of dicts from FrameAnalyzer.analyze_frame().
            timestamps:               Matching HH:MM:SS.mmm string for each frame.
            transcript_side_game_sec: Set of stream-seconds where transcript text
                                      flagged a side game. Hands whose start time
                                      falls within 120 s of any entry are skipped.
            transcript_segments:      Optional list of TranscriptSegment objects from
                                      parse_vtt(), used for action supplementation.

        Returns:
            (completed_hands, skipped_hands)
        """
        completed: list[HandState] = []
        skipped: list[HandState] = []
        current: Optional[HandState] = None
        hand_index = 0
        side_game_times = transcript_side_game_sec or set()

        # Debounce state
        board_empty_streak: int = 0   # consecutive frames with no board cards
        prev_had_board: bool = False   # True once a non-empty board was seen
        prev_pot: float = 0            # pot from previous frame for reset-detection;
                                       # explicitly set to 0 after every boundary so
                                       # the next frame never sees a spurious drop

        for i, (frame, ts) in enumerate(zip(frame_results, timestamps)):
            board = frame.get("board") or []
            board_empty = len(board) == 0
            curr_pot = frame.get("pot") or 0

            # --- Update streak counter ---
            if board_empty:
                board_empty_streak += 1
            else:
                board_empty_streak = 0
                prev_had_board = True

            # --- Board-clear boundary (debounced) ---
            # Fires exactly once when the streak hits the threshold.
            board_cleared = prev_had_board and board_empty_streak == _BOARD_EMPTY_THRESHOLD
            if board_cleared:
                prev_had_board = False  # reset so we don't re-fire on longer streaks

            # --- Pot-reset boundary (immediate) ---
            # prev_pot is reset to 0 after every boundary, so if a board-clear
            # and a pot-collapse happen on adjacent frames only one boundary fires.
            pot_reset = prev_pot > 2000 and curr_pot < prev_pot * 0.20

            # --- Text-cue boundary ---
            text_boundary = classify_boundary(frame.get("notes") or "")

            is_boundary = board_cleared or pot_reset or text_boundary.is_boundary

            if is_boundary and current is not None:
                _handle_completed_hand(current, completed, skipped)
                current = None

            if current is None:
                current = HandState(hand_index=hand_index, timestamp_start=ts)
                hand_index += 1
                if text_boundary.is_side_game or frame.get("is_side_game"):
                    current.is_side_game = True
                    current.side_game_type = (
                        text_boundary.side_game_type
                        or frame.get("side_game_type")
                        or "side_game"
                    )

            current.add_frame(frame)
            # Advance pot baseline: 0 after a boundary so the next frame can't
            # inherit a large pre-boundary pot and fire a spurious pot-reset.
            prev_pot = 0 if is_boundary else curr_pot

        # Flush last in-progress hand
        if current is not None:
            _handle_completed_hand(current, completed, skipped)

        # Cross-reference transcript side-game timestamps
        if side_game_times:
            clean: list[HandState] = []
            for hand in completed:
                hand_sec = _ts_to_sec(hand.timestamp_start)
                near = any(abs(hand_sec - sg) < 120 for sg in side_game_times)
                if near:
                    hand.is_side_game = True
                    hand.side_game_type = "transcript_flagged"
                    print(
                        f"  [skip] Hand {hand.hand_index + 1} at {hand.timestamp_start} "
                        f"flagged by nearby transcript side-game text"
                    )
                    skipped.append(hand)
                else:
                    clean.append(hand)
            completed = clean

        # Deduplicate consecutive hands with the same board cards + same players.
        # The HCL overlay carousel sometimes causes the same hand to be detected
        # twice in a row (different card subsets per carousel rotation).
        # Keep whichever has more frames.
        deduped: list[HandState] = []
        for hand in completed:
            if deduped:
                prev = deduped[-1]
                prev_board = _board_card_set(prev)
                curr_board = _board_card_set(hand)
                prev_players = _player_name_set(prev)
                curr_players = _player_name_set(hand)
                if (
                    prev_board and curr_board and prev_board == curr_board
                    and prev_players and curr_players and prev_players == curr_players
                ):
                    if len(hand.frames) > len(prev.frames):
                        print(
                            f"  [dedup] Hand {hand.hand_index + 1} duplicates Hand "
                            f"{prev.hand_index + 1} — keeping Hand {hand.hand_index + 1} "
                            f"({len(hand.frames)} frames > {len(prev.frames)})"
                        )
                        deduped[-1] = hand
                    else:
                        print(
                            f"  [dedup] Hand {hand.hand_index + 1} duplicates Hand "
                            f"{prev.hand_index + 1} — keeping Hand {prev.hand_index + 1} "
                            f"({len(prev.frames)} frames >= {len(hand.frames)})"
                        )
                    continue
            deduped.append(hand)
        completed = deduped

        # Attach transcript segments to each completed hand by time window
        if transcript_segments:
            all_ts = timestamps  # HH:MM:SS.mmm for each frame
            for hand in completed:
                hand_start_sec = _ts_to_sec(hand.timestamp_start)
                # Find the last frame that belongs to this hand by matching
                # timestamp_start to the timestamps list
                try:
                    start_idx = next(
                        i for i, ts in enumerate(all_ts)
                        if ts == hand.timestamp_start
                    )
                except StopIteration:
                    start_idx = 0
                # End = timestamp of the last frame in hand.frames (stored in each frame)
                hand_end_sec = max(
                    (_ts_to_sec(f.get("timestamp") or hand.timestamp_start)
                     for f in hand.frames),
                    default=hand_start_sec + 300,
                )
                # Add a small buffer so late-street commentary is included
                hand.transcript_segments = [
                    s for s in transcript_segments
                    if hand_start_sec - 10 <= _ts_to_seconds(s.start) <= hand_end_sec + 30
                ]

        print(
            f"Video pipeline: {len(completed)} hand(s) kept, "
            f"{len(skipped)} side game(s) skipped"
        )
        return completed, skipped


# ── Hand-state → export dict conversion ───────────────────────────────────────

def hand_states_to_dicts(
    completed: list[HandState],
    stream_url: str = "",
    hints_list: list[dict] | None = None,
    bb_ante: int = 0,
) -> list[dict]:
    result = []
    for i, hand in enumerate(completed):
        hints = hints_list[i] if (hints_list and i < len(hints_list)) else None
        # Session-level bb_ante applies to every hand unless the per-hand hint
        # already carries an explicit bb_ante (e.g. from reexport.py hints).
        if bb_ante and not (hints and hints.get("bb_ante")):
            hints = dict(hints or {})
            hints["bb_ante"] = bb_ante
        d = _hand_state_to_dict(
            hand, stream_url=stream_url, hand_index=i, hints=hints,
            transcript_segments=hand.transcript_segments or [],
        )
        if d:
            result.append(d)
    return result


def _hand_state_to_dict(
    hand: HandState, stream_url: str, hand_index: int, hints: dict | None = None,
    transcript_segments: list | None = None,
) -> dict | None:
    if not hand.frames:
        return None

    def _board_size(f: dict) -> int:
        return len(f.get("board") or [])

    # ── Stakes: majority vote across frames ───────────────────────────────────
    stakes_votes: list[str] = []
    for f in hand.frames:
        raw = (f.get("stakes") or "").replace("$", "").strip()
        m = re.search(r"(\d+)\s*/\s*(\d+)", raw)
        if m:
            stakes_votes.append(f"{m.group(1)}/{m.group(2)}")
    stakes = Counter(stakes_votes).most_common(1)[0][0] if stakes_votes else "50/100"
    try:
        # "sb/bb" or triple-blind "sb/bb/mandatory_straddle" — take the first two.
        _p = stakes.split("/")
        sb, bb = int(_p[0]), int(_p[1])
    except Exception:
        sb, bb = 50, 100

    # ── Players: best-scoring entry per unique name ───────────────────────────
    # Low-confidence frames and placeholder names are excluded.
    _PLACEHOLDER_NAMES = {"unknown", "player", "hero", "villain", ""}
    player_best: dict[str, dict] = {}
    for f in hand.frames:
        if (f.get("confidence") or 0) < 0.5:
            continue
        for p in f.get("players") or []:
            name = p.get("name")
            if not name or name.strip().casefold() in _PLACEHOLDER_NAMES:
                continue
            key = name.strip().casefold()
            p_score = sum(1 for v in (p.get("stack"), p.get("position"), p.get("hole_cards")) if v)
            e_score = sum(
                1 for v in (
                    (player_best.get(key) or {}).get("stack"),
                    (player_best.get(key) or {}).get("position"),
                    (player_best.get(key) or {}).get("hole_cards"),
                ) if v
            ) if key in player_best else -1
            if key not in player_best or p_score > e_score:
                player_best[key] = p

    players = [
        {
            "name": p.get("name") or f"Player{idx + 1}",
            "seat": idx + 1,
            "position": p.get("position") or "",
            "stack": p.get("stack") or 0,
            "hole_cards": p.get("hole_cards"),
        }
        for idx, p in enumerate(player_best.values())
    ]

    # Fix SB/BB mislabel: vision sometimes labels both SB and BB as "SB".
    # First SB-labeled player is the real SB; second is the BB.
    sb_seen: list[int] = []
    for idx, player in enumerate(players):
        if player["position"].upper() == "SB":
            sb_seen.append(idx)
    if len(sb_seen) >= 2:
        players[sb_seen[1]]["position"] = "BB"

    # ── Button seat ───────────────────────────────────────────────────────────
    button_seat = next(
        (p["seat"] for p in players if p.get("position", "").upper() == "BTN"),
        1,
    )

    # ── Board ────────────────────────────────────────────────────────────────
    # Use the frame with the most board cards; handle partial flop reads by
    # allowing 2-card flop sources when 3-card sources are unavailable.
    board_frames = sorted(
        [f for f in hand.frames if _board_size(f) > 0],
        key=lambda f: (_board_size(f), f.get("confidence") or 0),
        reverse=True,
    )
    flop_src  = next((f for f in board_frames if _board_size(f) >= 2), None)
    turn_src  = next((f for f in board_frames if _board_size(f) >= 4), None)
    river_src = next((f for f in board_frames if _board_size(f) >= 5), None)

    flop_cards = (flop_src.get("board") or [])[:3] if flop_src else []
    turn_card  = ((turn_src.get("board") or [])[3:4] or [None])[0] if turn_src else None
    river_card = ((river_src.get("board") or [])[4:5] or [None])[0] if river_src else None

    # ── Pot ──────────────────────────────────────────────────────────────────
    pot_total = max((f.get("pot") or 0 for f in hand.frames), default=0)

    # ── Action inference ─────────────────────────────────────────────────────
    action, folded_players, ante_spread = _infer_actions_from_frames(
        hand.frames, sb, bb, _PLACEHOLDER_NAMES, hints=hints,
        transcript_segments=transcript_segments or [],
        player_names=[p["name"] for p in players],
    )

    # ── Showdown: players visible in the final board street ───────────────────
    postflop_names: set[str] = set()
    final_board_threshold = max(
        (len(f.get("board") or []) for f in hand.frames), default=0
    )
    for f in hand.frames:
        if len(f.get("board") or []) < final_board_threshold:
            continue
        if (f.get("confidence") or 0) < 0.5:
            continue
        for p in f.get("players") or []:
            name = (p.get("name") or "").strip()
            if name and name.casefold() not in _PLACEHOLDER_NAMES:
                postflop_names.add(name.casefold())
    postflop_names -= folded_players

    board_card_set: set[str] = {c for c in flop_cards if c}
    if turn_card:
        board_card_set.add(turn_card)
    if river_card:
        board_card_set.add(river_card)

    # ── Winner: largest stack gain across the hand ────────────────────────────
    start_stacks: dict[str, int] = {}
    for f in hand.frames:
        if (f.get("confidence") or 0) < 0.5:
            continue
        if len(f.get("board") or []) > 0:
            break
        for p in f.get("players") or []:
            name = (p.get("name") or "").strip().casefold()
            stk = p.get("stack")
            if name and name not in _PLACEHOLDER_NAMES and stk and name not in start_stacks:
                start_stacks[name] = stk

    end_stacks: dict[str, int] = {}
    for f in reversed(hand.frames):
        if (f.get("confidence") or 0) < 0.5:
            continue
        for p in f.get("players") or []:
            name = (p.get("name") or "").strip().casefold()
            stk = p.get("stack")
            if name and name not in _PLACEHOLDER_NAMES and stk is not None and name not in end_stacks:
                end_stacks[name] = stk

    winner_key: str | None = None
    best_gain = 0
    for name_key, end_stk in end_stacks.items():
        start_stk = start_stacks.get(name_key, end_stk)
        gain = end_stk - start_stk
        if gain > best_gain:
            best_gain = gain
            winner_key = name_key

    # ── Showdown hand evaluation ──────────────────────────────────────────────
    showdown: list[dict] = []
    seen_names: list[str] = []
    for frame in reversed(hand.frames):
        if (frame.get("confidence") or 0) < 0.5:
            continue
        for p in frame.get("players") or []:
            name = p.get("name")
            if not name or name.strip().casefold() in _PLACEHOLDER_NAMES:
                continue
            if name.strip().casefold() not in postflop_names:
                continue
            cards = [c for c in (p.get("hole_cards") or []) if c not in board_card_set]
            if len(cards) != 2:
                continue
            if not any(names_match(name, s) for s in seen_names):
                showdown.append({
                    "player": name,
                    "hole_cards": cards,
                    "hand_description": "",
                    "result": "lost",
                })
                seen_names.append(name)

    full_board_eval = [
        c for c in
        flop_cards + ([turn_card] if turn_card else []) + ([river_card] if river_card else [])
        if c
    ]
    if showdown and len(full_board_eval) >= 3:
        scored: list[tuple[tuple, dict]] = []
        for sd in showdown:
            score, desc = evaluate_best_hand(sd["hole_cards"], full_board_eval)
            sd["hand_description"] = desc
            scored.append((score, sd))
        best_score = max(s for s, _ in scored)
        for score, sd in scored:
            sd["result"] = "wins" if score == best_score else "lost"

    # ── Confidence / flags ───────────────────────────────────────────────────
    avg_conf = sum(f.get("confidence") or 0 for f in hand.frames) / len(hand.frames)
    notes_flags = [
        f.get("notes")
        for f in hand.frames
        if f.get("notes") and f.get("notes") != "null"
    ]
    flags = ["actions inferred from video frames; no transcript action sequence"]
    flags.extend(notes_flags[:5])

    return {
        "hand_number": hand_index + 1,
        "players_in_hand": len(players),
        "timestamp_start": hand.timestamp_start,
        "timestamp_end": hand.frames[-1].get("timestamp") or hand.timestamp_start,
        "game": {
            "stakes": stakes,
            "ante": 0,
            "game_type": "NL Hold'em",
            "max_players": len(players) or 6,
        },
        "button_seat": button_seat,
        "players": players,
        "action": action,
        "board": {
            "flop": flop_cards,
            "turn": turn_card,
            "river": river_card,
        },
        "showdown": showdown,
        "pot": {"total": pot_total},
        "bb_ante_spread": ante_spread,
        "confidence": {
            "overall": round(avg_conf, 2),
            "flags": flags,
        },
    }


# ── Action inference ──────────────────────────────────────────────────────────
#
# Standard clockwise seat order used to build dynamic preflop order.
# Postflop order is always SB-first, clockwise to BTN.

_BASE_CLOCKWISE    = ["UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN", "SB", "BB"]
_POSTFLOP_POS_ORDER = ["SB", "BB", "UTG", "UTG+1", "UTG+2", "LJ", "HJ", "CO", "BTN"]


def _build_preflop_order(straddle_pos: str | None) -> list[str]:
    """Return preflop action order given the straddler's original position label.

    Rule: start from the seat immediately left of the straddler, go clockwise,
    straddler acts last (appended as "STR").

    Examples (from SPEC.md):
      BTN straddle → SB, BB, UTG, UTG+1, …, CO, STR
      CO straddle  → BTN, SB, BB, UTG, UTG+1, …, HJ, STR
    """
    if not straddle_pos or straddle_pos not in _BASE_CLOCKWISE:
        return list(_BASE_CLOCKWISE)
    idx = _BASE_CLOCKWISE.index(straddle_pos)
    return _BASE_CLOCKWISE[idx + 1:] + _BASE_CLOCKWISE[:idx] + ["STR"]


def _sort_actions_by_round(
    raw_actions: list[dict],
    position_map: dict[str, str],
    pos_order: list[str],
) -> list[dict]:
    """Round-aware position sort.

    Aggressors (raises / all-in / bets) appear in temporal (frame_idx) order.
    Passive responses (calls / folds / checks) are sorted by position within
    each round, starting from the seat immediately left of the aggressor.

    Calls are assigned to rounds by amount rather than frame_idx because OCR
    sometimes captures a call overlay after the NEXT raise has already appeared
    on screen.  A call with total C belongs to the smallest raise R where R >= C.
    """
    def prank(act: dict) -> int:
        pos = position_map.get(act["player"].casefold(), "")
        try:
            return pos_order.index(pos)
        except ValueError:
            return len(pos_order)

    aggressors = sorted(
        [a for a in raw_actions if a["action"] in ("raises", "all_in", "bets")],
        key=lambda a: a["_frame_idx"],
    )
    passives = [a for a in raw_actions if a["action"] not in ("raises", "all_in", "bets")]

    raise_thresholds = sorted(
        (a["amount"] or 0, i)
        for i, a in enumerate(aggressors)
        if (a["amount"] or 0) > 0
    )

    def call_round(act: dict) -> int:
        amt = act.get("amount") or 0
        for raise_amt, agg_idx in raise_thresholds:
            if raise_amt >= amt:
                return agg_idx
        return len(aggressors) - 1 if aggressors else 0

    result: list[dict] = []
    assigned_ids: set[int] = set()
    n = len(pos_order)

    if aggressors:
        first_agg_frame = aggressors[0]["_frame_idx"]
        pre_open = [
            a for a in passives
            if a["action"] != "calls" and a["_frame_idx"] < first_agg_frame
        ]
        pre_open.sort(key=prank)
        result.extend(pre_open)
        assigned_ids.update(id(a) for a in pre_open)

    for i, agg in enumerate(aggressors):
        result.append(agg)
        agg_rank = prank(agg)
        next_agg_frame = (
            aggressors[i + 1]["_frame_idx"] if i + 1 < len(aggressors) else float("inf")
        )

        frame_responses = [
            a for a in passives
            if id(a) not in assigned_ids
            and a["action"] != "calls"
            and a["_frame_idx"] > agg["_frame_idx"]
            and a["_frame_idx"] < next_agg_frame
        ]
        amt_responses = [
            a for a in passives
            if id(a) not in assigned_ids
            and a["action"] == "calls"
            and call_round(a) == i
        ]
        responses = frame_responses + amt_responses
        responses.sort(key=lambda a: ((prank(a) - agg_rank - 1) % n, a["_frame_idx"]))
        result.extend(responses)
        assigned_ids.update(id(a) for a in responses)

    remaining = [a for a in passives if id(a) not in assigned_ids]
    remaining.sort(key=lambda a: (prank(a), a["_frame_idx"]))
    result.extend(remaining)

    return result


def _parse_action_text(raw: str | None) -> tuple[str | None, int | None]:
    """Normalise a player's action_text overlay to (action_type, amount).

    Returns (None, None) for informational text ("X TO CALL", "OPTION").
    """
    if not raw:
        return None, None
    t = raw.strip().upper()
    if "TO CALL" in t or t == "OPTION":
        return None, None
    m = re.search(r"\$([\d,]+)", t)
    amount = int(m.group(1).replace(",", "")) if m else None
    if "ALL-IN" in t or "ALL IN" in t:
        return "all_in", amount
    if "3-BET" in t or "RE-RAISE" in t or "RERAISE" in t:
        return "raises", amount
    if "RAISE" in t:
        return "raises", amount
    if "BET" in t:
        return "bets", amount
    if "CALL" in t:
        return "calls", amount
    if "CHECK" in t:
        return "checks", None
    if "FOLD" in t:
        return "folds", None
    return None, None


def _infer_actions_from_frames(
    frames: list[dict], sb: int, bb: int, placeholder_names: set[str],
    hints: dict | None = None,
    transcript_segments: list | None = None,
    player_names: list[str] | None = None,
) -> tuple[dict, set[str], dict | None]:
    """Reconstruct the action sequence from per-frame action_text overlays.

    Returns:
        action_dict        — {street: [action, …]} with blind postings prepended to preflop
        folded_set         — casefold names of all players who folded
        ante_spread_info   — {"ante_per_player": N, "bb_ante": M} or None
    """
    conf_frames = [f for f in frames if (f.get("confidence") or 0) >= 0.5]
    if not conf_frames:
        return {"preflop": [], "flop": [], "turn": [], "river": []}, set(), None

    # ── Street segmentation: monotone forward only ────────────────────────────
    # If board count drops (camera cut), stay on the current street.
    street_frames: dict[str, list[tuple[int, dict]]] = {
        "preflop": [], "flop": [], "turn": [], "river": []
    }
    highest_board = 0
    for i, f in enumerate(conf_frames):
        n = len(f.get("board") or [])
        if n > highest_board:
            highest_board = n
        if highest_board == 0:
            street = "preflop"
        elif highest_board <= 3:
            street = "flop"
        elif highest_board == 4:
            street = "turn"
        else:
            street = "river"
        street_frames[street].append((i, f))

    # ── Blind posting detection ───────────────────────────────────────────────
    # Scan first 15 preflop frames for SB/BB position labels.
    # Vision bug: sometimes both SB and BB are labeled "SB" — first is real SB,
    # second is BB.
    blind_by_player: dict[str, dict] = {}
    sb_count = 0
    for _, f in street_frames["preflop"][:15]:
        for p in (f.get("players") or []):
            name = (p.get("name") or "").strip()
            pos  = (p.get("position") or "").upper()
            if not name or name.casefold() in placeholder_names:
                continue
            key = name.casefold()
            if key in blind_by_player or pos not in ("SB", "BB"):
                continue
            if pos == "BB":
                blind_by_player[key] = {"player": name, "action": "posts_bb", "amount": bb}
            else:
                sb_count += 1
                if sb_count == 1:
                    blind_by_player[key] = {"player": name, "action": "posts_sb", "amount": sb}
                else:
                    blind_by_player[key] = {"player": name, "action": "posts_bb", "amount": bb}

    # ── Straddle detection ────────────────────────────────────────────────────
    # Check first 8 preflop frames for a player with cur_bet >= 2×bb and no
    # action_text (forced post, not a voluntary raise).
    #
    # Disambiguation (SPEC §"Straddle vs Button Overlay"):
    #   BTN cur_bet ≥ 2×bb is a common OCR overlay artifact — exclude BTN unless
    #   the vision model explicitly gave the player a "STR" position label.
    #   Priority: STR-labeled > non-BTN.  BTN-only candidate → no straddle.
    straddle_player: str | None = None
    straddle_amount: int = 0
    bb_ante: int = 0
    ante_per_player: int = 0

    _str_cands: dict[str, tuple[str, int, str]] = {}  # key → (name, cur_bet, pos)
    for _, f in street_frames["preflop"][:8]:
        for p in (f.get("players") or []):
            name = (p.get("name") or "").strip()
            at   = (p.get("action_text") or "").strip()
            cb   = p.get("current_bet") or 0
            key  = name.casefold()
            pos  = (p.get("position") or "").upper()
            if not name or key in placeholder_names or key in blind_by_player:
                continue
            if at:  # action_text present → voluntary action, not a forced post
                continue
            if cb >= 2 * bb and key not in _str_cands:
                _str_cands[key] = (name, cb, pos)

    for _pref in ("STR_LABEL", "NON_BTN"):
        for _key, (_name, _cb, _pos) in _str_cands.items():
            if _pref == "STR_LABEL" and "STR" not in _pos:
                continue
            if _pref == "NON_BTN" and _pos == "BTN":
                continue
            straddle_player = _name
            straddle_amount = _cb
            break
        if straddle_player:
            break

    # ── Hints override (for hands where OCR can't detect straddle) ───────────
    if hints:
        if hints.get("straddle_player"):
            straddle_player = hints["straddle_player"]
            straddle_amount = hints.get("straddle_amount", straddle_amount)
        if hints.get("bb_ante"):
            bb_ante = hints["bb_ante"]

    # ── BB ante detection ─────────────────────────────────────────────────────
    # Requires straddle to be known first.
    # bb_ante = first_valid_pot - sb - bb - straddle
    if straddle_player and not (hints and hints.get("bb_ante")):
        for _, f in street_frames["preflop"][:10]:
            pot = f.get("pot") or 0
            if pot:
                bb_ante = max(0, pot - sb - bb - straddle_amount)
                break

    # ── Position map ──────────────────────────────────────────────────────────
    # position_map: original labels (used for postflop sort).
    # preflop_position_map: same but overrides straddler → "STR" (acts last preflop).
    # Postflop: straddler acts in their real seat position (BTN straddle → BTN slot).
    position_map: dict[str, str] = {}
    sb_first_seen: list[str] = []
    for _, f in street_frames["preflop"][:20]:
        for p in (f.get("players") or []):
            name = (p.get("name") or "").strip()
            pos  = (p.get("position") or "").upper()
            key  = name.casefold()
            if name and pos and key not in placeholder_names and key not in position_map:
                position_map[key] = pos
                if pos == "SB":
                    sb_first_seen.append(key)
    if len(sb_first_seen) >= 2:
        position_map[sb_first_seen[1]] = "BB"

    straddle_original_pos = (
        position_map.get(straddle_player.casefold(), "") if straddle_player else ""
    )
    preflop_order = _build_preflop_order(straddle_original_pos)
    preflop_position_map = dict(position_map)
    if straddle_player:
        preflop_position_map[straddle_player.casefold()] = "STR"

    # ── BB ante → equal-spread conversion ────────────────────────────────────
    # PT4 does not support BB-only ante.  Spread bb_ante equally across all
    # players; adjust starting stacks in the formatter to compensate.
    # ante_per_player = ceil(bb_ante / num_players)  (matches spec example: 100/6→17)
    if bb_ante:
        all_pf: list[tuple[str, str]] = []  # (casefold_key, display_name)
        seen_pf: set[str] = set()
        for _, f in street_frames["preflop"]:
            for p in (f.get("players") or []):
                n = (p.get("name") or "").strip()
                k = n.casefold()
                if n and k not in placeholder_names and k not in seen_pf:
                    seen_pf.add(k)
                    all_pf.append((k, n))

        num_players = len(all_pf) or 6
        ante_per_player = math.ceil(bb_ante / num_players)

        def _pos_rank_base(key: str) -> int:
            pos = position_map.get(key, "")
            try:
                return _BASE_CLOCKWISE.index(pos)
            except ValueError:
                return len(_BASE_CLOCKWISE)

        all_pf.sort(key=lambda kn: _pos_rank_base(kn[0]))
        antes_list: list[dict] = [
            {"player": n, "action": "posts_ante", "amount": ante_per_player}
            for _, n in all_pf
        ]
    else:
        antes_list = []

    # Assemble blind_actions in spec order: antes → SB → BB → straddle
    sb_act = next((a for a in blind_by_player.values() if a["action"] == "posts_sb"), None)
    bb_act = next((a for a in blind_by_player.values() if a["action"] == "posts_bb"), None)
    blind_actions: list[dict] = []
    blind_actions.extend(antes_list)
    if sb_act:
        blind_actions.append(sb_act)
    if bb_act:
        blind_actions.append(bb_act)
    if straddle_player:
        blind_actions.append({
            "player": straddle_player,
            "action": "posts_straddle",
            "amount": straddle_amount,
        })

    # ── Preflop-only players (for integrated fold inference) ──────────────────
    # Build before the street loop so inferred folds can participate in the
    # round-aware sort rather than being appended after it.
    _pf_card_keys: set[str] = set()
    _pf_display: dict[str, str] = {}
    for _, f in street_frames["preflop"]:
        for p in (f.get("players") or []):
            n  = (p.get("name") or "").strip()
            hc = p.get("hole_card_count") or len(p.get("hole_cards") or [])
            k  = n.casefold()
            if n and k not in placeholder_names and hc > 0:
                _pf_card_keys.add(k)
                if k not in _pf_display:
                    _pf_display[k] = n
    _postflop_seen: set[str] = set()
    for st in ("flop", "turn", "river"):
        for _, f in street_frames[st]:
            for p in (f.get("players") or []):
                n = (p.get("name") or "").strip()
                if n and n.casefold() not in placeholder_names:
                    _postflop_seen.add(n.casefold())
    _preflop_only: set[str] = _pf_card_keys - _postflop_seen

    # ── Voluntary action collection per street ────────────────────────────────
    allin_players: set[str] = set()                     # went all-in on any prior street
    recorded_global: set[tuple[str, str, int | None]] = set()  # cross-street dedup
    result: dict[str, list[dict]] = {"preflop": [], "flop": [], "turn": [], "river": []}
    _prev_street_stacks: dict[str, int] = {}  # last stack reading per player from previous street

    for street_name, sf in street_frames.items():
        # Per-player deduplication dict:
        #   key → dedup_key → (first_fi, last_fi, amount, display_name, action_type)
        # "first_fi" = when the action first appeared (used for temporal ordering).
        # "last_fi"  = most recent frame it appeared (most accurate OCR read wins).
        # Calls bucket by amount (// 500) so two different-sized calls can coexist
        # on the same street (e.g., limp-call + call of a 3-bet).
        per_player: dict[str, dict[str, tuple[int, int, int | None, str, str]]] = {}

        for frame_idx, f in sf:
            for p in (f.get("players") or []):
                name = (p.get("name") or "").strip()
                if not name or name.casefold() in placeholder_names:
                    continue
                action_type, amount = _parse_action_text(p.get("action_text"))
                if not action_type:
                    continue
                key = name.casefold()

                # All-in persistence: ignore further actions from all-in players
                if action_type == "all_in" and key in allin_players:
                    continue

                # Fallback: use current_bet if no dollar amount in the overlay text
                if amount is None:
                    cb = p.get("current_bet")
                    if cb and cb > 0:
                        amount = cb

                if key not in per_player:
                    per_player[key] = {}

                dedup_key = (
                    f"calls_{(amount or 0) // 500}"
                    if action_type == "calls"
                    else action_type
                )

                existing = per_player[key].get(dedup_key)
                if existing is None:
                    per_player[key][dedup_key] = (frame_idx, frame_idx, amount, name, action_type)
                elif frame_idx > existing[1]:
                    # Keep first_fi; update last_fi and amount with the later read
                    per_player[key][dedup_key] = (existing[0], frame_idx, amount, name, action_type)

        # Per-player stack timeline for postflop call validation.
        # Calls are accepted when within-street OR cross-street stack decrease ≥ 25%
        # of the stated amount.  Cross-street handles calls at the very start of a
        # street (before the first captured frame on that street shows the decrease).
        # Preflop is excluded: blind posts already cause stack drops independently.
        _street_stacks: dict[str, list[int]] = {}
        for _, f in sf:
            for p in (f.get("players") or []):
                k   = (p.get("name") or "").strip().casefold()
                stk = p.get("stack")
                if k and isinstance(stk, (int, float)) and stk > 0:
                    _street_stacks.setdefault(k, []).append(int(stk))

        # Flatten, applying cross-street dedup and all-in tracking
        raw_actions: list[dict] = []
        for player_key, actions_dict in per_player.items():
            if "all_in" in actions_dict:
                allin_players.add(player_key)
            for _dk, (first_fi, _last_fi, amount, display_name, action_type) in actions_dict.items():
                gkey = (player_key, action_type, amount)
                if gkey in recorded_global:
                    continue  # overlay bleed-through from a prior street

                # Postflop call validation: reject if neither within-street nor
                # cross-street stack decrease is ≥ 25% of the stated amount,
                # AND no current_bet corroboration.
                # HCL overlays for callers show the PRE-call stack with current_bet
                # equal to the call amount — use that as a secondary acceptance signal.
                if action_type == "calls" and street_name != "preflop":
                    readings = _street_stacks.get(player_key, [])
                    if len(readings) >= 2:
                        within_decrease = readings[0] - readings[-1]
                        prev_last       = _prev_street_stacks.get(player_key)
                        cross_decrease  = (prev_last - readings[0]) if (prev_last and readings) else 0
                        actual_decrease = max(within_decrease, cross_decrease)
                        min_needed      = max(1, (amount or 0) * 0.25)
                        if actual_decrease < min_needed:
                            # Secondary: accept if current_bet matches call amount AND the
                            # call overlay persisted across ≥ 2 frames (guards against a
                            # fleeting "$X TO CALL" prompt that OCR briefly reads as "CALL").
                            call_in_multiple_frames = first_fi != _last_fi
                            cb_confirms = call_in_multiple_frames and any(
                                abs((p.get("current_bet") or 0) - (amount or 0))
                                    <= max(50, (amount or 0) * 0.10)
                                for _, f in sf
                                for p in (f.get("players") or [])
                                if (p.get("name") or "").strip().casefold() == player_key
                                    and (p.get("current_bet") or 0) > 0
                            )
                            if not cb_confirms:
                                continue  # no corroborating signal → spurious call, skip

                raw_actions.append({
                    "player": display_name,
                    "action": action_type,
                    "amount": amount,
                    "_frame_idx": first_fi,
                })

        # Integrated preflop fold inference — inject BEFORE the sort so inferred
        # folds land in position order relative to the last aggressive action.
        # Use per-player last-seen frame so Dylan (last seen at frame 12 when
        # Greedo raised) gets fold_fi=13 and sorts into round 1, not round 2.
        if street_name == "preflop":
            agg_fis = [
                a["_frame_idx"] for a in raw_actions
                if a["action"] in ("raises", "all_in", "bets")
            ]
            default_fold_fi = max(agg_fis, default=0) + 1
            pf_last_seen: dict[str, int] = {}
            for fi, f in street_frames["preflop"]:
                for p in (f.get("players") or []):
                    k = (p.get("name") or "").strip().casefold()
                    if k and k not in placeholder_names:
                        pf_last_seen[k] = max(pf_last_seen.get(k, 0), fi)
            have_fold  = {a["player"].casefold() for a in raw_actions if a["action"] == "folds"}
            have_call  = {a["player"].casefold() for a in raw_actions if a["action"] == "calls"}
            for pkey in sorted(_preflop_only):
                if pkey not in have_fold:
                    if pkey in have_call:
                        # Called in an earlier round → fold must be in a later round.
                        # Use default_fold_fi (after the last aggressive action) so the
                        # sort places this fold in round 2+, not round 1.
                        player_fold_fi = default_fold_fi
                    else:
                        # Never called → use per-player last-seen frame for tighter timing.
                        player_fold_fi = pf_last_seen.get(pkey, default_fold_fi - 1) + 1
                    raw_actions.append({
                        "player": _pf_display.get(pkey, pkey),
                        "action": "folds",
                        "amount": None,
                        "_frame_idx": player_fold_fi,
                    })

        # Supersession: remove a call when the same player raised/all-in AFTER
        # that call on the same street.  Early OCR reads the call overlay before
        # the raise graphic has resolved.  "bets" are excluded — a player can
        # legitimately bet, face a raise, then call that raise as two actions.
        # Also remove a call when the same player already bet the same amount
        # BEFORE the call on the same street (stale CALL overlay after a BET).
        raise_frames_map: dict[str, list[int]] = {}
        bet_frames_map:   dict[str, list[tuple[int, int | None]]] = {}
        for a in raw_actions:
            if a["action"] in ("raises", "all_in"):
                k = a["player"].casefold()
                raise_frames_map.setdefault(k, []).append(a["_frame_idx"])
            elif a["action"] == "bets":
                k = a["player"].casefold()
                bet_frames_map.setdefault(k, []).append((a["_frame_idx"], a.get("amount")))
        raw_actions = [
            a for a in raw_actions
            if not (
                a["action"] == "calls"
                and (
                    # raise/all-in from same player appeared AFTER this call
                    (
                        a["player"].casefold() in raise_frames_map
                        and any(
                            rf > a["_frame_idx"]
                            for rf in raise_frames_map[a["player"].casefold()]
                        )
                    )
                    # bet from same player for same amount appeared BEFORE this call
                    or (
                        a["player"].casefold() in bet_frames_map
                        and any(
                            bf < a["_frame_idx"] and ba == a.get("amount")
                            for bf, ba in bet_frames_map[a["player"].casefold()]
                        )
                    )
                )
            )
        ]

        # Sort by position order (round-aware)
        if street_name == "preflop":
            raw_actions = _sort_actions_by_round(
                raw_actions, preflop_position_map, preflop_order
            )
        else:
            raw_actions = _sort_actions_by_round(
                raw_actions, position_map, _POSTFLOP_POS_ORDER
            )

        result[street_name] = [
            {k: v for k, v in a.items() if k != "_frame_idx"} for a in raw_actions
        ]
        for a in raw_actions:
            recorded_global.add((a["player"].casefold(), a["action"], a.get("amount")))

        # Update cross-street stack reference for next street's call validation
        for pkey, stk_readings in _street_stacks.items():
            if stk_readings:
                _prev_street_stacks[pkey] = stk_readings[-1]

    result["preflop"] = blind_actions + result["preflop"]

    # ── Post-collection fold inference ────────────────────────────────────────
    # Preflop: had hole cards but never appeared postflop → folded preflop
    preflop_players: set[str] = set()
    for _, f in street_frames["preflop"]:
        for p in (f.get("players") or []):
            name = (p.get("name") or "").strip()
            hc   = p.get("hole_card_count") or len(p.get("hole_cards") or [])
            if name and name.casefold() not in placeholder_names and hc > 0:
                preflop_players.add(name.casefold())

    postflop_players: set[str] = set()
    for st in ("flop", "turn", "river"):
        for _, f in street_frames[st]:
            for p in (f.get("players") or []):
                name = (p.get("name") or "").strip()
                if name and name.casefold() not in placeholder_names:
                    postflop_players.add(name.casefold())

    folded: set[str] = set()
    already_folded = {
        a["player"].casefold() for a in result["preflop"] if a["action"] == "folds"
    }
    for pname_key in sorted(preflop_players - postflop_players):
        folded.add(pname_key)
        if pname_key not in already_folded:
            display = next(
                (
                    (p.get("name") or "").strip()
                    for _, f in street_frames["preflop"]
                    for p in (f.get("players") or [])
                    if (p.get("name") or "").strip().casefold() == pname_key
                ),
                pname_key,
            )
            result["preflop"].append({"player": display, "action": "folds", "amount": None})

    # ── Street-disappearance fold inference (flop → turn, turn → river) ─────────
    # Rule: if a player appeared on street S but not on street S+1 or later,
    # their fold is placed on S if they had NO voluntary action (bet/call/raise/
    # all-in) on S, or on S+1 if they DID act (they survived the street, so the
    # fold must be on the next street).
    # "Checks" are NOT counted as voluntary actions because the HCL overlay never
    # shows a CHECK text — absence of action_text means the player just checked.
    _VOLUNTARY = {"bets", "calls", "raises", "all_in"}

    def _infer_disappearance_folds(
        src_street: str,
        next_streets: tuple[str, ...],
    ) -> None:
        src_frames = street_frames[src_street]
        if not src_frames:
            return
        later_frames = [f for st in next_streets for _, f in street_frames[st]]
        if not later_frames:
            return

        src_names: set[str] = set()
        src_display: dict[str, str] = {}
        for _, f in src_frames:
            for p in (f.get("players") or []):
                name = (p.get("name") or "").strip()
                if name and name.casefold() not in placeholder_names:
                    k = name.casefold()
                    src_names.add(k)
                    src_display.setdefault(k, name)

        later_names: set[str] = set()
        for f in later_frames:
            for p in (f.get("players") or []):
                name = (p.get("name") or "").strip()
                if name and name.casefold() not in placeholder_names:
                    later_names.add(name.casefold())

        already_have_fold = {
            a["player"].casefold()
            for st_acts in result.values()
            for a in st_acts
            if a["action"] == "folds"
        }

        # Players who had a voluntary action on src_street (bet/call/raise/all-in)
        acted_on_src: set[str] = {
            a["player"].casefold()
            for a in result[src_street]
            if a["action"] in _VOLUNTARY
        }

        for pname_key in sorted(src_names - later_names - folded - allin_players - already_have_fold):
            display = src_display.get(pname_key, pname_key)
            # Assign fold to src_street if player only checked (no voluntary action);
            # assign to the first next_street if they did act on src_street.
            target_street = next_streets[0] if pname_key in acted_on_src else src_street
            result[target_street].append({"player": display, "action": "folds", "amount": None})
            folded.add(pname_key)

    _infer_disappearance_folds("flop", ("turn", "river"))
    _infer_disappearance_folds("turn", ("river",))

    # ── Transcript-assisted action supplementation ────────────────────────────
    if transcript_segments and player_names:
        def _street_start_sec(min_board_len: int) -> float | None:
            for f in conf_frames:
                ts = f.get("timestamp") or ""
                if ts and len(f.get("board") or []) >= min_board_len:
                    return _ts_to_sec(ts)
            return None

        _flop_sec  = _street_start_sec(3)
        _turn_sec  = _street_start_sec(4)
        _river_sec = _street_start_sec(5)

        def _assign_street(seg_sec: float) -> str:
            if _river_sec and seg_sec >= _river_sec:
                return "river"
            if _turn_sec and seg_sec >= _turn_sec:
                return "turn"
            if _flop_sec and seg_sec >= _flop_sec:
                return "flop"
            return "preflop"

        hand_start = _ts_to_sec(conf_frames[0].get("timestamp") or "") if conf_frames else 0.0
        hand_end   = _ts_to_sec(conf_frames[-1].get("timestamp") or "") if conf_frames else 0.0
        t_actions  = extract_actions_from_segments(
            transcript_segments, player_names, hand_start, hand_end
        )
        for ta in t_actions:
            t_street  = _assign_street(ta["seg_sec"])
            pkey      = ta["player"].casefold()
            t_action  = ta["action"]
            street_acts = result[t_street]
            already_has = any(
                a["player"].casefold() == pkey and a["action"] == t_action
                for a in street_acts
            )
            already_folded_anywhere = any(
                a["player"].casefold() == pkey and a["action"] == "folds"
                for st_acts in result.values()
                for a in st_acts
            )
            if not already_has and not already_folded_anywhere:
                display = next(
                    (a["player"] for a in street_acts if a["player"].casefold() == pkey),
                    ta["player"],
                )
                result[t_street].append({"player": display, "action": t_action, "amount": None})
                if t_action == "folds":
                    folded.add(pkey)

    # Collect remaining explicit folds
    for street_acts in result.values():
        for act in street_acts:
            if act["action"] == "folds":
                folded.add(act["player"].casefold())

    ante_spread_info = (
        {"ante_per_player": ante_per_player, "bb_ante": bb_ante} if bb_ante else None
    )
    return result, folded, ante_spread_info


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ts_to_sec(ts: str) -> float:
    """Convert 'HH:MM:SS.mmm' or 'HH:MM:SS' to fractional seconds."""
    try:
        parts = ts.split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except Exception:
        return 0.0
