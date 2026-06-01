"""
tests/test_rules.py

Verifies that validate_hand() passes on Hand 1 and Hand 2,
and correctly flags a known-bad hand.

Run:
    python tests/test_rules.py
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.vision.hand_feeder import feed_hand
from src.validate.rules import validate_hand, RuleViolation, _CARD_RE, _STREET_KEYS

# ── Test harness ──────────────────────────────────────────────────────────────

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        msg = f"  FAIL  {label}" + (f": {detail}" if detail else "")
        print(msg)
        failures.append(msg)


# ── Hand fixtures ─────────────────────────────────────────────────────────────

HAND1_JSON: dict = {
    "players": [
        {"seat": 1, "name": "Greedo",    "position": "BTN", "stack": 19000, "hole_cards": ["4s","4h"]},
        {"seat": 2, "name": "Francisco", "position": "SB",  "stack": 18950, "hole_cards": None},
        {"seat": 3, "name": "Dylan",     "position": "BB",  "stack": 18800, "hole_cards": None},
        {"seat": 4, "name": "Otto",      "position": "UTG", "stack": 24000, "hole_cards": ["Tc","8d"]},
        {"seat": 5, "name": "Alex",      "position": "HJ",  "stack": 22000, "hole_cards": ["Ah","5d"]},
        {"seat": 6, "name": "Airball",   "position": "STR", "stack": 48800, "hole_cards": None},
    ],
    "action": {
        "preflop": [
            {"player": "Greedo",    "action": "posts_ante",     "amount": 17},
            {"player": "Francisco", "action": "posts_ante",     "amount": 17},
            {"player": "Dylan",     "action": "posts_ante",     "amount": 17},
            {"player": "Otto",      "action": "posts_ante",     "amount": 17},
            {"player": "Alex",      "action": "posts_ante",     "amount": 17},
            {"player": "Airball",   "action": "posts_ante",     "amount": 17},
            {"player": "Francisco", "action": "posts_sb",       "amount": 50},
            {"player": "Dylan",     "action": "posts_bb",       "amount": 100},
            {"player": "Airball",   "action": "posts_straddle", "amount": 200},
            {"player": "Greedo",    "action": "raises",         "amount": 600},
            {"player": "Francisco", "action": "calls",          "amount": 550},
            {"player": "Dylan",     "action": "folds"},
            {"player": "Otto",      "action": "calls",          "amount": 600},
            {"player": "Alex",      "action": "raises",         "amount": 3000},
            {"player": "Airball",   "action": "folds"},
            {"player": "Greedo",    "action": "calls",          "amount": 2400},
            {"player": "Francisco", "action": "folds"},
            {"player": "Otto",      "action": "calls",          "amount": 2400},
        ],
        "flop": [
            {"player": "Otto",   "action": "checks"},
            {"player": "Alex",   "action": "bets",   "amount": 3000},
            {"player": "Otto",   "action": "raises", "amount": 21000},
            {"player": "Alex",   "action": "calls",  "amount": 18000},
            {"player": "Greedo", "action": "folds"},
        ],
        "turn":  [],
        "river": [],
    },
    "board": {"flop": ["Kd","9s","6s"], "turn": "Ks", "river": "2s"},
    "showdown": [
        {"player": "Otto", "hole_cards": ["Tc","8d"],
         "hand_description": "a pair of Kings", "result": "loses"},
        {"player": "Alex", "hole_cards": ["Ah","5d"],
         "hand_description": "a pair of Kings", "result": "wins"},
    ],
}

HAND2_JSON: dict = {
    "players": [
        {"seat": 1, "name": "Steve",     "position": "BTN", "stack": 15000, "hole_cards": ["As","Ks"]},
        {"seat": 2, "name": "Dylan",     "position": "SB",  "stack": 12000, "hole_cards": None},
        {"seat": 3, "name": "Airball",   "position": "BB",  "stack": 30000, "hole_cards": None},
        {"seat": 4, "name": "Alex",      "position": "UTG", "stack": 18000, "hole_cards": None},
        {"seat": 5, "name": "Francisco", "position": "CO",  "stack": 22000, "hole_cards": None},
    ],
    "action": {
        "preflop": [
            {"player": "Steve",     "action": "posts_ante",  "amount": 20},
            {"player": "Dylan",     "action": "posts_ante",  "amount": 20},
            {"player": "Airball",   "action": "posts_ante",  "amount": 20},
            {"player": "Alex",      "action": "posts_ante",  "amount": 20},
            {"player": "Francisco", "action": "posts_ante",  "amount": 20},
            {"player": "Dylan",     "action": "posts_sb",    "amount": 50},
            {"player": "Airball",   "action": "posts_bb",    "amount": 100},
            {"player": "Alex",      "action": "folds"},
            {"player": "Francisco", "action": "folds"},
            {"player": "Steve",     "action": "raises",      "amount": 300},
            {"player": "Dylan",     "action": "folds"},
            {"player": "Airball",   "action": "folds"},
        ],
        "flop":  [],
        "turn":  [],
        "river": [],
    },
    "board": {"flop": None, "turn": None, "river": None},
    "showdown": [],
}


# ── Tests ─────────────────────────────────────────────────────────────────────

def _run_validate(label: str, hand_json: dict, bb_ante: int, button_seat: int) -> dict:
    pt4 = feed_hand(hand_json, bb_ante=bb_ante, stakes="50/100", button_seat=button_seat)
    viols = validate_hand(pt4)
    check(f"{label}/zero-violations", len(viols) == 0,
          "; ".join(str(v) for v in viols))
    return pt4


def test_hand1_rules() -> None:
    print("\n-- Hand 1: validate_hand (6-player straddle, all-in showdown) --")
    pt4 = _run_validate("h1", HAND1_JSON, bb_ante=100, button_seat=1)

    positions = [p["position"] for p in pt4["players"]]
    check("h1/pos/one-btn", positions.count("BTN") == 1)
    check("h1/pos/one-sb",  positions.count("SB") == 1)
    check("h1/pos/one-bb",  positions.count("BB") == 1)

    all_acts = []
    for st in _STREET_KEYS:
        all_acts.extend((pt4.get("action") or {}).get(st) or [])
    total = sum((a.get("amount") or 0) for a in all_acts)
    check("h1/pot/invested-positive", total > 0, str(total))

    board = pt4.get("board") or {}
    check("h1/card/flop-3",  len(board.get("flop") or []) == 3)
    check("h1/card/turn-1",  isinstance(board.get("turn"), str))
    check("h1/card/river-1", isinstance(board.get("river"), str))

    all_cards: list[str] = list(board.get("flop") or [])
    if board.get("turn"):   all_cards.append(board["turn"])
    if board.get("river"):  all_cards.append(board["river"])
    for p in pt4["players"]:
        all_cards.extend(p.get("hole_cards") or [])
    check("h1/card/no-dups", len(all_cards) == len(set(all_cards)),
          f"dupes in {all_cards}")

    seats = [p["seat"] for p in pt4["players"]]
    check("h1/fmt/unique-seats",   len(seats) == len(set(seats)))
    check("h1/fmt/names-nonempty", all((p.get("name") or "").strip() for p in pt4["players"]))


def test_hand2_rules() -> None:
    print("\n-- Hand 2: validate_hand (5-player, preflop fold-out) --")
    pt4 = _run_validate("h2", HAND2_JSON, bb_ante=100, button_seat=1)

    positions = [p["position"] for p in pt4["players"]]
    check("h2/pos/one-btn", positions.count("BTN") == 1)
    check("h2/pos/one-sb",  positions.count("SB") == 1)
    check("h2/pos/one-bb",  positions.count("BB") == 1)

    preflop = (pt4.get("action") or {}).get("preflop") or []
    check("h2/act/has-sb", any(a.get("action") == "posts_sb" for a in preflop))
    check("h2/act/has-bb", any(a.get("action") == "posts_bb" for a in preflop))

    board = pt4.get("board") or {}
    check("h2/board/no-flop",  not board.get("flop"))
    check("h2/board/no-turn",  board.get("turn") is None)
    check("h2/board/no-river", board.get("river") is None)

    steve = next((p for p in pt4["players"] if p["name"] == "Steve"), None)
    if steve:
        hc = steve.get("hole_cards") or []
        check("h2/card/steve-cards-valid", all(_CARD_RE.match(c) for c in hc), str(hc))
    else:
        check("h2/card/steve-present", False, "Steve not found in players")

    player_names = {p["name"] for p in pt4["players"]}
    all_acts = []
    for st in _STREET_KEYS:
        all_acts.extend((pt4.get("action") or {}).get(st) or [])
    unknown = [a["player"] for a in all_acts if a["player"] not in player_names]
    check("h2/act/no-unknown-players", len(unknown) == 0, str(unknown))


def test_reject_bad_hand() -> None:
    print("\n-- Bad hand: missing BTN, duplicate card, unknown acting player --")

    bad = {
        "players": [
            {"seat": 1, "name": "Alice", "position": "SB", "stack": 10000,
             "hole_cards": ["Ah","Ah"]},
            {"seat": 2, "name": "Bob",   "position": "BB", "stack": 10000,
             "hole_cards": None},
        ],
        "action": {
            "preflop": [
                {"player": "Alice", "action": "posts_sb", "amount": 50},
                {"player": "Bob",   "action": "posts_bb", "amount": 100},
                {"player": "Ghost", "action": "raises",   "amount": 300},
            ],
            "flop": [], "turn": [], "river": [],
        },
        "board": {"flop": None, "turn": None, "river": None},
        "showdown": [],
    }

    viols = validate_hand(bad)
    rule_ids = {v.rule for v in viols}

    check("bad/missing-btn",    "POS-2" in rule_ids, str(rule_ids))
    check("bad/unknown-player", "ACT-3" in rule_ids, str(rule_ids))
    check("bad/duplicate-card", "CARD-6" in rule_ids, str(rule_ids))
    check("bad/has-violations", len(viols) > 0, str(viols))


# ---------------------------------------------------------------------------

def main() -> None:
    print("=== test_rules ===")

    test_hand1_rules()
    test_hand2_rules()
    test_reject_bad_hand()

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
