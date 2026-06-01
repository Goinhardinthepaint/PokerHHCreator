"""
tests/test_e2e_baseline.py

End-to-end baseline: Hand 1 and Hand 2 through the full pipeline, plus
regression tests for known edge cases.

Frozen baseline — do not weaken existing assertions.  Add new ones only
when a new feature is implemented and verified.  Fix the formatter if a
test proves its output is wrong; do not "fix" a test to match wrong output.

Run:
    python tests/test_e2e_baseline.py
"""

from __future__ import annotations

import copy
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.vision.hand_feeder import feed_hand
from src.export.pt4_formatter import format_hand, _preprocess_hand
from src.validate.rules import validate_hand
from src.validate.pt4_linter import lint_hand

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


# ── Helper: run pipeline and return (pt4_dict, formatted_string) ──────────────

def _pipeline(hand_json: dict, bb_ante: int, stakes: str, button_seat: int,
              ts: str = "00:00:00", idx: int = 0,
              stream_url: str = "p0Q7LW64ecM") -> tuple[dict, str]:
    pt4 = feed_hand(hand_json, bb_ante=bb_ante, stakes=stakes, button_seat=button_seat)
    pt4["game"] = {"stakes": stakes}
    pt4["timestamp_start"] = ts
    pt4["button_seat"] = button_seat
    hh = format_hand(pt4, stream_url=stream_url, hand_index=idx)
    return pt4, hh


# ── End-to-end: Hand 1 ────────────────────────────────────────────────────────

def test_hand1_e2e() -> None:
    print("\n-- Hand 1 end-to-end (6-player straddle, flop all-in) --")

    # 1. Parse / validate
    pt4, hh = _pipeline(HAND1_JSON, bb_ante=100, stakes="50/100",
                         button_seat=1, ts="00:38:57", idx=0)
    viols = validate_hand(pt4)
    check("h1/e2e/validate-clean", len(viols) == 0,
          "; ".join(str(v) for v in viols))

    # 2. Stack validation + pot recalculation via _preprocess_hand
    pre = _preprocess_hand(copy.deepcopy(pt4))
    all_amts = sum(a.get("amount", 0)
                   for st in pre["action"].values() for a in st)
    uncalled  = (pre.get("_uncalled") or {}).get("amount", 0)
    check("h1/e2e/pot-invariant",
          pre["pot"]["total"] == all_amts - uncalled,
          f"pot={pre['pot']['total']}  amts={all_amts}  unc={uncalled}  "
          f"expected={all_amts - uncalled}")
    check("h1/e2e/pot-positive", pre["pot"]["total"] > 0, str(pre["pot"]["total"]))

    # 3. PT4 linter (byte-level format check)
    lint = lint_hand(hh, 1)
    check("h1/e2e/linter", lint.passed,
          "; ".join(lint.violations))

    # 4. PT4-sensitive field checks
    check("h1/e2e/header",       hh.startswith("PokerStars Hand #"))
    check("h1/e2e/header-nlhe",  "Hold'em No Limit" in hh)
    check("h1/e2e/header-stakes","($50/$100" in hh)
    check("h1/e2e/table-btn",    "Seat #1 is the button" in hh)
    check("h1/e2e/seat-count",
          len([l for l in hh.splitlines()
               if re.match(r"Seat \d+: \w", l) and "in chips" in l]) == 6)
    check("h1/e2e/antes",        "posts the ante $17" in hh)
    check("h1/e2e/sb",           "Francisco: posts small blind $50" in hh)
    check("h1/e2e/bb",           "Dylan: posts big blind $100" in hh)
    check("h1/e2e/straddle",     "Airball: posts straddle $200" in hh)
    check("h1/e2e/hole-cards",   "*** HOLE CARDS ***" in hh)
    check("h1/e2e/flop",         "*** FLOP *** [Kd 9s 6s]" in hh)
    check("h1/e2e/turn",         "*** TURN ***" in hh and "Ks" in hh)
    check("h1/e2e/river",        "*** RIVER ***" in hh and "2s" in hh)
    check("h1/e2e/allin",        "and is all-in" in hh)
    check("h1/e2e/showdown",     "*** SHOW DOWN ***" in hh)
    check("h1/e2e/winner",       "Alex collected" in hh)
    check("h1/e2e/raise-format", bool(re.search(r"raises \$\d+ to \$\d+", hh)))
    check("h1/e2e/summary",      "*** SUMMARY ***" in hh)
    check("h1/e2e/board-summary","Board [Kd 9s 6s Ks 2s]" in hh)
    check("h1/e2e/rake",         "| Rake $0" in hh)
    check("h1/e2e/double-newline", hh.endswith("\n\n"))
    check("h1/e2e/ascii",
          all(ord(c) < 256 for c in hh),
          "non-latin-1 character in output")


# ── End-to-end: Hand 2 ────────────────────────────────────────────────────────

def test_hand2_e2e() -> None:
    print("\n-- Hand 2 end-to-end (5-player, preflop fold-out) --")

    pt4, hh = _pipeline(HAND2_JSON, bb_ante=100, stakes="50/100",
                         button_seat=1, ts="01:00:00", idx=1)
    viols = validate_hand(pt4)
    check("h2/e2e/validate-clean", len(viols) == 0,
          "; ".join(str(v) for v in viols))

    pre = _preprocess_hand(copy.deepcopy(pt4))
    all_amts = sum(a.get("amount", 0)
                   for st in pre["action"].values() for a in st)
    uncalled  = (pre.get("_uncalled") or {}).get("amount", 0)
    check("h2/e2e/pot-invariant",
          pre["pot"]["total"] == all_amts - uncalled,
          f"pot={pre['pot']['total']}  amts={all_amts}  unc={uncalled}  "
          f"expected={all_amts - uncalled}")
    check("h2/e2e/pot-value",    pre["pot"]["total"] == 350,
          str(pre["pot"]["total"]))

    lint = lint_hand(hh, 1)
    check("h2/e2e/linter", lint.passed, "; ".join(lint.violations))

    check("h2/e2e/header",       hh.startswith("PokerStars Hand #"))
    check("h2/e2e/table-btn",    "Seat #1 is the button" in hh)
    check("h2/e2e/antes",        "posts the ante $20" in hh)
    check("h2/e2e/sb",           "Dylan: posts small blind $50" in hh)
    check("h2/e2e/bb",           "Airball: posts big blind $100" in hh)
    check("h2/e2e/no-flop",      "*** FLOP ***" not in hh)
    check("h2/e2e/no-turn",      "*** TURN ***" not in hh)
    check("h2/e2e/no-river",     "*** RIVER ***" not in hh)
    check("h2/e2e/uncalled",     "Uncalled bet ($200)" in hh)
    check("h2/e2e/winner",       "Steve collected $350" in hh)
    check("h2/e2e/no-board-sum", "Board [" not in hh)
    check("h2/e2e/summary",      "*** SUMMARY ***" in hh)
    check("h2/e2e/rake",         "| Rake $0" in hh)
    check("h2/e2e/double-newline", hh.endswith("\n\n"))


# ── Regression: folded-preflop hand, board=[] vs None ────────────────────────

def test_regression_no_board() -> None:
    """Formatter must omit *** FLOP/TURN/RIVER *** when board is empty list []."""
    print("\n-- Regression: no-board hand (flop=[] vs None) --")

    base_players = [
        {"seat": 1, "name": "Alice", "position": "BTN", "stack": 10000,
         "hole_cards": ["Ah","Kh"]},
        {"seat": 2, "name": "Bob",   "position": "SB",  "stack": 8000,
         "hole_cards": None},
        {"seat": 3, "name": "Carol", "position": "BB",  "stack": 6000,
         "hole_cards": None},
    ]
    base_preflop = [
        {"player": "Bob",   "action": "posts_sb", "amount": 25},
        {"player": "Carol", "action": "posts_bb", "amount": 50},
        {"player": "Alice", "action": "raises",   "amount": 150},
        {"player": "Bob",   "action": "folds"},
        {"player": "Carol", "action": "folds"},
    ]
    showdown = [{"player": "Alice", "hole_cards": ["Ah","Kh"],
                 "hand_description": "", "result": "wins"}]
    common = {
        "game": {"stakes": "25/50"},
        "button_seat": 1,
        "timestamp_start": "00:00:00",
        "bb_ante_spread": {"bb_ante": 0, "ante_per_player": 0},
        "showdown": showdown,
    }

    for variant_name, flop_value in [("none-flop", None), ("empty-list-flop", [])]:
        hand = {
            "players": base_players,
            "action": {
                "preflop": base_preflop,
                "flop": [], "turn": [], "river": [],
            },
            "board": {"flop": flop_value, "turn": None, "river": None},
            **common,
        }
        hh = format_hand(hand, stream_url="test", hand_index=0)

        check(f"no-board/{variant_name}/no-flop-hdr",  "*** FLOP ***"  not in hh)
        check(f"no-board/{variant_name}/no-turn-hdr",  "*** TURN ***"  not in hh)
        check(f"no-board/{variant_name}/no-river-hdr", "*** RIVER ***" not in hh)
        check(f"no-board/{variant_name}/no-board-sum", "Board ["       not in hh)
        check(f"no-board/{variant_name}/summary",      "*** SUMMARY ***" in hh)
        check(f"no-board/{variant_name}/winner",       "Alice collected" in hh)


# ── Regression: straddle hand — straddler acts last preflop ──────────────────

def test_regression_straddle_order() -> None:
    """In a straddle hand, the straddler must act last in the first preflop round."""
    print("\n-- Regression: straddle — straddler acts last preflop --")

    pt4, hh = _pipeline(HAND1_JSON, bb_ante=100, stakes="50/100",
                         button_seat=1, ts="00:38:57", idx=0)

    # Extract just the preflop voluntary action lines
    lines = hh.splitlines()
    preflop_vol = []
    in_preflop = False
    for line in lines:
        if line == "*** HOLE CARDS ***":
            in_preflop = True
            continue
        if line.startswith("*** ") and in_preflop:
            break
        if in_preflop:
            preflop_vol.append(line)

    # Airball (STR) posts straddle and must fold AFTER Alex raises.
    # Both indices are from preflop_vol (same list) to make comparison valid.
    airball_fold_idx = next(
        (i for i, l in enumerate(preflop_vol)
         if l.startswith("Airball:") and "folds" in l), None
    )
    alex_raise_idx = next(
        (i for i, l in enumerate(preflop_vol)
         if l.startswith("Alex:") and "raises" in l), None
    )

    check("straddle/airball-acts-after-alex",
          airball_fold_idx is not None and alex_raise_idx is not None
          and airball_fold_idx > alex_raise_idx,
          f"Airball-folds@{airball_fold_idx} Alex-raises@{alex_raise_idx}")

    # The straddle line must appear before voluntary action
    straddle_line_idx = next(
        (i for i, l in enumerate(lines) if "posts straddle" in l), None
    )
    first_voluntary_idx = next(
        (i for i, l in enumerate(lines) if "raises" in l or "calls" in l
         or ("folds" in l and "HOLE CARDS" not in lines[max(0,i-5):i])), None
    )
    check("straddle/straddle-before-voluntary",
          straddle_line_idx is not None and first_voluntary_idx is not None
          and straddle_line_idx < first_voluntary_idx,
          f"straddle@{straddle_line_idx} voluntary@{first_voluntary_idx}")

    check("straddle/linter", lint_hand(hh, 1).passed)


# ── Regression: all-in cap — call amount capped at remaining stack ────────────

def test_regression_allin_cap() -> None:
    """
    Broke (SB) has only $300 total.  After posting SB $25, she has $275 left.
    Rich raises to $500.  Broke tries to call $500 but is capped at $275.
    Formatter must show 'calls $275 and is all-in', NOT 'calls $500'.
    """
    print("\n-- Regression: all-in cap (calls capped at remaining stack) --")

    hand: dict = {
        "players": [
            {"seat": 1, "name": "Rich",  "position": "BTN", "stack": 10000,
             "hole_cards": ["Ah","Kh"]},
            {"seat": 2, "name": "Broke", "position": "SB",  "stack": 300,
             "hole_cards": ["2d","7c"]},
            {"seat": 3, "name": "Bob",   "position": "BB",  "stack": 5000,
             "hole_cards": None},
        ],
        "action": {
            "preflop": [
                {"player": "Broke", "action": "posts_sb", "amount": 25},
                {"player": "Bob",   "action": "posts_bb", "amount": 50},
                {"player": "Rich",  "action": "raises",   "amount": 500},
                {"player": "Broke", "action": "calls",    "amount": 500},
                {"player": "Bob",   "action": "folds"},
            ],
            "flop": [], "turn": [], "river": [],
        },
        "board": {"flop": None, "turn": None, "river": None},
        "showdown": [
            {"player": "Rich",  "hole_cards": ["Ah","Kh"],
             "hand_description": "high card Ace", "result": "wins"},
            {"player": "Broke", "hole_cards": ["2d","7c"],
             "hand_description": "high card 7",   "result": "loses"},
        ],
        "game": {"stakes": "25/50"},
        "button_seat": 1,
        "timestamp_start": "00:00:00",
        "bb_ante_spread": {"bb_ante": 0, "ante_per_player": 0},
    }

    # Verify through _preprocess_hand directly
    pre = _preprocess_hand(copy.deepcopy(hand))

    # Broke should be capped at 275 (300 total - 25 SB = 275 remaining)
    preflop_acts = pre["action"]["preflop"]
    broke_call = next(
        (a for a in preflop_acts
         if a["player"] == "Broke" and "call" in a["action"]), None
    )
    check("allin-cap/broke-action-present", broke_call is not None,
          str([a["action"] for a in preflop_acts if a["player"] == "Broke"]))
    if broke_call:
        check("allin-cap/broke-is-allin",
              broke_call["action"] == "calls_allin",
              f"action={broke_call['action']!r}")
        check("allin-cap/broke-amount-capped",
              broke_call["amount"] == 275,
              f"amount={broke_call['amount']}")

    # Formatted output check
    hh = format_hand(hand, stream_url="test", hand_index=0)
    check("allin-cap/fmt-calls-allin",    "calls $275 and is all-in" in hh)
    check("allin-cap/fmt-not-500",        "calls $500" not in hh)
    check("allin-cap/fmt-uncalled",       "Uncalled bet ($200)" in hh)

    # Pot invariant: 25+50+500+275 - 200 = 650
    all_amts = sum(a.get("amount", 0) for st in pre["action"].values() for a in st)
    unc      = (pre.get("_uncalled") or {}).get("amount", 0)
    check("allin-cap/pot-invariant",
          pre["pot"]["total"] == all_amts - unc,
          f"pot={pre['pot']['total']}  amts={all_amts}  unc={unc}")
    check("allin-cap/pot-value",
          pre["pot"]["total"] == 650,
          str(pre["pot"]["total"]))

    check("allin-cap/linter", lint_hand(hh, 1).passed)


# ── Regression: pot total == investments − uncalled ───────────────────────────

def test_regression_pot_invariant() -> None:
    """
    For every hand: pot['total'] must equal the sum of all action amounts
    in the preprocessed dict minus the uncalled bet amount.
    This must hold even when a player calls all-in (calls_allin action).
    """
    print("\n-- Regression: pot invariant (pot = investments - uncalled) --")

    for label, hand_json, bb_ante, btn in [
        ("h1", HAND1_JSON, 100, 1),
        ("h2", HAND2_JSON, 100, 1),
    ]:
        pt4, _ = _pipeline(hand_json, bb_ante=bb_ante, stakes="50/100",
                            button_seat=btn)
        pre = _preprocess_hand(copy.deepcopy(pt4))
        all_amts = sum(a.get("amount", 0)
                       for st in pre["action"].values() for a in st)
        unc      = (pre.get("_uncalled") or {}).get("amount", 0)
        check(f"pot-inv/{label}",
              pre["pot"]["total"] == all_amts - unc,
              f"pot={pre['pot']['total']}  amts={all_amts}  unc={unc}  "
              f"expected={all_amts - unc}")


# ── Regression: missing-caller inference — divisibility + candidates ──────────

def test_regression_pot_delta_resolver() -> None:
    """
    PotDeltaResolver must infer calls only when:
      (a) missing_delta % call_amount == 0  AND
      (b) len(candidates) >= n_callers needed
    """
    print("\n-- Regression: pot delta resolver (divisibility + candidates) --")

    from src.engine.pot_delta_resolver import Action, InferredAction, PotDeltaResolver
    r = PotDeltaResolver()

    # Case A: divisible + enough candidates → infer
    result_a = r.resolve(
        [Action("Alice", "raises", 300)],
        expected_pot=900, call_amount=300, candidates=["Bob", "Charlie"]
    )
    check("resolver/divisible-infers",
          len(result_a) == 3 and sum(isinstance(a, InferredAction) for a in result_a) == 2)

    # Case B: not divisible → no inference
    result_b = r.resolve(
        [Action("Alice", "raises", 300)],
        expected_pot=850, call_amount=300, candidates=["Bob", "Charlie"]
    )
    check("resolver/indivisible-no-infer",
          len(result_b) == 1 and not any(isinstance(a, InferredAction) for a in result_b))

    # Case C: divisible but not enough candidates → no inference
    result_c = r.resolve(
        [Action("Alice", "raises", 300)],
        expected_pot=900, call_amount=300, candidates=["Bob"]  # only 1 candidate, need 2
    )
    check("resolver/insufficient-candidates",
          len(result_c) == 1 and not any(isinstance(a, InferredAction) for a in result_c))

    # Case D: exact match → nothing inferred
    result_d = r.resolve(
        [Action("Alice", "raises", 300), Action("Bob", "calls", 300)],
        expected_pot=600, call_amount=300, candidates=["Charlie"]
    )
    check("resolver/exact-match-no-infer",
          len(result_d) == 2 and not any(isinstance(a, InferredAction) for a in result_d))


# ── Runner ────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== test_e2e_baseline ===")

    test_hand1_e2e()
    test_hand2_e2e()
    test_regression_no_board()
    test_regression_straddle_order()
    test_regression_allin_cap()
    test_regression_pot_invariant()
    test_regression_pot_delta_resolver()

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
