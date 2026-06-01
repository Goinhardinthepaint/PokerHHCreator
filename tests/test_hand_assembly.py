"""
tests/test_hand_assembly.py
Zero-API test: loads cached frames, runs pipeline + formatter, asserts correctness.
Run: python tests/test_hand_assembly.py
"""
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.video.pipeline import VideoPipeline, hand_states_to_dicts
from src.export.pt4_formatter import format_session, _preprocess_hand

CACHE_2000 = os.path.join(os.path.dirname(__file__), "..", "output", "frames_cache_p0Q7LW64ecM_2000-2120.json")
CACHE_2120 = os.path.join(os.path.dirname(__file__), "..", "output", "frames_cache_p0Q7LW64ecM_2120-2300.json")
URL = "https://www.youtube.com/watch?v=p0Q7LW64ecM"

_BB_ANTE = 100
_STRADDLE_HINTS_2000 = [
    {"straddle_player": "Airball", "straddle_amount": 200, "bb_ante": _BB_ANTE, "bb_player": "Dylan"}
]
_BLIND_ACTIONS = {"posts_sb", "posts_bb", "posts_ante", "posts_straddle"}
_VOLUNTARY = {"calls", "calls_allin", "bets", "bets_allin", "raises", "all_in"}
_STREETS = ["preflop", "flop", "turn", "river"]

failures: list[str] = []


def check(test_id: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS  {test_id}")
    else:
        msg = f"FAIL  {test_id}" + (f": {detail}" if detail else "")
        print(msg)
        failures.append(msg)


def load_and_run(cache_path: str, hints_list=None) -> list[dict]:
    with open(cache_path) as f:
        frames = json.load(f)
    timestamps = [f"00:{i//60:02d}:{i%60:02d}.000" for i in range(len(frames))]
    vp = VideoPipeline()
    completed, _ = vp.process(frames, timestamps)
    return hand_states_to_dicts(completed, stream_url=URL, hints_list=hints_list)


def inject_antes_for_straddle(hands: list[dict]) -> None:
    """Replicates reexport.py ante injection for the Airball straddle hand."""
    airball_hand = next(
        (h for h in hands
         if any(a.get("action") == "posts_straddle" and a.get("player") == "Airball"
                for a in h.get("action", {}).get("preflop", []))),
        None,
    )
    if not airball_hand or not airball_hand.get("players"):
        return
    h = airball_hand
    num = len(h["players"])
    ante_pp = math.ceil(_BB_ANTE / num)
    sorted_players = sorted(h["players"], key=lambda p: p.get("seat", 99))
    antes = [{"player": p["name"], "action": "posts_ante", "amount": ante_pp} for p in sorted_players]
    pf = h["action"]["preflop"]
    blinds    = [a for a in pf if a["action"] in ("posts_sb", "posts_bb")]
    straddle  = [a for a in pf if a["action"] == "posts_straddle"]
    non_blind = [a for a in pf if a["action"] not in ("posts_sb", "posts_bb", "posts_ante", "posts_straddle")]
    h["action"]["preflop"] = antes + blinds + straddle + non_blind
    h["bb_ante_spread"] = {"ante_per_player": ante_pp, "bb_ante": _BB_ANTE}


def run_suite(hands: list[dict], label: str) -> None:
    active    = [h for h in hands if h.get("players")]
    processed = [_preprocess_hand(h) for h in active]
    txt       = format_session(hands, stream_url=URL)
    blocks    = [b for b in re.split(r"(?=PokerStars Hand #)", txt) if b.strip()]

    # 1. Empty hands suppressed — no formatted block missing seat entries
    bad = [b[:50] for b in blocks if not re.search(r"^Seat \d+:.+in chips", b, re.MULTILINE)]
    check(f"{label}/empty-hands-suppressed", not bad, str(bad[:2]))

    # 2. Once a player folds on a street, no further actions from that player on the same street
    vios = []
    for h in active:
        for street, acts in h.get("action", {}).items():
            for pkey in {a["player"].casefold() for a in acts if a["action"] == "folds"}:
                fold_idx = next(
                    i for i, a in enumerate(acts)
                    if a["player"].casefold() == pkey and a["action"] == "folds"
                )
                after = [a for a in acts[fold_idx + 1:] if a["player"].casefold() == pkey]
                if after:
                    vios.append(f"#{h['hand_number']} {street} {acts[fold_idx]['player']} acted after fold")
    check(f"{label}/no-act-after-fold-same-street", not vios, "; ".join(vios[:2]))

    # 3. No action on any later street after a player folded
    vios = []
    for h in active:
        action = h.get("action", {})
        for si, street in enumerate(_STREETS):
            folded_here = {a["player"].casefold() for a in action.get(street, []) if a["action"] == "folds"}
            for later in _STREETS[si + 1:]:
                for a in action.get(later, []):
                    if a["action"] in _BLIND_ACTIONS:
                        continue
                    if a["player"].casefold() in folded_here:
                        vios.append(f"#{h['hand_number']} {a['player']} folded {street} acted {later}")
    check(f"{label}/no-act-after-fold", not vios, "; ".join(vios[:2]))

    # 4. Call amounts are positive ints in the processed action dicts
    vios = []
    for h in processed:
        for street, acts in h.get("action", {}).items():
            for a in acts:
                if a["action"] in ("calls", "calls_allin"):
                    amt = a.get("amount")
                    if not isinstance(amt, int) or amt <= 0:
                        vios.append(f"#{h['hand_number']} {street} {a['player']} calls ${amt}")
    check(f"{label}/call-amounts-positive-int", not vios, "; ".join(vios[:2]))

    # 5. Raise total Y > current facing bet X at time of raise
    vios = []
    for h in processed:
        action = h.get("action", {})
        game   = h.get("game", {})
        try:
            _sb, _bb = (int(x) for x in game.get("stakes", "50/100").split("/"))
        except Exception:
            _sb, _bb = 50, 100
        for street_name in _STREETS:
            acts = action.get(street_name, [])
            straddle_a = next((a for a in acts if a.get("action") == "posts_straddle"), None)
            current_bet = straddle_a["amount"] if straddle_a else (_bb if street_name == "preflop" else 0)
            for a in [x for x in acts if x["action"] not in _BLIND_ACTIONS]:
                if a["action"] in ("raises", "all_in"):
                    amt = a.get("amount") or 0
                    if amt <= current_bet:
                        vios.append(
                            f"#{h['hand_number']} {street_name}: {a['player']} "
                            f"raises to ${amt} ≤ current ${current_bet}"
                        )
                    current_bet = max(current_bet, amt)
                elif a["action"] in ("bets", "bets_allin"):
                    current_bet = a.get("amount") or 0
    check(f"{label}/raise-Y-gt-facing-X", not vios, "; ".join(vios[:2]))

    # 6. Pot = sum(all contributions) − uncalled bet
    vios = []
    for h in processed:
        total = sum(
            a.get("amount") or 0
            for street in _STREETS
            for a in h.get("action", {}).get(street, [])
            if a["action"] in _BLIND_ACTIONS | _VOLUNTARY
        )
        uncalled  = (h.get("_uncalled") or {}).get("amount", 0)
        expected  = total - uncalled
        reported  = h.get("pot", {}).get("total", 0)
        if expected != reported:
            vios.append(f"#{h['hand_number']} expected={expected} reported={reported}")
    check(f"{label}/pot-equals-contributions", not vios, "; ".join(vios[:2]))

    # 7. No player's total contributions exceed their (ante-adjusted) starting stack
    vios = []
    for h in processed:
        players   = h.get("players", [])
        action    = h.get("action", {})
        bb_spread = h.get("bb_ante_spread") or {}
        ante_pp   = bb_spread.get("ante_per_player", 0)
        bb_total  = bb_spread.get("bb_ante", 0)
        blind_a   = [a for a in action.get("preflop", []) if a["action"] in _BLIND_ACTIONS]
        bb_player = next((a["player"] for a in blind_a if a["action"] == "posts_bb"), None)

        starts: dict[str, int] = {}
        for p in players:
            key  = (p.get("name") or "").casefold()
            orig = p.get("stack", 0)
            if ante_pp:
                adj = max(0, orig - (bb_total - ante_pp)) if p["name"] == bb_player else orig + ante_pp
            else:
                adj = orig
            starts[key] = adj

        contribs: dict[str, int] = {}
        for street in _STREETS:
            for a in action.get(street, []):
                if a["action"] in _BLIND_ACTIONS | _VOLUNTARY:
                    k = a["player"].casefold()
                    contribs[k] = contribs.get(k, 0) + (a.get("amount") or 0)

        for k, total in contribs.items():
            start = starts.get(k, 0)
            if start > 0 and total > start + 1:  # +1 for rounding tolerance
                display = next((p["name"] for p in players if p["name"].casefold() == k), k)
                vios.append(f"#{h['hand_number']} {display} committed ${total} > stack ${start}")
    check(f"{label}/action-le-stack", not vios, "; ".join(vios[:2]))

    # 8. Postflop: a player who called on a street cannot also fold on that same street.
    # (Preflop limp-folds are valid so preflop is excluded from this check.)
    vios = []
    for h in active:
        for street, acts in h.get("action", {}).items():
            if street == "preflop":
                continue
            callers = {a["player"].casefold() for a in acts if a["action"] in ("calls", "calls_allin")}
            folders = {a["player"].casefold() for a in acts if a["action"] == "folds"}
            for pkey in callers & folders:
                display = next((a["player"] for a in acts if a["player"].casefold() == pkey), pkey)
                vios.append(f"#{h['hand_number']} {street} {display} called+folded same street")
    check(f"{label}/postflop-call-then-fold-next-street", not vios, "; ".join(vios[:2]))

    # 9. Every formatted hand block has at least one Seat entry in *** SUMMARY ***
    vios = []
    for block in blocks:
        m = re.search(r"\*\*\* SUMMARY \*\*\*(.*)", block, re.DOTALL)
        if not m:
            hid = re.search(r"PokerStars Hand #(\d+)", block)
            vios.append(f"hand #{hid.group(1) if hid else '?'} missing SUMMARY section")
            continue
        if not re.search(r"^Seat \d+:", m.group(1), re.MULTILINE):
            hid = re.search(r"PokerStars Hand #(\d+)", block)
            vios.append(f"hand #{hid.group(1) if hid else '?'} SUMMARY has no seat entries")
    check(f"{label}/players-in-summary", not vios, "; ".join(vios[:2]))


def main() -> None:
    print("=== test_hand_assembly ===\n")

    print("Loading 2000-2120 cache...")
    hands_2000 = load_and_run(CACHE_2000, hints_list=_STRADDLE_HINTS_2000)
    inject_antes_for_straddle(hands_2000)
    run_suite(hands_2000, "2000-2120")

    print()
    print("Loading 2120-2300 cache...")
    hands_2120 = load_and_run(CACHE_2120)
    run_suite(hands_2120, "2120-2300")

    print()
    if failures:
        print(f"RESULT: FAIL  ({len(failures)} assertion(s) failed)")
        sys.exit(1)
    else:
        print(f"RESULT: PASS  (18 assertions)")
        sys.exit(0)


if __name__ == "__main__":
    main()
