"""
src/ocr/frame_accumulator.py

Processes a chronological sequence of video frames (1 fps), accumulates
OCR overlay observations, detects hand boundaries, reconstructs poker hands,
and emits formatted PT4-compatible hand history strings.

Pipeline
--------
frame paths
  → overlay_reader.read_frame()    (one OCR call per changed frame)
  → FrameAccumulator._observe()   (diff against prev frame → action events)
  → HandBuilder                   (per-hand observation log)
  → _close_hand()
      → inject transcript actions for hand's time window
      → HandBuilder.compile()     (build hand JSON from events)
      → _apply_resolver()         (infer missing calls per street)
      → feed_hand()               (state machine + validation)
      → format_hand()             (PT4 text output)
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2

log = logging.getLogger(__name__)

# Preflop position order (lower = acts earlier)
_PREFLOP_ORDER: dict[str, int] = {
    "UTG": 0, "UTG+1": 1, "UTG+2": 2,
    "EP":  0,
    "MP":  3, "HJ": 4, "CO": 5,
    "BTN": 6,
    "SB":  7, "BB": 8,
    "STR": 9, "STRADDLE": 9,
    "":    99,
}


# ── Data containers ────────────────────────────────────────────────────────────

@dataclass
class ActionEvent:
    """An action inferred from a state change between two consecutive frames."""
    frame_num: int
    player: str
    action: str    # calls / raises / bets / checks / folds / all_in
    amount: int
    street: str    # preflop / flop / turn / river
    inferred_fold: bool = False
    source: str = "ocr"   # "ocr" | "transcript" | "resolver"


@dataclass
class PlayerRecord:
    """Persistent per-player record within one hand."""
    name: str
    seat: int
    initial_stack: int
    position: str
    last_action: str = ""
    last_amount: int = 0
    first_frame: int = 0
    frames_seen: int = 0
    frames_absent: int = 0
    events: list[ActionEvent] = field(default_factory=list)
    from_roster: bool = False   # True when pre-seeded from session config


class HandBuilder:
    """
    Accumulates frame observations for one poker hand.
    Call observe_frame() for each frame that belongs to this hand,
    inject_transcript_actions() after all frames,
    then compile() to get a hand JSON suitable for feed_hand().
    """

    def __init__(
        self,
        stakes: str,
        bb_ante: int,
        roster_players: list[dict] | None = None,
    ) -> None:
        self.stakes = stakes
        self.bb_ante = bb_ante
        self._records: dict[str, PlayerRecord] = {}
        self._pot_readings: list[tuple[int, int]] = []
        self._board_events: list[tuple[int, int]] = []
        self._current_street: str = "preflop"
        self._last_board_cards: list[str] = []
        self._max_board_cards: list[str] = []
        self.start_frame: int = 0
        self.end_frame: int = 0

        # Per-street pot snapshot (set when street transitions)
        self._pot_at_street_start: dict[str, int] = {
            "preflop": 0, "flop": 0, "turn": 0, "river": 0
        }
        self._prev_pot: int = 0

        # Pre-seed roster players so all known seats are populated
        if roster_players:
            for p in roster_players:
                name = p["name"]
                key = name.casefold()
                self._records[key] = PlayerRecord(
                    name=name,
                    seat=p.get("seat", 0),
                    initial_stack=p.get("stack", 0),
                    position=p.get("position", ""),
                    first_frame=0,
                    frames_seen=0,
                    from_roster=True,
                )

    # ── Public API ─────────────────────────────────────────────────────────────

    def observe_frame(self, frame_num: int, ocr: dict) -> None:
        if not self.start_frame:
            self.start_frame = frame_num
        self.end_frame = frame_num

        pot         = ocr.get("pot", 0)
        board_cards = ocr.get("board_cards") or []
        players     = ocr.get("players") or []

        self._pot_readings.append((frame_num, pot))

        # Street transitions driven by board card count changes
        board_count = len(board_cards)
        prev_count  = len(self._last_board_cards)
        if board_count != prev_count:
            self._board_events.append((frame_num, board_count))
            if board_count == 3 and prev_count < 3:
                self._pot_at_street_start["flop"] = pot
                self._current_street = "flop"
            elif board_count == 4 and prev_count == 3:
                self._pot_at_street_start["turn"] = pot
                self._current_street = "turn"
            elif board_count == 5 and prev_count >= 4:
                self._pot_at_street_start["river"] = pot
                self._current_street = "river"
            self._last_board_cards = board_cards

        if board_count > len(self._max_board_cards):
            self._max_board_cards = board_cards

        # Per-player observation — update stacks and emit action events
        seen_keys: set[str] = set()
        for p in players:
            name = p.get("name") or ""
            if not name:
                continue
            key    = name.casefold()
            action = p.get("action") or ""
            amount = p.get("amount") or 0
            stack  = p.get("stack") or 0
            pos    = p.get("position") or ""

            seen_keys.add(key)

            if key not in self._records:
                rec = PlayerRecord(
                    name=name,
                    seat=0,
                    initial_stack=stack,
                    position=pos,
                    first_frame=frame_num,
                )
                self._records[key] = rec
            else:
                rec = self._records[key]
                # Update stack from OCR — use first good non-zero reading
                if stack and not rec.initial_stack:
                    rec.initial_stack = stack

            rec.frames_absent = 0
            rec.frames_seen  += 1

            if pos and not rec.position:
                rec.position = pos

            action_changed = action != rec.last_action
            if action_changed and action and action != "folds":
                evt = ActionEvent(
                    frame_num=frame_num,
                    player=name,
                    action=action,
                    amount=amount,
                    street=self._current_street,
                )
                rec.events.append(evt)
                log.debug("Frame %d: %s %s $%d [%s]",
                          frame_num, name, action, amount, self._current_street)
            rec.last_action = action
            rec.last_amount = amount

        # NOTE: Absence-based fold inference is intentionally disabled.
        # Players are only marked as folded via explicit OCR "folds" text,
        # transcript mentions, or pot-math inference.  Overlay disappearance
        # alone is not reliable (camera angles, HUD glitches).
        for key, rec in self._records.items():
            if key not in seen_keys:
                rec.frames_absent += 1

        self._prev_pot = pot

    def inject_transcript_actions(self, transcript_actions: list[dict]) -> None:
        """
        Merge transcript-derived actions into this hand's event log.

        Each dict should have: player, action, seg_sec, [amount].
        Only injects actions for players already in the roster.
        Transcript folds ARE honoured (they are explicit evidence).
        """
        for ta in transcript_actions:
            player_raw = ta.get("player") or ""
            key = player_raw.casefold()
            action = ta.get("action") or ""
            if not action or not key:
                continue

            # Match to canonical roster name
            rec = self._records.get(key)
            if rec is None:
                # Try fuzzy match
                from difflib import get_close_matches
                keys = list(self._records.keys())
                close = get_close_matches(key, keys, n=1, cutoff=0.70)
                if close:
                    rec = self._records[close[0]]
            if rec is None:
                continue

            # Don't duplicate — skip if this action is already recorded nearby
            amount = ta.get("amount") or 0
            already = any(
                e.action == action and e.source == "transcript"
                for e in rec.events
            )
            if already:
                continue

            evt = ActionEvent(
                frame_num=self.end_frame,
                player=rec.name,
                action=action,
                amount=amount,
                street=self._current_street,
                source="transcript",
            )
            rec.events.append(evt)
            if action == "folds":
                rec.last_action = "folds"
            log.debug("Transcript: %s %s $%d", rec.name, action, amount)

    def compile(self) -> Optional[dict]:
        """
        Build a hand JSON dict from accumulated observations.
        Returns None if no meaningful data was seen.
        """
        # Include all players: roster-seeded ones + OCR-discovered ones
        # Only drop roster-seeded players that were never seen by OCR AND
        # have zero stack (pure noise with no evidence of participation).
        all_recs = {
            k: r for k, r in self._records.items()
            if r.frames_seen >= 1 or (r.from_roster and r.initial_stack > 0)
        }
        # Roster players with no OCR sighting and no events — keep them in
        # the players list but with no actions (state machine handles them).
        if not all_recs:
            return None

        # Assign seats: roster-seeded players keep their config seat;
        # OCR-discovered players get seats after roster players.
        roster_recs   = {k: r for k, r in all_recs.items() if r.from_roster and r.seat}
        ocr_only_recs = {k: r for k, r in all_recs.items()
                         if not (r.from_roster and r.seat)}

        used_seats = {r.seat for r in roster_recs.values()}
        next_seat = max(used_seats, default=0) + 1
        for rec in ocr_only_recs.values():
            if rec.seat == 0:
                while next_seat in used_seats:
                    next_seat += 1
                rec.seat = next_seat
                used_seats.add(next_seat)
                next_seat += 1

        sorted_recs = sorted(all_recs.values(), key=lambda r: r.seat)

        players: list[dict] = [
            {
                "seat":       rec.seat,
                "name":       rec.name,
                "position":   rec.position.upper() if rec.position else "",
                "stack":      rec.initial_stack,
                "hole_cards": None,
            }
            for rec in sorted_recs
        ]

        btn_seat = 1
        for p in players:
            if p["position"] == "BTN":
                btn_seat = p["seat"]
                break

        # Collect all action events, sorted by source then frame
        all_events: list[ActionEvent] = []
        for rec in all_recs.values():
            all_events.extend(rec.events)

        max_real_frame: dict[str, int] = {
            "preflop": 0, "flop": 0, "turn": 0, "river": 0
        }
        for e in all_events:
            if not e.inferred_fold:
                max_real_frame[e.street] = max(max_real_frame[e.street], e.frame_num)

        def _sort_key(e: ActionEvent) -> tuple:
            virtual_frame = (max_real_frame.get(e.street, 0) + 1) if e.inferred_fold else e.frame_num
            pos_order = _PREFLOP_ORDER.get(
                self._records[e.player.casefold()].position.upper(), 99
            )
            return (virtual_frame, pos_order, e.player)

        all_events.sort(key=_sort_key)

        players_with_real_preflop: set[str] = {
            e.player.casefold()
            for e in all_events
            if e.street == "preflop" and not e.inferred_fold
        }

        per_street: dict[str, list[dict]] = {
            "preflop": [], "flop": [], "turn": [], "river": []
        }
        for evt in all_events:
            if (evt.street == "preflop" and evt.inferred_fold
                    and evt.player.casefold() not in players_with_real_preflop):
                continue
            act: dict = {"player": evt.player, "action": evt.action}
            if evt.amount:
                act["amount"] = evt.amount
            per_street.get(evt.street, per_street["preflop"]).append(act)

        max_pot = max((p for _, p in self._pot_readings), default=0)
        bc = self._max_board_cards

        return {
            "players": players,
            "action":  per_street,
            "board":   {
                "flop":  bc[:3] if len(bc) >= 3 else None,
                "turn":  bc[3]  if len(bc) >= 4 else None,
                "river": bc[4]  if len(bc) >= 5 else None,
            },
            "showdown": [],
            "_meta": {
                "stakes":      self.stakes,
                "bb_ante":     self.bb_ante,
                "button_seat": btn_seat,
                "max_pot":     max_pot,
                "start_frame": self.start_frame,
                "end_frame":   self.end_frame,
                "pot_at_street_start": dict(self._pot_at_street_start),
            },
        }


# ── Main accumulator ──────────────────────────────────────────────────────────

class FrameAccumulator:
    """
    Processes a chronological list of frame image paths, detects hand
    boundaries, and produces formatted PT4 hand history strings.

    Parameters
    ----------
    stakes            : "50/100" style string (overridden by session config)
    bb_ante           : big-blind ante
    roster            : player name list for fuzzy OCR matching
    session           : SessionConfig (drives button rotation, player roster)
    transcript_segments : parsed VTT segments for transcript fusion
    frame_offset_sec  : video second corresponding to frame number 1 in the
                        frame directory (e.g. 2300 for p0Q7LW64ecM_2300-4100)
    """

    def __init__(
        self,
        stakes: str = "50/100",
        bb_ante: int = 100,
        roster: list[str] | None = None,
        hand_index_start: int = 1,
        stream_url: str = "ocr://frame_accumulator",
        session=None,
        transcript_segments=None,
        frame_offset_sec: int = 0,
    ) -> None:
        if session is not None:
            self.stakes   = session.stakes
            self.bb_ante  = session.bb_ante
        else:
            self.stakes  = stakes
            self.bb_ante = bb_ante

        self.roster              = roster
        self._stream_url         = stream_url
        self._hand_counter       = hand_index_start - 1
        self._session            = session
        self._transcript_segs    = transcript_segments or []
        self._frame_offset_sec   = frame_offset_sec

        # Track last-known stack per player across hands (casefold key → int)
        self._last_known_stacks: dict[str, int] = {}

        self._builder: Optional[HandBuilder]  = None
        self._prev_ocr: Optional[dict]        = None
        self._prev_frame_arr                  = None
        self._consecutive_dark: int           = 0
        self._pre_dark_nplayers: int          = 0
        self._skipped_frames: int             = 0

        self.completed_hands: list[dict] = []
        self.hand_histories:  list[str]  = []
        self.errors:          list[str]  = []

    # ── Public API ─────────────────────────────────────────────────────────────

    def process(self, frame_paths: list[str]) -> list[str]:
        from src.ocr.overlay_reader import read_frame, _get_reader
        _get_reader()

        _DIFF_SKIP = 3.0

        for path in frame_paths:
            frame_num = _parse_frame_num(path)
            log.info("Frame %d", frame_num)

            curr_arr = cv2.imread(str(path))

            if (curr_arr is not None
                    and self._prev_frame_arr is not None
                    and self._prev_ocr is not None
                    and curr_arr.shape == self._prev_frame_arr.shape):
                fh, fw = curr_arr.shape[:2]
                y0     = int(fh * 0.55)
                pl_c   = curr_arr[y0:, :int(fw * 0.455)]
                pl_p   = self._prev_frame_arr[y0:, :int(fw * 0.455)]
                pot_c  = curr_arr[y0:, int(fw * 0.70):]
                pot_p  = self._prev_frame_arr[y0:, int(fw * 0.70):]
                diff   = (cv2.absdiff(pl_c, pl_p).mean()
                          + cv2.absdiff(pot_c, pot_p).mean()) / 2.0
                if diff < _DIFF_SKIP:
                    self._prev_frame_arr = curr_arr
                    self._skipped_frames += 1
                    log.debug("Frame %d: OCR skipped (overlay diff=%.2f)", frame_num, diff)
                    self._observe(frame_num, self._prev_ocr)
                    continue

            self._prev_frame_arr = curr_arr

            try:
                ocr = read_frame(str(path), roster=self.roster)
            except Exception as exc:
                log.warning("OCR failed on frame %d (%s): %s", frame_num, path, exc)
                continue

            self._observe(frame_num, ocr)
            self._prev_ocr = ocr

        self._close_hand()
        if self._skipped_frames:
            log.info("Skipped OCR on %d frame(s) (no overlay change)", self._skipped_frames)
        return self.hand_histories

    # ── Internal ──────────────────────────────────────────────────────────────

    def _roster_players_for_hand(self) -> list[dict] | None:
        """Build the roster player list with last-known stacks for a new hand."""
        if self._session is None:
            return None
        result = []
        for p in self._session._players:
            stack = self._last_known_stacks.get(p.name.casefold(), 0)
            result.append({
                "name":     p.name,
                "seat":     p.seat,
                "stack":    stack,
                "position": "",
            })
        return result

    def _observe(self, frame_num: int, ocr: dict) -> None:
        if not ocr.get("players") and ocr.get("pot", 0) == 0:
            if self._consecutive_dark == 0:
                self._pre_dark_nplayers = len((self._prev_ocr or {}).get("players") or [])
            self._consecutive_dark += 1
        else:
            self._consecutive_dark = 0
            self._pre_dark_nplayers = 0

        if self._prev_ocr is not None and self._is_boundary(ocr, self._prev_ocr):
            log.info("Frame %d: hand boundary detected", frame_num)
            self._close_hand()
            self._builder = None
            self._consecutive_dark = 0
            self._pre_dark_nplayers = 0

        if self._builder is None:
            if ocr.get("pot", 0) > 0 or ocr.get("players"):
                roster_players = self._roster_players_for_hand()
                self._builder = HandBuilder(
                    stakes=self.stakes,
                    bb_ante=self.bb_ante,
                    roster_players=roster_players,
                )

        if self._builder is not None:
            self._builder.observe_frame(frame_num, ocr)

            # Update last-known stacks from every frame we see
            for p in ocr.get("players") or []:
                if p.get("name") and p.get("stack"):
                    self._last_known_stacks[p["name"].casefold()] = p["stack"]

    def _is_boundary(self, curr: dict, prev: dict) -> bool:
        prev_pot      = prev.get("pot", 0)
        curr_pot      = curr.get("pot", 0)
        prev_board    = prev.get("board_count", 0)
        curr_board    = curr.get("board_count", 0)
        prev_nplayers = len(prev.get("players") or [])
        curr_nplayers = len(curr.get("players") or [])

        if self._consecutive_dark >= 2 and self._pre_dark_nplayers >= 2:
            return True

        if (prev_pot > 2000 and curr_pot < prev_pot * 0.30
                and curr_nplayers == 0 and self._consecutive_dark >= 2):
            return True

        if (prev_board > 0 and curr_board == 0
                and curr_pot < prev_pot * 0.70
                and curr_nplayers < prev_nplayers):
            return True

        return False

    def _close_hand(self) -> None:
        if self._builder is None:
            return

        # ── STEP 3: Inject transcript actions before compiling ──────────────
        if self._transcript_segs and self._builder.start_frame:
            start_sec = self._frame_offset_sec + self._builder.start_frame
            end_sec   = self._frame_offset_sec + self._builder.end_frame
            roster_names = (
                [p.name for p in self._session._players]
                if self._session else (self.roster or [])
            )
            from src.transcript.parser import extract_actions_from_segments
            transcript_acts = extract_actions_from_segments(
                self._transcript_segs,
                player_names=roster_names,
                start_sec=float(start_sec),
                end_sec=float(end_sec),
            )
            if transcript_acts:
                log.info("Injecting %d transcript action(s) for frames %d–%d",
                         len(transcript_acts), self._builder.start_frame, self._builder.end_frame)
                self._builder.inject_transcript_actions(transcript_acts)

        hand_json = self._builder.compile()
        self._builder = None

        if hand_json is None:
            return

        meta         = hand_json.pop("_meta", {})
        max_pot      = meta.get("max_pot", 0)
        stakes       = meta.get("stakes", self.stakes)
        bb_ante      = meta.get("bb_ante", self.bb_ante)
        start_frame  = meta.get("start_frame", 0)
        end_frame    = meta.get("end_frame", 0)
        pot_at_ss    = meta.get("pot_at_street_start", {})

        # ── STEP 1: Button seat from session config (with OCR fallback) ─────
        ocr_button_seat = meta.get("button_seat", 1)
        if self._session is not None:
            button_seat = self._session.get_button_seat(self._session.current_offset())
            log.info("Button seat from config: %d (OCR saw: %d)", button_seat, ocr_button_seat)
        else:
            button_seat = ocr_button_seat

        all_acts = [
            a
            for street in ("preflop", "flop", "turn", "river")
            for a in (hand_json.get("action") or {}).get(street) or []
        ]
        if len(hand_json.get("players") or []) < 2 or not all_acts:
            log.info("Skipping hand: too few players or no actions")
            return

        # ── STEP 2: Pot-delta resolver for all streets ──────────────────────
        hand_json = self._apply_resolver_all_streets(
            hand_json, max_pot, pot_at_ss, stakes, bb_ante
        )

        self._hand_counter += 1
        label = f"Hand {self._hand_counter} (frames {start_frame}–{end_frame})"

        from src.vision.hand_feeder import feed_hand, FeedError
        try:
            pt4 = feed_hand(
                hand_json,
                bb_ante=bb_ante,
                stakes=stakes,
                button_seat=button_seat,
            )
        except FeedError as exc:
            exc_str = str(exc)
            if "cannot determine winner" in exc_str:
                import re as _re
                names_match = _re.search(r"\[([^\]]+)\]", exc_str)
                if names_match:
                    raw = names_match.group(1)
                    active_names = [n.strip().strip("'") for n in raw.split(",")]
                else:
                    active_names = [p["name"] for p in (hand_json.get("players") or [])]
                hand_json["showdown"] = [
                    {"player": n, "hole_cards": [], "hand_description": "", "result": ""}
                    for n in active_names
                ]
                try:
                    pt4 = feed_hand(
                        hand_json, bb_ante=bb_ante, stakes=stakes, button_seat=button_seat,
                    )
                    log.info("%s: partial hand — %d player(s) active", label, len(active_names))
                except Exception as exc2:
                    msg = f"{label}: feed_hand error (retry): {exc2}"
                    self.errors.append(msg)
                    log.warning(msg)
                    return
            else:
                msg = f"{label}: feed_hand error: {exc}"
                self.errors.append(msg)
                log.warning(msg)
                return
        except Exception as exc:
            msg = f"{label}: feed_hand error: {exc}"
            self.errors.append(msg)
            log.warning(msg)
            return

        # Advance session counter after a successfully processed hand
        if self._session is not None:
            self._session.advance()

        from src.export.pt4_formatter import format_hand, HandValidationError
        try:
            hhtext = format_hand(
                pt4,
                stream_url=self._stream_url,
                hand_index=self._hand_counter,
            )
            self.completed_hands.append(pt4)
            self.hand_histories.append(hhtext)
            log.info("%s → %d chars (full PT4)", label, len(hhtext))
        except HandValidationError as exc:
            summary = self._partial_summary(pt4, label, str(exc))
            self.hand_histories.append(summary)
            log.info("%s → partial summary (%s)", label, exc)
        except Exception as exc:
            msg = f"{label}: format_hand error: {exc}"
            self.errors.append(msg)
            log.warning(msg)

    def _partial_summary(self, pt4: dict, label: str, reason: str) -> str:
        game   = pt4.get("game", {})
        stakes = game.get("stakes", "?")
        lines  = [
            f"# PARTIAL HAND: {label}",
            f"# Note: {reason}",
            f"# Stakes: {stakes}",
            "#",
        ]
        for p in pt4.get("players") or []:
            pos = f" [{p['position']}]" if p.get("position") else ""
            lines.append(f"# Seat {p['seat']}: {p['name']}{pos}  stack=${p.get('stack', 0):,}")

        action = pt4.get("action") or {}
        for street in ("preflop", "flop", "turn", "river"):
            acts = action.get(street) or []
            if acts:
                lines.append(f"# {street.upper()}:")
                for a in acts:
                    amt = f" ${a['amount']:,}" if a.get("amount") else ""
                    lines.append(f"#   {a['player']}: {a['action']}{amt}")

        return "\n".join(lines)

    # ── STEP 2: Per-street pot delta resolver ─────────────────────────────────

    def _apply_resolver_all_streets(
        self,
        hand_json: dict,
        max_pot: int,
        pot_at_street_start: dict,
        stakes: str,
        bb_ante: int,
    ) -> dict:
        """
        Run PotDeltaResolver for preflop (and postflop streets when pot data exists).
        Infers missing calls by comparing recorded action totals to observed pot growth.
        """
        from src.engine.pot_delta_resolver import PotDeltaResolver, Action as PdrAction

        player_names = {p["name"] for p in (hand_json.get("players") or [])}
        hand_json    = copy.deepcopy(hand_json)

        streets = [
            ("preflop", max_pot),   # use max_pot as upper bound for preflop
        ]
        # For postflop, use the pot snapshot at next-street start as the target
        for street, next_street in [("flop", "turn"), ("turn", "river")]:
            pot_end = pot_at_street_start.get(next_street, 0)
            if pot_end > 0:
                streets.append((street, pot_end))

        for street_name, expected_pot in streets:
            if expected_pot <= 0:
                continue

            street_acts = (hand_json.get("action") or {}).get(street_name) or []
            voluntary = [a for a in street_acts if a.get("action") not in (
                "posts_ante", "posts_sb", "posts_bb", "posts_straddle"
            )]
            if not voluntary:
                continue

            call_amount = max((a.get("amount") or 0 for a in voluntary), default=0)
            if call_amount <= 0:
                continue

            acted    = {a["player"] for a in voluntary}
            candidates = [n for n in player_names if n not in acted]
            if not candidates:
                continue

            confirmed = [
                PdrAction(a["player"], a.get("action", ""), a.get("amount") or 0)
                for a in voluntary
            ]
            resolved = PotDeltaResolver().resolve(
                actions=confirmed,
                expected_pot=expected_pot,
                call_amount=call_amount,
                candidates=candidates,
            )

            if len(resolved) == len(confirmed):
                continue

            inferred = [
                {"player": r.player, "action": r.action, "amount": r.amount}
                for r in resolved[len(confirmed):]
            ]
            log.info("Resolver [%s] inferred %d call(s): %s",
                     street_name, len(inferred),
                     [(a["player"], a["amount"]) for a in inferred])
            hand_json["action"][street_name] = street_acts + inferred

        return hand_json


# ── Convenience function ──────────────────────────────────────────────────────

def accumulate_frames(
    frame_paths: list[str],
    stakes: str = "50/100",
    bb_ante: int = 100,
    roster: list[str] | None = None,
    stream_url: str = "ocr://frame_accumulator",
    session=None,
    transcript_segments=None,
    frame_offset_sec: int = 0,
) -> list[str]:
    acc = FrameAccumulator(
        stakes=stakes,
        bb_ante=bb_ante,
        roster=roster,
        stream_url=stream_url,
        session=session,
        transcript_segments=transcript_segments,
        frame_offset_sec=frame_offset_sec,
    )
    return acc.process(frame_paths)


# ── Internal helper ───────────────────────────────────────────────────────────

def _parse_frame_num(path: str) -> int:
    m = re.search(r"frame_(\d+)", Path(path).name)
    return int(m.group(1)) if m else 0


# ── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import os
    import subprocess
    import sys
    from pathlib import Path

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)-8s %(message)s",
    )

    frames_dir = Path(
        sys.argv[1] if len(sys.argv) > 1
        else r"downloads/frames/p0Q7LW64ecM_2300-4100"
    )

    frame_paths = sorted(
        str(p)
        for p in frames_dir.glob("frame_*.jpg")
        if 70 <= _parse_frame_num(str(p)) <= 400
    )

    if not frame_paths:
        print(f"No frames found in {frames_dir} for range 70–400")
        sys.exit(1)

    print(f"Found {len(frame_paths)} frames in range 70–400")

    # ── Load session config ──────────────────────────────────────────────────
    from src.session.session_config import SessionConfig
    config_path = Path("src/session/session_config.json")
    session = None
    if config_path.exists():
        session = SessionConfig.from_json(str(config_path))
        print(f"Loaded session config: {len(session._players)} players, "
              f"stakes={session.stakes}, btn_seat={session.get_button_seat(0)}")
    else:
        print("No session config found — running without it")

    roster = (
        [p.name for p in session._players]
        if session else None
    )

    # ── Load transcript ──────────────────────────────────────────────────────
    from src.transcript.parser import parse_vtt
    transcript_segments = []
    # Parse the directory name to derive the video offset (e.g. _2300-4100 → 2300)
    dir_name = frames_dir.name   # e.g. "p0Q7LW64ecM_2300-4100"
    m_offset = re.search(r"_(\d+)-\d+$", dir_name)
    frame_offset_sec = int(m_offset.group(1)) if m_offset else 0

    vtt_candidates = list(Path("downloads").glob("*.vtt"))
    if vtt_candidates:
        vtt_path = vtt_candidates[0]
        try:
            transcript_segments = parse_vtt(str(vtt_path))
            print(f"Loaded {len(transcript_segments)} VTT segments from {vtt_path.name}")
            print(f"Frame offset: {frame_offset_sec}s (frames -> video seconds)")
        except Exception as e:
            print(f"VTT load failed: {e}")
    else:
        print("No .vtt file found — running without transcript fusion")

    # ── Run accumulator ──────────────────────────────────────────────────────
    stream_url = (
        session._players[0].name if session else frames_dir.name   # placeholder
    )
    acc = FrameAccumulator(
        roster=roster,
        stream_url=f"ocr://{frames_dir.name}",
        session=session,
        transcript_segments=transcript_segments,
        frame_offset_sec=frame_offset_sec,
    )

    histories = acc.process(frame_paths)

    print(f"\n{'='*60}")
    print(f"Reconstructed {len(histories)} hand(s) | errors: {len(acc.errors)}")
    print("=" * 60)

    if acc.errors:
        print("\nErrors:")
        for e in acc.errors:
            print(f"  {e}")

    for i, hh in enumerate(histories, 1):
        print(f"\n{'-'*60}")
        print(f"Hand {i}  ({len(hh)} chars)")
        print("-" * 60)
        print(hh[:3000])
        if len(hh) > 3000:
            print(f"  ... [{len(hh) - 3000} more chars]")

    # ── Run all tests ────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("Running existing test suite (174 assertions)…")
    print("=" * 60)

    test_scripts = [
        "tests/test_rules.py",
        "tests/test_e2e_baseline.py",
    ]
    all_passed = True
    for script in test_scripts:
        result = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True,
        )
        out_lines = (result.stdout + result.stderr).strip().splitlines()
        for line in out_lines[-6:]:
            print(line)
        if result.returncode != 0:
            all_passed = False

    print()
    print("Suite result:", "PASS" if all_passed else "FAIL")
    sys.exit(0 if all_passed else 1)
