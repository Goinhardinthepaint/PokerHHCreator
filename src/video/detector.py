"""
Hand and side-game boundary detector.

Identifies new-hand boundaries in transcript text and flags side games
(bomb pots, PLO, etc.) so the pipeline can route them separately.
"""

import re
from dataclasses import dataclass

# ── Patterns that signal a side game ──────────────────────────────────────────
SIDE_GAME_PATTERNS = [
    r"bomb\s*pot",
    r"plo",
    r"pot\s*limit\s*omaha",
    r"four\s*cards",
    r"double\s*board",
]

# ── Patterns that signal a new Hold'em hand ────────────────────────────────────
_HAND_BOUNDARY_PATTERNS = [
    r"new hand",
    r"brand new hand",
    r"button moves",
    r"here we go.*button",
    r"another hand",
    r"next hand",
    r"hand number",
]

_side_game_re = re.compile("|".join(SIDE_GAME_PATTERNS), re.IGNORECASE)
_boundary_re = re.compile("|".join(_HAND_BOUNDARY_PATTERNS), re.IGNORECASE)


@dataclass
class BoundaryResult:
    is_boundary: bool
    is_side_game: bool
    side_game_type: str | None  # e.g. "plo_bomb_pot", "plo", None


def _classify_side_game(text: str) -> str:
    """Return a canonical side-game type string for matched text."""
    lower = text.lower()
    if re.search(r"bomb\s*pot", lower) and re.search(r"plo|omaha|four\s*cards", lower):
        return "plo_bomb_pot"
    if re.search(r"bomb\s*pot", lower):
        return "bomb_pot"
    if re.search(r"plo|pot\s*limit\s*omaha|four\s*cards", lower):
        return "plo"
    if re.search(r"double\s*board", lower):
        return "double_board"
    return "side_game"


def is_new_hand(text: str) -> bool:
    """Return True if text signals any new hand boundary.

    Includes side-game announcements (bomb pots, PLO) because they still
    represent a hand boundary the chunker must split on.  Callers that want
    to skip side games should follow up with is_side_game().
    """
    if _boundary_re.search(text):
        return True
    # A side-game announcement without an explicit "new hand" phrase is still
    # a boundary (e.g. "alright bomb pot everyone posts").
    if _side_game_re.search(text):
        return True
    return False


def is_side_game(text: str) -> bool:
    """Return True if text contains a side-game indicator."""
    return bool(_side_game_re.search(text))


def classify_boundary(text: str) -> BoundaryResult:
    """Full boundary classification — boundary type and side-game label."""
    boundary = is_new_hand(text)
    side = is_side_game(text)
    return BoundaryResult(
        is_boundary=boundary,
        is_side_game=side,
        side_game_type=_classify_side_game(text) if side else None,
    )
