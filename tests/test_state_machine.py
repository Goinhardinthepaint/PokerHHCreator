"""
tests/test_state_machine.py

Unit tests for PokerStateMachine and HandFeeder.
No API calls -- all data is hard-coded from known hands.

Run:
    python tests/test_state_machine.py

Hand 1: SPEC.md example -- 6 players, CO straddle by Airball, flop all-in.
Hand 2: Simple 5-player hand -- no straddle, preflop fold-out, no showdown.
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.poker_state_machine import PokerStateMachine
from src.vision.hand_feeder import feed_hand
from src.export.pt4_formatter import format_hand

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        msg = f"  FAIL  {label}" + (f": {detail}" if detail else "")
        print(msg)
        failures.append(msg)


# ---------------------------------------------------------------------------
# Hand 1 fixtures (from SPEC.md example)
#
# 6 players, $50/$100, BB-ante $100 -> $17/player
# Airball (seat 6, CO) straddles $200
# Preflop order (CO straddle): BTN(1) SB(2) BB(3) UTG(4) HJ(5) STR(6)
#   Greedo raises to 600, Francisco calls 550, Dylan folds,
#   Otto calls 600, Alex raises to 3000, Airball folds,
#   Greedo calls, Francisco folds, Otto calls
# Flop [Kd 9s 6s]: Otto checks, Alex bets 3000, Otto raises to 21000 (all-in),
#                   Alex calls 18000, Greedo folds
# Turn [Ks], River [2s]
# Showdown: Otto [Tc 8d] pair of Kings loses; Alex [Ah 5d] pair of Kings wins
# ---------------------------------------------------------------------------

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
    "board": {
        "flop":  ["Kd","9s","6s"],
        "turn":  "Ks",
        "river": "2s",
    },
    "showdown": [
        {"player": "Otto", "hole_cards": ["Tc","8d"],
         "hand_description": "a pair of Kings", "result": "loses"},
        {"player": "Alex", "hole_cards": ["Ah","5d"],
         "hand_description": "a pair of Kings", "result": "wins"},
    ],
}


def test_hand1_state_machine() -> None:
    print("\n-- Hand 1: state machine (direct API) --")
    sm = PokerStateMachine("50/100")
    sm.set_button(1)
    sm.set_ante_spread(bb_ante=100, ante_per_player=17)

    for p in HAND1_JSON["players"]:
        sm.add_player(p["seat"], p["name"], p["stack"], p["position"])
    for p in HAND1_JSON["players"]:
        if p.get("hole_cards"):
            sm.deal_hole_cards(p["name"], p["hole_cards"])

    pf = HAND1_JSON["action"]["preflop"]
    for act in pf:
        if act["action"] == "posts_ante":
            sm.post_ante(act["player"], act["amount"])
    sm.post_blind("Francisco", 50, "sb")
    sm.post_blind("Dylan", 100, "bb")
    sm.post_straddle("Airball", 200)
    sm.begin_preflop()

    for act in pf:
        if act["action"] not in ("posts_ante","posts_sb","posts_bb","posts_straddle"):
            sm.action(act["player"], act["action"], act.get("amount", 0))

    # Pot after preflop (incremental chips added, not player totals):
    #   Antes: 6x17=102, SB=50, BB=100, Straddle=200
    #   Greedo raises +600, Francisco calls +550 (had 50 SB),
    #   Otto calls +600, Alex raises +3000,
    #   Greedo calls +2400 (had 600), Otto calls +2400 (had 600)
    expected_preflop_pot = (6*17) + 50 + 100 + 200 + 600 + 550 + 600 + 3000 + 2400 + 2400
    check("hand1/preflop-pot", sm.pot == expected_preflop_pot,
          f"got {sm.pot}, expected {expected_preflop_pot}")

    sm.deal_board(["Kd","9s","6s"])
    for act in HAND1_JSON["action"]["flop"]:
        sm.action(act["player"], act["action"], act.get("amount", 0))

    # Flop (incremental):
    #   Alex bets +3000; Otto raises all-in to 20983 actual (stack=24000-17-3000=20983);
    #   Alex calls +15983 actual (stack=22000-17-3000-3000=15983, calls all-in)
    expected_flop_addition = 3000 + 20983 + 15983
    expected_total_pot = expected_preflop_pot + expected_flop_addition
    check("hand1/total-pot", sm.pot == expected_total_pot,
          f"got {sm.pot}, expected {expected_total_pot}")

    sm.deal_board(["Ks"])
    sm.deal_board(["2s"])

    sm.add_showdown("Otto", ["Tc","8d"], "a pair of Kings", "loses")
    sm.add_showdown("Alex", ["Ah","5d"], "a pair of Kings", "wins")

    d = sm.to_pt4_dict()
    check("hand1/players-count", len(d["players"]) == 6)
    check("hand1/flop-cards", d["board"]["flop"] == ["Kd","9s","6s"])
    check("hand1/turn-card",  d["board"]["turn"]  == "Ks")
    check("hand1/river-card", d["board"]["river"] == "2s")
    check("hand1/showdown-has-winner",
          any(s["result"] == "wins" for s in d["showdown"]))
    check("hand1/preflop-has-straddle",
          any(a["action"] == "posts_straddle" for a in d["action"]["preflop"]))
    check("hand1/otto-all-in",
          any(a["action"] == "all_in" and a["player"] == "Otto"
              for a in d["action"]["flop"]),
          f"flop actions: {d['action']['flop']}")

    folded_preflop = {"Dylan", "Airball", "Francisco"}
    flop_players = {a["player"] for a in d["action"]["flop"]}
    check("hand1/preflop-folders-not-in-flop",
          not (folded_preflop & flop_players),
          f"overlap: {folded_preflop & flop_players}")


def test_hand1_via_feeder() -> None:
    print("\n-- Hand 1: via HandFeeder + pt4_formatter --")
    pt4 = feed_hand(HAND1_JSON, bb_ante=100, stakes="50/100", button_seat=1)
    pt4["game"] = {"stakes": "50/100"}
    pt4["timestamp_start"] = "00:38:57"
    pt4["button_seat"] = 1

    hh = format_hand(pt4, stream_url="p0Q7LW64ecM", hand_index=0)

    check("hand1/feeder/header",      hh.startswith("PokerStars Hand #"))
    check("hand1/feeder/table-line",  "Seat #1 is the button" in hh)
    check("hand1/feeder/antes",       "posts the ante $17" in hh)
    check("hand1/feeder/sb",          "Francisco: posts small blind $50" in hh)
    check("hand1/feeder/bb",          "Dylan: posts big blind $100" in hh)
    check("hand1/feeder/straddle",    "Airball: posts straddle $200" in hh)
    check("hand1/feeder/flop",        "*** FLOP *** [Kd 9s 6s]" in hh)
    check("hand1/feeder/turn",        "*** TURN ***" in hh and "Ks" in hh)
    check("hand1/feeder/river",       "*** RIVER ***" in hh and "2s" in hh)
    check("hand1/feeder/showdown",    "*** SHOW DOWN ***" in hh)
    check("hand1/feeder/winner",      "Alex" in hh and "collected" in hh)
    check("hand1/feeder/rake",        "| Rake $0" in hh)
    check("hand1/feeder/summary",     "*** SUMMARY ***" in hh)
    check("hand1/feeder/in-chips",    "in chips" in hh)
    # Greedo (non-BB): 19000 + 17 = 19017
    check("hand1/feeder/stack-greedo", "Greedo ($19017 in chips)" in hh,
          f"lines: {[l for l in hh.splitlines() if 'Greedo' in l and 'chips' in l]}")
    # Dylan (BB): 18800 - (100-17) = 18717
    check("hand1/feeder/stack-dylan",  "Dylan ($18717 in chips)" in hh,
          f"lines: {[l for l in hh.splitlines() if 'Dylan' in l and 'chips' in l]}")
    check("hand1/feeder/raise-format", re.search(r"raises \$\d+ to \$\d+", hh) is not None)

    print("\nFormatted Hand 1:\n" + "-"*60)
    print(hh)
    print("-"*60)


# ---------------------------------------------------------------------------
# Hand 2 fixtures (simple 5-player, no straddle, preflop fold-out)
#
# 5 players, $50/$100, BB-ante $100 -> $20/player
# Preflop: Alex and Francisco fold, Steve (BTN) raises to 300,
#          Dylan (SB) folds, Airball (BB) folds -- Steve wins uncontested
# No flop, no showdown
# ---------------------------------------------------------------------------

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
            # UTG(4) CO(5) BTN(1) SB(2) BB(3)
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


def test_hand2_state_machine() -> None:
    print("\n-- Hand 2: state machine (preflop fold-out) --")
    sm = PokerStateMachine("50/100")
    sm.set_button(1)
    sm.set_ante_spread(bb_ante=100, ante_per_player=20)

    for p in HAND2_JSON["players"]:
        sm.add_player(p["seat"], p["name"], p["stack"], p["position"])
    for p in HAND2_JSON["players"]:
        if p.get("hole_cards"):
            sm.deal_hole_cards(p["name"], p["hole_cards"])

    pf = HAND2_JSON["action"]["preflop"]
    for act in pf:
        if act["action"] == "posts_ante":
            sm.post_ante(act["player"], act["amount"])
    sm.post_blind("Dylan", 50, "sb")
    sm.post_blind("Airball", 100, "bb")
    sm.begin_preflop()

    for act in pf:
        if act["action"] not in ("posts_ante","posts_sb","posts_bb"):
            sm.action(act["player"], act["action"], act.get("amount", 0))

    # Pot = 5x20 antes + 50 SB + 100 BB + 300 Steve raise = 550
    check("hand2/pot", sm.pot == 550, f"got {sm.pot}")
    check("hand2/steve-active",   sm._get_player("Steve").status == "active")
    check("hand2/others-folded",
          all(sm._get_player(n).status == "folded"
              for n in ["Dylan","Airball","Alex","Francisco"]))
    check("hand2/no-flop",        not sm.flop_cards)
    check("hand2/hand-complete",  sm.is_hand_complete())


def test_hand2_via_feeder() -> None:
    print("\n-- Hand 2: via HandFeeder + pt4_formatter --")
    pt4 = feed_hand(HAND2_JSON, bb_ante=100, stakes="50/100", button_seat=1)
    pt4["game"] = {"stakes": "50/100"}
    pt4["timestamp_start"] = "01:00:00"
    pt4["button_seat"] = 1

    hh = format_hand(pt4, stream_url="p0Q7LW64ecM", hand_index=1)

    check("hand2/feeder/header",   hh.startswith("PokerStars Hand #"))
    check("hand2/feeder/antes",    "posts the ante $20" in hh)
    check("hand2/feeder/sb",       "Dylan: posts small blind $50" in hh)
    check("hand2/feeder/bb",       "Airball: posts big blind $100" in hh)
    check("hand2/feeder/no-flop",  "*** FLOP ***" not in hh)
    check("hand2/feeder/steve-wins","Steve" in hh and "collected" in hh)
    check("hand2/feeder/uncalled", "Uncalled bet" in hh)
    check("hand2/feeder/summary",  "*** SUMMARY ***" in hh)
    check("hand2/feeder/rake",     "| Rake $0" in hh)
    # Steve (BTN non-BB): 15000 + 20 = 15020
    check("hand2/feeder/stack-steve", "Steve ($15020 in chips)" in hh,
          f"lines: {[l for l in hh.splitlines() if 'Steve' in l and 'chips' in l]}")

    print("\nFormatted Hand 2:\n" + "-"*60)
    print(hh)
    print("-"*60)


# ---------------------------------------------------------------------------
# Out-of-order action queueing test
# Same as Hand 2 voluntary actions fed in a scrambled order
# ---------------------------------------------------------------------------

def test_ooo_queue() -> None:
    print("\n-- Out-of-order action queueing --")
    sm = PokerStateMachine("50/100")
    sm.set_button(1)
    for p in HAND2_JSON["players"]:
        sm.add_player(p["seat"], p["name"], p["stack"], p["position"])
    for act in HAND2_JSON["action"]["preflop"]:
        if act["action"] == "posts_ante":
            sm.post_ante(act["player"], act["amount"])
    sm.post_blind("Dylan", 50, "sb")
    sm.post_blind("Airball", 100, "bb")
    sm.begin_preflop()

    # Feed in a scrambled order:
    # Correct order: Alex(UTG), Francisco(CO), Steve(BTN), Dylan(SB), Airball(BB)
    scrambled = [
        {"player": "Airball",   "action": "folds"},                  # BB, should be last
        {"player": "Steve",     "action": "raises", "amount": 300},  # BTN, should be 3rd
        {"player": "Alex",      "action": "folds"},                  # UTG, correct (1st)
        {"player": "Dylan",     "action": "folds"},                  # SB, should be 4th
        {"player": "Francisco", "action": "folds"},                  # CO, should be 2nd
    ]
    for act in scrambled:
        sm.action(act["player"], act["action"], act.get("amount", 0))

    check("ooo/all-resolved",  len(sm._pending_queue) == 0,
          f"still pending: {sm._pending_queue}")
    check("ooo/steve-pot",     sm.pot == 550, f"got {sm.pot}")
    check("ooo/hand-complete", sm.is_hand_complete())
    vol_count = len([
        a for a in sm._action_log["preflop"]
        if a["action"] not in ("posts_ante","posts_sb","posts_bb")
    ])
    check("ooo/log-count", vol_count == 5, f"got {vol_count}")


# ---------------------------------------------------------------------------
# Round-close test: once all active players match the bet, no more actions
# ---------------------------------------------------------------------------

def test_round_closes_after_all_call() -> None:
    print("\n-- Round closes after all players match the bet --")
    sm = PokerStateMachine("50/100")
    sm.set_button(1)
    sm.set_ante_spread(bb_ante=100, ante_per_player=33)
    sm.add_player(1, "Alice", 10000, "BTN")
    sm.add_player(2, "Bob",   10000, "SB")
    sm.add_player(3, "Carol", 10000, "BB")
    sm.post_ante("Alice", 33)
    sm.post_ante("Bob",   33)
    sm.post_ante("Carol", 34)
    sm.post_blind("Bob",   50, "sb")
    sm.post_blind("Carol", 100, "bb")
    sm.begin_preflop()

    # Preflop order (BTN acts first 3-handed): Alice, Bob, Carol
    r1 = sm.action("Alice", "raises", 4000)
    r2 = sm.action("Bob",   "calls",  4000)
    r3 = sm.action("Carol", "calls",  4000)   # all match → round must close
    check("round-close/all-applied", r1 and r2 and r3,
          f"r1={r1} r2={r2} r3={r3}")
    check("round-close/round-closed", sm._round_closed,
          "round should be closed after all callers match")

    # A fold submitted after the round closes must be rejected
    fold_result = sm.action("Alice", "folds")
    check("round-close/fold-rejected", not fold_result,
          "fold after round closed should return False")
    check("round-close/alice-still-active",
          sm._get_player("Alice").status == "active",
          f"Alice status: {sm._get_player('Alice').status}")

    # deal_board() must re-open the street
    sm.deal_board(["Ah", "Kd", "Qc"])
    check("round-close/reopened-after-board", not sm._round_closed,
          "round should reopen after deal_board()")
    postflop_fold = sm.action("Bob", "folds")
    check("round-close/postflop-action-accepted", postflop_fold,
          "postflop fold should be accepted after board is dealt")


# ---------------------------------------------------------------------------

def main() -> None:
    print("=== test_state_machine ===")

    test_hand1_state_machine()
    test_hand1_via_feeder()
    test_hand2_state_machine()
    test_hand2_via_feeder()
    test_ooo_queue()
    test_round_closes_after_all_call()

    print()
    if failures:
        print(f"RESULT: FAIL  ({len(failures)} assertion(s) failed)")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        total = sum(1 for v in [True] * 100)  # placeholder
        print("RESULT: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
