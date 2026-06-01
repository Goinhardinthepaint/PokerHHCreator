"""
tests/test_session_config.py

Unit tests for SessionConfig.

Run:
    python tests/test_session_config.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.session.session_config import SessionConfig

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        msg = f"  FAIL  {label}" + (f": {detail}" if detail else "")
        print(msg)
        failures.append(msg)


# ---------------------------------------------------------------------------
# Fixture: 6 consecutive seats, button starts at seat 1
# ---------------------------------------------------------------------------

PLAYERS_6 = [
    {"seat": 1, "name": "Alice"},
    {"seat": 2, "name": "Bob"},
    {"seat": 3, "name": "Carol"},
    {"seat": 4, "name": "Dave"},
    {"seat": 5, "name": "Eve"},
    {"seat": 6, "name": "Frank"},
]


def test_rotation_6_players() -> None:
    print("\n-- 6-player button/SB/BB rotation (5 hands) --")
    cfg = SessionConfig(
        stakes="50/100",
        bb_ante=100,
        players=PLAYERS_6,
        start_button_seat=1,
        start_hand_number=1,
    )

    # hand 0: BTN=1, SB=2, BB=3
    check("rot/h0/btn", cfg.get_button_seat(0) == 1, f"got {cfg.get_button_seat(0)}")
    check("rot/h0/sb",  cfg.get_sb_seat(0) == 2,     f"got {cfg.get_sb_seat(0)}")
    check("rot/h0/bb",  cfg.get_bb_seat(0) == 3,     f"got {cfg.get_bb_seat(0)}")

    # hand 1: BTN=2, SB=3, BB=4
    check("rot/h1/btn", cfg.get_button_seat(1) == 2, f"got {cfg.get_button_seat(1)}")
    check("rot/h1/sb",  cfg.get_sb_seat(1) == 3,     f"got {cfg.get_sb_seat(1)}")
    check("rot/h1/bb",  cfg.get_bb_seat(1) == 4,     f"got {cfg.get_bb_seat(1)}")

    # hand 2: BTN=3, SB=4, BB=5
    check("rot/h2/btn", cfg.get_button_seat(2) == 3, f"got {cfg.get_button_seat(2)}")
    check("rot/h2/sb",  cfg.get_sb_seat(2) == 4,     f"got {cfg.get_sb_seat(2)}")
    check("rot/h2/bb",  cfg.get_bb_seat(2) == 5,     f"got {cfg.get_bb_seat(2)}")

    # hand 3: BTN=4, SB=5, BB=6
    check("rot/h3/btn", cfg.get_button_seat(3) == 4, f"got {cfg.get_button_seat(3)}")
    check("rot/h3/sb",  cfg.get_sb_seat(3) == 5,     f"got {cfg.get_sb_seat(3)}")
    check("rot/h3/bb",  cfg.get_bb_seat(3) == 6,     f"got {cfg.get_bb_seat(3)}")

    # hand 4: BTN=5, SB=6, BB=1  (wrap)
    check("rot/h4/btn", cfg.get_button_seat(4) == 5, f"got {cfg.get_button_seat(4)}")
    check("rot/h4/sb",  cfg.get_sb_seat(4) == 6,     f"got {cfg.get_sb_seat(4)}")
    check("rot/h4/bb",  cfg.get_bb_seat(4) == 1,     f"got {cfg.get_bb_seat(4)}")


def test_non_consecutive_seats() -> None:
    print("\n-- Non-consecutive seat numbers --")
    players = [
        {"seat": 1,  "name": "P1"},
        {"seat": 3,  "name": "P3"},
        {"seat": 5,  "name": "P5"},
        {"seat": 7,  "name": "P7"},
        {"seat": 9,  "name": "P9"},
        {"seat": 11, "name": "P11"},
    ]
    cfg = SessionConfig("50/100", 100, players, start_button_seat=1)

    # hand 0: BTN=1, SB=3, BB=5
    check("nc/h0/btn", cfg.get_button_seat(0) == 1,  f"got {cfg.get_button_seat(0)}")
    check("nc/h0/sb",  cfg.get_sb_seat(0) == 3,      f"got {cfg.get_sb_seat(0)}")
    check("nc/h0/bb",  cfg.get_bb_seat(0) == 5,      f"got {cfg.get_bb_seat(0)}")

    # hand 1: BTN=3, SB=5, BB=7
    check("nc/h1/btn", cfg.get_button_seat(1) == 3,  f"got {cfg.get_button_seat(1)}")
    check("nc/h1/sb",  cfg.get_sb_seat(1) == 5,      f"got {cfg.get_sb_seat(1)}")
    check("nc/h1/bb",  cfg.get_bb_seat(1) == 7,      f"got {cfg.get_bb_seat(1)}")

    # hand 5 (full rotation): BTN wraps back to 11, SB=1, BB=3
    check("nc/h5/btn", cfg.get_button_seat(5) == 11, f"got {cfg.get_button_seat(5)}")
    check("nc/h5/sb",  cfg.get_sb_seat(5) == 1,      f"got {cfg.get_sb_seat(5)}")
    check("nc/h5/bb",  cfg.get_bb_seat(5) == 3,      f"got {cfg.get_bb_seat(5)}")


def test_full_rotation_wrap() -> None:
    print("\n-- Full rotation wrap (3 players) --")
    players = [{"seat": i, "name": f"P{i}"} for i in [1, 2, 3]]
    cfg = SessionConfig("25/50", 50, players, start_button_seat=2)

    # After 3 hands the button is back where it started
    check("wrap/h3-back", cfg.get_button_seat(3) == cfg.get_button_seat(0),
          f"h0={cfg.get_button_seat(0)} h3={cfg.get_button_seat(3)}")
    check("wrap/h6-back", cfg.get_button_seat(6) == cfg.get_button_seat(0),
          f"h0={cfg.get_button_seat(0)} h6={cfg.get_button_seat(6)}")


def test_get_player_name() -> None:
    print("\n-- get_player_name --")
    cfg = SessionConfig("50/100", 100, PLAYERS_6, start_button_seat=1)

    check("name/seat1",  cfg.get_player_name(1) == "Alice",  cfg.get_player_name(1))
    check("name/seat6",  cfg.get_player_name(6) == "Frank",  cfg.get_player_name(6))

    raised = False
    try:
        cfg.get_player_name(99)
    except KeyError:
        raised = True
    check("name/missing", raised, "expected KeyError for seat 99")


def test_advance() -> None:
    print("\n-- advance() increments hand_number and offset --")
    cfg = SessionConfig("50/100", 100, PLAYERS_6, start_button_seat=1, start_hand_number=42)

    check("adv/initial-hand-num",   cfg.hand_number == 42,  str(cfg.hand_number))
    check("adv/initial-offset",     cfg.current_offset() == 0)
    check("adv/initial-btn",        cfg.get_button_seat(cfg.current_offset()) == 1)

    cfg.advance()
    check("adv/after1-hand-num",    cfg.hand_number == 43,  str(cfg.hand_number))
    check("adv/after1-offset",      cfg.current_offset() == 1)
    check("adv/after1-btn",         cfg.get_button_seat(cfg.current_offset()) == 2)

    cfg.advance()
    check("adv/after2-hand-num",    cfg.hand_number == 44,  str(cfg.hand_number))
    check("adv/after2-offset",      cfg.current_offset() == 2)
    check("adv/after2-btn",         cfg.get_button_seat(cfg.current_offset()) == 3)


def test_start_mid_rotation() -> None:
    print("\n-- Button starts mid-table (seat 4 of 6) --")
    cfg = SessionConfig("50/100", 100, PLAYERS_6, start_button_seat=4)

    # hand 0: BTN=4, SB=5, BB=6
    check("mid/h0/btn", cfg.get_button_seat(0) == 4, f"got {cfg.get_button_seat(0)}")
    check("mid/h0/sb",  cfg.get_sb_seat(0) == 5,     f"got {cfg.get_sb_seat(0)}")
    check("mid/h0/bb",  cfg.get_bb_seat(0) == 6,     f"got {cfg.get_bb_seat(0)}")

    # hand 1: BTN=5, SB=6, BB=1
    check("mid/h1/btn", cfg.get_button_seat(1) == 5, f"got {cfg.get_button_seat(1)}")
    check("mid/h1/sb",  cfg.get_sb_seat(1) == 6,     f"got {cfg.get_sb_seat(1)}")
    check("mid/h1/bb",  cfg.get_bb_seat(1) == 1,     f"got {cfg.get_bb_seat(1)}")

    # hand 2: BTN=6, SB=1, BB=2
    check("mid/h2/btn", cfg.get_button_seat(2) == 6, f"got {cfg.get_button_seat(2)}")
    check("mid/h2/sb",  cfg.get_sb_seat(2) == 1,     f"got {cfg.get_sb_seat(2)}")
    check("mid/h2/bb",  cfg.get_bb_seat(2) == 2,     f"got {cfg.get_bb_seat(2)}")


# ---------------------------------------------------------------------------

def main() -> None:
    print("=== test_session_config ===")

    test_rotation_6_players()
    test_non_consecutive_seats()
    test_full_rotation_wrap()
    test_get_player_name()
    test_advance()
    test_start_mid_rotation()

    print()
    if failures:
        print(f"RESULT: FAIL  ({len(failures)} assertion(s) failed)")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("RESULT: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
