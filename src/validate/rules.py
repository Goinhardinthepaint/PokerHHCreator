"""
src/validate/rules.py

Pre-export hand validation rules.

Usage in the pipeline:
    from src.validate.rules import validate_hand, RuleViolation
    violations = validate_hand(pt4_dict)
    if violations:
        log and skip hand
"""

from __future__ import annotations

import re
from typing import NamedTuple

# ── Constants ─────────────────────────────────────────────────────────────────

_VALID_POSITIONS = frozenset({
    "BTN", "SB", "BB",
    "UTG", "UTG+1", "UTG+2",
    "HJ", "MP", "CO",
    "STR", "STRADDLE", "EP",
    "",
})

_CARD_RE     = re.compile(r"^[2-9TJQKA][cdhs]$")
_STREET_KEYS = ("preflop", "flop", "turn", "river")
_BOARD_KEYS  = ("flop", "turn", "river")


# ── Rule violation type ────────────────────────────────────────────────────────

class RuleViolation(NamedTuple):
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.rule}: {self.detail}"


# ── Validator ─────────────────────────────────────────────────────────────────

def validate_hand(hand: dict) -> list[RuleViolation]:
    """
    Validate a hand dict (output of sm.to_pt4_dict() / feed_hand()).

    Returns a list of RuleViolation.  An empty list means the hand is clean.
    Any violation should cause the hand to be skipped before export.
    """
    v: list[RuleViolation] = []

    players  = hand.get("players") or []
    action   = hand.get("action") or {}
    board    = hand.get("board") or {}
    showdown = hand.get("showdown") or []

    player_names: set[str] = {p["name"] for p in players}

    # ------------------------------------------------------------------
    # Position rules
    # ------------------------------------------------------------------

    if len(players) < 2:
        v.append(RuleViolation("POS-1", f"fewer than 2 seated players ({len(players)})"))

    positions = [p.get("position", "") for p in players]

    btn_count = positions.count("BTN")
    if btn_count != 1:
        v.append(RuleViolation("POS-2", f"expected 1 BTN, found {btn_count}"))

    sb_count = positions.count("SB")
    if sb_count != 1:
        v.append(RuleViolation("POS-3", f"expected 1 SB, found {sb_count}"))

    bb_count = positions.count("BB")
    if bb_count != 1:
        v.append(RuleViolation("POS-4", f"expected 1 BB, found {bb_count}"))

    for p in players:
        pos = p.get("position", "")
        if pos not in _VALID_POSITIONS:
            v.append(RuleViolation("POS-5", f"unknown position {pos!r} for {p['name']!r}"))

    # ------------------------------------------------------------------
    # Action rules
    # ------------------------------------------------------------------

    preflop = action.get("preflop") or []

    if not any(a.get("action") == "posts_sb" for a in preflop):
        v.append(RuleViolation("ACT-1", "no posts_sb in preflop"))

    if not any(a.get("action") == "posts_bb" for a in preflop):
        v.append(RuleViolation("ACT-2", "no posts_bb in preflop"))

    all_acts: list[dict] = []
    for st in _STREET_KEYS:
        all_acts.extend(action.get(st) or [])

    for a in all_acts:
        pname = a.get("player", "")
        if pname not in player_names:
            v.append(RuleViolation("ACT-3", f"unknown player {pname!r} in action"))
        amt = a.get("amount", 0) or 0
        if not isinstance(amt, (int, float)) or amt < 0:
            v.append(RuleViolation("ACT-4",
                f"negative amount {amt} for {pname!r} {a.get('action')!r}"))

    # ------------------------------------------------------------------
    # Pot rules
    # ------------------------------------------------------------------

    total_invested = sum((a.get("amount") or 0) for a in all_acts)
    if total_invested <= 0:
        v.append(RuleViolation("POT-1", f"total invested is {total_invested} (expected > 0)"))

    # ------------------------------------------------------------------
    # Card rules
    # ------------------------------------------------------------------

    def _valid(c: str) -> bool:
        return bool(_CARD_RE.match(c))

    all_cards: list[str] = []

    flop = board.get("flop") or None     # normalize [] to None
    if flop is not None:
        if len(flop) != 3:
            v.append(RuleViolation("CARD-1", f"flop must have 3 cards, got {len(flop)}"))
        for c in flop:
            if not _valid(c):
                v.append(RuleViolation("CARD-2", f"invalid flop card {c!r}"))
        all_cards.extend(flop)

    turn = board.get("turn")
    if turn is not None:
        if not isinstance(turn, str) or not _valid(turn):
            v.append(RuleViolation("CARD-3", f"invalid turn card {turn!r}"))
        else:
            all_cards.append(turn)

    river = board.get("river")
    if river is not None:
        if not isinstance(river, str) or not _valid(river):
            v.append(RuleViolation("CARD-4", f"invalid river card {river!r}"))
        else:
            all_cards.append(river)

    for p in players:
        hc = p.get("hole_cards") or []
        for c in hc:
            if not _valid(c):
                v.append(RuleViolation("CARD-5",
                    f"invalid hole card {c!r} for {p['name']!r}"))
        all_cards.extend(hc)

    seen_cards: set[str] = set()
    for c in all_cards:
        if c in seen_cards:
            v.append(RuleViolation("CARD-6", f"duplicate card {c!r} in deck"))
            break
        seen_cards.add(c)

    # ------------------------------------------------------------------
    # Format rules
    # ------------------------------------------------------------------

    for p in players:
        if not (p.get("name") or "").strip():
            v.append(RuleViolation("FMT-1", f"empty player name at seat {p.get('seat')}"))

    seats = [p.get("seat") for p in players]
    if len(seats) != len(set(seats)):
        v.append(RuleViolation("FMT-2", f"duplicate seat numbers: {seats}"))

    for st in _STREET_KEYS:
        if st not in action:
            v.append(RuleViolation("FMT-3", f"missing street key {st!r} in action dict"))

    for k in _BOARD_KEYS:
        if k not in board:
            v.append(RuleViolation("FMT-4", f"missing key {k!r} in board dict"))

    for sd in showdown:
        if not (sd.get("player") or "").strip():
            v.append(RuleViolation("FMT-5", "showdown entry missing player name"))
        if sd.get("result") not in ("wins", "loses", ""):
            v.append(RuleViolation("FMT-6",
                f"showdown result {sd.get('result')!r} not in ('wins','loses','')"))

    return v
