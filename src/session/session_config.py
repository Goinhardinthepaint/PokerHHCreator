"""
src/session/session_config.py

Session-level configuration that persists across all hands in a stream.
Tracks stakes, ante, seated players, and the button rotation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _PlayerEntry:
    seat: int
    name: str


class SessionConfig:
    """
    Manages stakes, bb_ante, player roster, and button rotation across hands.

    Parameters
    ----------
    stakes            : "50/100" style string
    bb_ante           : big-blind ante amount (whole session)
    players           : list of dicts with keys "seat" (int) and "name" (str)
    start_button_seat : seat number that holds the button in hand 0
    start_hand_number : absolute hand number for the first hand (default 1)
    """

    def __init__(
        self,
        stakes: str,
        bb_ante: int,
        players: list[dict],
        start_button_seat: int,
        start_hand_number: int = 1,
    ) -> None:
        self.stakes = stakes
        self.bb_ante = bb_ante
        self._players: list[_PlayerEntry] = [
            _PlayerEntry(int(p["seat"]), str(p["name"])) for p in players
        ]
        self._start_button_seat = start_button_seat
        self.hand_number = start_hand_number
        self._hand_offset: int = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seats(self) -> list[int]:
        return sorted(p.seat for p in self._players)

    def _next_clockwise(self, seat: int) -> int:
        seats = self._seats()
        idx = seats.index(seat)
        return seats[(idx + 1) % len(seats)]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_button_seat(self, n: int) -> int:
        """Return the button seat for the n-th hand (0-based offset from session start)."""
        seats = self._seats()
        start_idx = seats.index(self._start_button_seat)
        return seats[(start_idx + n) % len(seats)]

    def get_sb_seat(self, n: int) -> int:
        """Return the SB seat for the n-th hand."""
        return self._next_clockwise(self.get_button_seat(n))

    def get_bb_seat(self, n: int) -> int:
        """Return the BB seat for the n-th hand."""
        return self._next_clockwise(self.get_sb_seat(n))

    def get_player_name(self, seat: int) -> str:
        """Return the player name seated at the given seat number."""
        for p in self._players:
            if p.seat == seat:
                return p.name
        raise KeyError(f"No player at seat {seat}")

    def current_offset(self) -> int:
        """Zero-based index of the hand currently being played."""
        return self._hand_offset

    def advance(self) -> None:
        """Advance to the next hand, incrementing both offset and hand_number."""
        self._hand_offset += 1
        self.hand_number += 1

    @classmethod
    def from_json(cls, path: str) -> "SessionConfig":
        """Load a SessionConfig from a JSON file."""
        import json
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        stakes_raw = data["stakes"]
        if isinstance(stakes_raw, list):
            stakes = f"{stakes_raw[0]}/{stakes_raw[1]}"
        else:
            stakes = str(stakes_raw)
        return cls(
            stakes=stakes,
            bb_ante=int(data["bb_ante"]),
            players=data["players"],
            start_button_seat=int(data["starting_button_seat"]),
        )
