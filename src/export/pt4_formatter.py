"""
PT4 Hand History Formatter
Outputs PokerStars-compatible .txt format for import into PokerTracker 4.

Spec: SPEC.md — PT4 HAND HISTORY FORMAT section.
"""

import copy
import hashlib
from datetime import datetime
from typing import Optional


_BLIND_ACTIONS = {"posts_sb", "posts_bb", "posts_ante", "posts_straddle"}
_VOLUNTARY_ACTIONS = {"calls", "calls_allin", "bets", "bets_allin", "raises", "all_in"}
_SUMMARY_POS_LABEL = {"BTN": "button", "SB": "small blind", "BB": "big blind"}

# Canonical posting order: antes → SB → BB → straddle
_BLIND_SORT = {"posts_ante": 0, "posts_sb": 1, "posts_bb": 2, "posts_straddle": 3}


class HandValidationError(ValueError):
    """Raised when a hand fails structural validation and must be skipped."""


def _blind_sort_key(a: dict) -> int:
    return _BLIND_SORT.get(a.get("action", ""), 99)


def generate_hand_id(stream_url: str, timestamp: str, hand_index: int) -> str:
    raw = f"{stream_url}:{timestamp}:{hand_index}"
    hash_hex = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return str(int(hash_hex, 16) % 10**12)


def format_cards(cards: list[str]) -> str:
    if not cards:
        return ""
    return "[" + " ".join(cards) + "]"


# ── Stack validation + pot recalculation ──────────────────────────────────────

def _preprocess_hand(hand: dict) -> dict:
    """Validate actions against player stacks; recalculate pot and uncalled bet.

    Modifies a deep-copy of the hand in place:
    - Caps call/raise amounts at remaining stack
    - Marks capped calls as "calls_allin", capped bets as "bets_allin"
    - Sets hand["pot"]["total"] = sum of validated contributions - uncalled
    - Sets hand["_uncalled"] = {"player": name, "amount": N} or None
    """
    hand = copy.deepcopy(hand)

    game = hand.get("game", {})
    stakes = game.get("stakes", "50/100")
    try:
        # Stakes may be "sb/bb" or triple-blind "sb/bb/mandatory_straddle".
        _p = stakes.split("/")
        sb, bb = int(_p[0]), int(_p[1])
    except Exception:
        sb, bb = 50, 100

    players = hand.get("players") or []
    raw_action = hand.get("action") or {}
    # Normalize: Claude sometimes returns null for a street instead of []
    action = {s: (raw_action.get(s) or []) for s in ("preflop", "flop", "turn", "river")}

    # Casefold → display name
    name_display: dict[str, str] = {}
    for p in players:
        n = (p.get("name") or "").strip()
        name_display[n.casefold()] = n

    # Remaining stacks must start from ADJUSTED stacks (same as seat listing),
    # not the raw OCR stacks.  After the displayed $17 ante is posted, each
    # player's remaining chips equal their original (real-game) stack.
    bb_ante_spread = hand.get("bb_ante_spread") or {}
    ante_per_player_adj = bb_ante_spread.get("ante_per_player", 0)
    bb_ante_total_adj   = bb_ante_spread.get("bb_ante", 0)
    preflop_acts_tmp    = action["preflop"]
    blind_acts_tmp      = [a for a in preflop_acts_tmp if a["action"] in _BLIND_ACTIONS]
    bb_player_for_adj   = next(
        (a["player"] for a in blind_acts_tmp if a["action"] == "posts_bb"), None
    )

    remaining: dict[str, int] = {}
    for p in players:
        key  = (p.get("name") or "").casefold()
        orig = p.get("stack") or 0
        if ante_per_player_adj:
            if p.get("name") == bb_player_for_adj:
                adj = max(0, orig - (bb_ante_total_adj - ante_per_player_adj))
            else:
                adj = orig + ante_per_player_adj
        else:
            adj = orig
        remaining[key] = adj

    pot = 0
    all_in_players: set[str] = set()

    # ── Deduct blind / ante / straddle posts from stacks ─────────────────────
    preflop_acts = action["preflop"]
    blind_acts = [a for a in preflop_acts if a["action"] in _BLIND_ACTIONS]

    for a in blind_acts:
        key = a["player"].casefold()
        amt = a.get("amount") or 0
        actual = min(amt, remaining.get(key, 0))
        if key in remaining:
            remaining[key] -= actual
        pot += actual

    # ── Infer missing SB/BB posts from player positions ───────────────────────
    # Vision sometimes misses the blind-posting frames (e.g. when the window
    # starts mid-hand).  Infer from player positions so pot accounting is
    # correct.  The inferred posts are inserted into blind_acts so that
    # action["preflop"] and the formatter both include them.
    if not any(a["action"] in ("posts_sb", "posts_bb") for a in blind_acts):
        inferred_blinds: list[dict] = []
        for p in players:
            pos   = (p.get("position") or "").upper()
            pname = (p.get("name") or "").strip()
            key   = pname.casefold()
            if not pname or key not in remaining:
                continue
            if pos == "SB":
                actual = min(sb, remaining[key])
                remaining[key] -= actual
                pot += actual
                inferred_blinds.insert(0, {"player": pname, "action": "posts_sb", "amount": actual})
            elif pos == "BB":
                actual = min(bb, remaining[key])
                remaining[key] -= actual
                pot += actual
                inferred_blinds.append({"player": pname, "action": "posts_bb", "amount": actual})
        # Antes come before SB/BB in the hand history spec;
        # append inferred SB/BB after any antes already in blind_acts.
        antes_in_blind = [a for a in blind_acts if a["action"] == "posts_ante"]
        non_antes      = [a for a in blind_acts if a["action"] != "posts_ante"]
        blind_acts = antes_in_blind + inferred_blinds + non_antes

    # ── Preflop initial commitments (antes are dead money — exclude them) ─────
    preflop_commitments: dict[str, int] = {}
    for a in blind_acts:
        if a["action"] == "posts_ante":
            continue
        key = a["player"].casefold()
        preflop_commitments[key] = preflop_commitments.get(key, 0) + (a.get("amount") or 0)

    # Multiple straddles double up (UTG 2bb, UTG+1 4bb, …); the facing bet is the
    # highest straddle.
    straddle_amts = [a["amount"] for a in blind_acts if a["action"] == "posts_straddle"]
    preflop_facing = max(straddle_amts) if straddle_amts else bb

    # ── Per-street voluntary action validation ────────────────────────────────
    final_street_commitments: dict[str, int] = {}
    final_current_bet: int = 0

    for street_name in ("preflop", "flop", "turn", "river"):
        if street_name == "preflop":
            commitments = dict(preflop_commitments)
            current_bet = preflop_facing
        else:
            commitments = {}
            current_bet = 0

        vol_acts = [a for a in (action.get(street_name) or []) if a["action"] not in _BLIND_ACTIONS]
        new_vol: list[dict] = []

        for a in vol_acts:
            act_type = a["action"]
            player = a["player"]
            key = player.casefold()
            amount = a.get("amount") or 0
            already_in = commitments.get(key, 0)
            rem = remaining.get(key, 0)

            if act_type in ("folds", "checks"):
                new_vol.append(a)
                continue

            if key in all_in_players:
                continue

            if act_type in ("calls", "calls_allin"):
                if act_type == "calls_allin":
                    # State machine already determined this was an all-in call;
                    # trust its amount but cap at remaining stack.
                    actual = min(a.get("amount") or 0, rem)
                else:
                    call_inc = max(0, current_bet - already_in)
                    actual = min(call_inc, rem)
                if actual <= 0:
                    continue
                new_a = dict(a)
                new_a["amount"] = actual
                remaining[key] = rem - actual
                commitments[key] = already_in + actual
                if remaining[key] == 0 or act_type == "calls_allin":
                    new_a["action"] = "calls_allin"
                    all_in_players.add(key)
                pot += actual
                new_vol.append(new_a)

            elif act_type in ("bets", "bets_allin"):
                actual = min(amount, rem)
                if actual <= 0:
                    new_vol.append(a)
                    continue
                new_a = dict(a)
                new_a["amount"] = actual
                remaining[key] = rem - actual
                commitments[key] = already_in + actual
                current_bet = already_in + actual
                if remaining[key] == 0 or act_type == "bets_allin":
                    new_a["action"] = "bets_allin"
                    all_in_players.add(key)
                pot += actual
                new_vol.append(new_a)

            elif act_type in ("raises", "all_in"):
                # amount = player's new total commitment on this street
                added = max(0, amount - already_in)
                actual_added = min(added, rem)
                if actual_added <= 0:
                    continue
                actual_total = already_in + actual_added
                new_a = dict(a)
                new_a["amount"] = actual_total
                remaining[key] = rem - actual_added
                commitments[key] = actual_total
                current_bet = max(current_bet, actual_total)
                if remaining[key] == 0 or act_type == "all_in":
                    new_a["action"] = "all_in"
                    all_in_players.add(key)
                pot += actual_added
                new_vol.append(new_a)

        if street_name == "preflop":
            processed = blind_acts + new_vol
            action["preflop"] = processed
            raw_action["preflop"] = processed
        else:
            action[street_name] = new_vol
            raw_action[street_name] = new_vol

        if new_vol:
            final_street_commitments = dict(commitments)
            final_current_bet = current_bet

    # ── Uncalled bet detection ────────────────────────────────────────────────
    # If one player committed more than anyone else on the final action street:
    uncalled_player: Optional[str] = None
    uncalled_amount: int = 0

    if final_street_commitments:
        sorted_items = sorted(final_street_commitments.items(), key=lambda kv: kv[1], reverse=True)
        top_key, top_commit = sorted_items[0]
        # A lone committer on the final action street (everyone else folded to a
        # bet/raise) means the whole bet is uncalled — second_commit is 0.
        second_commit = sorted_items[1][1] if len(sorted_items) >= 2 else 0
        if top_commit > second_commit:
            uncalled_amount = top_commit - second_commit
            uncalled_player = name_display.get(top_key, top_key)

    if "pot" not in hand:
        hand["pot"] = {}
    hand["pot"]["total"] = pot - uncalled_amount
    hand["_uncalled"] = (
        {"player": uncalled_player, "amount": uncalled_amount}
        if uncalled_amount > 0 else None
    )
    return hand


# ── Main formatter ────────────────────────────────────────────────────────────

def _secs_to_minsec(s: int) -> str:
    return f"{s // 60}m{s % 60:02d}s"


# ── Side pots (multiway all-ins) ──────────────────────────────────────────────
# hand["pots"] (when present) = [{amount, type:"main"|"side", winners:[names]}],
# index 0 = main pot. Computed client-side from contributions + hand strength.

def _pot_won_amounts(pots: list) -> dict:
    """Total winnings per player across all pots (split shares for ties)."""
    won: dict[str, int] = {}
    for pot in pots:
        winners = pot.get("winners") or []
        if not winners:
            continue
        share = pot["amount"] // len(winners)
        rem = pot["amount"] - share * len(winners)
        for i, w in enumerate(winners):
            won[w] = won.get(w, 0) + share + (rem if i == 0 else 0)
    return won


def _render_pot_collections(pots: list) -> list:
    """`X collected $Y from {main pot|side pot|side pot-N}` — side pots first."""
    out: list = []
    n_side = len(pots) - 1
    for idx in range(len(pots) - 1, -1, -1):
        pot = pots[idx]
        winners = pot.get("winners") or []
        if not winners:
            continue
        if idx == 0:
            label = "main pot" if n_side > 0 else "pot"
        elif n_side == 1:
            label = "side pot"
        else:
            label = f"side pot-{idx}"
        share = pot["amount"] // len(winners)
        rem = pot["amount"] - share * len(winners)
        for i, w in enumerate(winners):
            out.append(f"{w} collected ${share + (rem if i == 0 else 0)} from {label}")
    return out


def _split_pot(total: int, winners: list) -> dict:
    """Split `total` equally among winners; odd chip(s) to the first winner."""
    if not winners:
        return {}
    share = total // len(winners)
    rem = total - share * len(winners)
    return {w: share + (rem if i == 0 else 0) for i, w in enumerate(winners)}


def _pots_summary_line(pots: list) -> str:
    total = sum(p["amount"] for p in pots)
    parts = [f"Total pot ${total}", f"Main pot ${pots[0]['amount']}."]
    sides = pots[1:]
    if len(sides) == 1:
        parts.append(f"Side pot ${sides[0]['amount']}.")
    else:
        for i, p in enumerate(sides, 1):
            parts.append(f"Side pot-{i} ${p['amount']}.")
    return " ".join(parts) + " | Rake $0"


def format_hand(hand: dict, stream_url: str = "", hand_index: int = 0,
                start_sec: int = 0, end_sec: int = 0) -> str:
    hand = _preprocess_hand(hand)

    lines = []

    game = hand.get("game", {})
    stakes = game.get("stakes", "50/100")
    # Stakes may be "sb/bb" or triple-blind "sb/bb/mandatory_straddle" — take the
    # first two; the mandatory straddle is rendered from the action list's posts.
    _p = stakes.split("/")
    sb_amount, bb_amount = int(_p[0]), int(_p[1])

    timestamp_str = hand.get("timestamp_start", "00:00:00")
    hand_id = generate_hand_id(stream_url, timestamp_str, hand_index)
    now = datetime.now()
    dt_str = now.strftime("%Y/%m/%d %H:%M:%S") + " ET"

    # ── Header ────────────────────────────────────────────────────────────────
    lines.append(
        f"PokerStars Hand #{hand_id}:  Hold'em No Limit "
        f"(${sb_amount}/${bb_amount} USD) - {dt_str}"
    )
    # An explicit table_name (e.g. a YouTube link from manual entry) wins; a
    # single quote would break the 'Table '...'' delimiter so it's stripped.
    explicit_name = (hand.get("table_name") or "").strip().replace("'", "")
    if explicit_name:
        table_name = explicit_name
    elif start_sec or end_sec:
        ts = f"HCL_{_secs_to_minsec(start_sec)}-{_secs_to_minsec(end_sec)}"
        table_name = f"{ts} {stream_url[-11:]}" if stream_url else ts
    else:
        table_name = f"Livestream {stream_url[-11:]}" if stream_url else "Livestream"
    button_seat = hand.get("button_seat", 1)
    lines.append(f"Table '{table_name}' 9-max Seat #{button_seat} is the button")

    # ── Extract blind / ante actions for stack adjustment ─────────────────────
    _raw_action = hand.get("action") or {}
    action = {s: (_raw_action.get(s) or []) for s in ("preflop", "flop", "turn", "river")}
    board = hand.get("board") or {}
    preflop_actions = action["preflop"]
    blind_acts = [a for a in preflop_actions if a["action"] in _BLIND_ACTIONS]

    bb_ante_spread = hand.get("bb_ante_spread")
    ante_per_player = (bb_ante_spread or {}).get("ante_per_player", 0)
    bb_ante_total   = (bb_ante_spread or {}).get("bb_ante", 0)
    bb_player_adj   = next((a["player"] for a in blind_acts if a["action"] == "posts_bb"), None)

    # ── Seats ─────────────────────────────────────────────────────────────────
    players = hand.get("players", [])
    blind_poster_names: set[str] = set()   # SB and BB (for "(didn't bet)" logic)
    for a in blind_acts:
        if a["action"] in ("posts_sb", "posts_bb"):
            blind_poster_names.add(a["player"].casefold())

    for i, player in enumerate(players):
        seat = player.get("seat") or (i + 1)
        player["_seat"] = seat
        stack = player.get("stack") or 10000
        if ante_per_player:
            if player["name"] == bb_player_adj:
                stack = max(0, stack - (bb_ante_total - ante_per_player))
            else:
                stack = stack + ante_per_player
        lines.append(f"Seat {seat}: {player['name']} (${stack} in chips)")

    # ── Blind / ante / straddle postings ─────────────────────────────────────
    blind_acts = sorted(blind_acts, key=_blind_sort_key)  # antes→SB→BB→straddle
    if blind_acts:
        for a in blind_acts:
            act, amt = a["action"], a["amount"]
            if act == "posts_ante":
                lines.append(f"{a['player']}: posts the ante ${amt}")
            elif act == "posts_sb":
                lines.append(f"{a['player']}: posts small blind ${amt}")
            elif act == "posts_bb":
                lines.append(f"{a['player']}: posts big blind ${amt}")
            elif act == "posts_straddle":
                lines.append(f"{a['player']}: posts straddle ${amt}")
    else:
        for player in players:
            pos = player.get("position", "")
            if pos == "SB":
                lines.append(f"{player['name']}: posts small blind ${sb_amount}")
            elif pos == "BB":
                lines.append(f"{player['name']}: posts big blind ${bb_amount}")

    # ── Hole cards ────────────────────────────────────────────────────────────
    lines.append("*** HOLE CARDS ***")
    for player in players:
        cards = player.get("hole_cards")
        if cards:
            lines.append(f"Dealt to {player['name']} {format_cards(cards)}")

    # ── Preflop voluntary actions ─────────────────────────────────────────────
    preflop_commitments: dict[str, int] = {}
    for a in blind_acts:
        if a["action"] == "posts_ante":
            continue
        pkey = a["player"].casefold()
        preflop_commitments[pkey] = preflop_commitments.get(pkey, 0) + a["amount"]

    straddle_amts = [a["amount"] for a in blind_acts if a["action"] == "posts_straddle"]
    facing_bet_preflop = max(straddle_amts) if straddle_amts else bb_amount

    non_blind_preflop = [a for a in preflop_actions if a["action"] not in _BLIND_ACTIONS]
    lines.extend(_format_street_actions(
        non_blind_preflop,
        facing_bet=facing_bet_preflop,
        initial_commitments=preflop_commitments,
    ))

    # ── Detect run-it-multiple-times (1–4 runs) ───────────────────────────────
    # New shape: board["runs"] = [{flop?,turn?,river?}, …] (post-all-in streets).
    # Back-compat: first_run/second_run map to a 2-run list.
    runs = board.get("runs")
    if not runs:
        fr, sr = board.get("first_run") or {}, board.get("second_run") or {}
        if fr and sr:
            runs = [fr, sr]
    is_rit = bool(runs and len(runs) >= 2)  # name kept; gates "no side pots" etc.
    num_runs = len(runs) if is_rit else 1
    ORDINALS = ["FIRST", "SECOND", "THIRD", "FOURTH", "FIFTH"]

    # ── Regular board (the streets dealt before the all-in, shown once) ──────
    flop_cards = board.get("flop") or []
    turn_card  = board.get("turn")
    river_card = board.get("river")

    if flop_cards:
        lines.append(f"*** FLOP *** {format_cards(flop_cards)}")
        lines.extend(_format_street_actions(action["flop"], facing_bet=0))

    if turn_card:
        lines.append(f"*** TURN *** {format_cards(flop_cards)} [{turn_card}]")
        lines.extend(_format_street_actions(action["turn"], facing_bet=0))

    if not is_rit and river_card:
        board_to_turn = flop_cards + ([turn_card] if turn_card else [])
        lines.append(f"*** RIVER *** {format_cards(board_to_turn)} [{river_card}]")
        lines.extend(_format_street_actions(action["river"], facing_bet=0))

    # ── Run-it-multiple-times boards (FIRST/SECOND/THIRD/FOURTH … markers) ────
    full_boards: list[list] = []
    if is_rit:
        shared = list(flop_cards) + ([turn_card] if turn_card else [])
        for i, run in enumerate(runs):
            ordn = ORDINALS[i] if i < len(ORDINALS) else f"RUN{i + 1}"
            r_flop = run.get("flop") or []
            r_turn = run.get("turn")
            r_river = run.get("river")
            cum = list(shared)
            if r_flop:
                lines.append(f"*** {ordn} FLOP *** {format_cards(r_flop)}")
                cum = list(r_flop)
            if r_turn:
                lines.append(f"*** {ordn} TURN *** {format_cards(cum)} [{r_turn}]")
                cum = cum + [r_turn]
            if r_river:
                lines.append(f"*** {ordn} RIVER *** {format_cards(cum)} [{r_river}]")
                cum = cum + [r_river]
            full_boards.append(cum)

    # ── Determine winner(s) (needed for validation) ───────────────────────────
    # Fix 2: deduplicate showdown by player name before any rendering
    _sd_raw = hand.get("showdown") or []
    if is_rit:
        # Multi-run: each player legitimately appears once per run — keep them all.
        showdown = list(_sd_raw)
    else:
        _sd_seen: dict[str, dict] = {}
        for _sd in _sd_raw:
            _key = (_sd.get("player") or "").casefold()
            if _key not in _sd_seen or _sd.get("result") == "wins":
                _sd_seen[_key] = _sd
        showdown = list(_sd_seen.values())
    pot_total = hand.get("pot", {}).get("total", 0)
    uncalled  = hand.get("_uncalled")
    # Explicit side pots (multiway all-in) — non-RIT only.
    pots = hand.get("pots") if not is_rit else None
    # Chopped pot: 2+ showdown players marked "wins" split the single pot.
    sd_winners = [sd["player"] for sd in showdown if sd.get("result") == "wins"]
    chop = (not is_rit) and (not pots) and len(sd_winners) >= 2
    if pots:
        won_amounts = _pot_won_amounts(pots)
    elif chop:
        won_amounts = _split_pot(pot_total, sd_winners)
    else:
        won_amounts = None
    winner_name: Optional[str] = None

    if is_rit:
        winner_name = next((sd["player"] for sd in showdown if sd.get("result") == "wins"), None)
    elif showdown:
        winner_name = next((sd["player"] for sd in showdown if sd.get("result") == "wins"), None)
    else:
        winner_name = _find_winner_no_showdown(action) or hand.get("winner")

    # ── Structural validation ─────────────────────────────────────────────────
    _validate_hand(hand, action, winner_name, showdown, is_rit=is_rit)

    # ── Uncalled bet (always before any SHOW DOWN line) ───────────────────────
    if uncalled and uncalled["amount"] > 0:
        lines.append(
            f"Uncalled bet (${uncalled['amount']}) returned to {uncalled['player']}"
        )

    # ── Showdown / winner ─────────────────────────────────────────────────────
    if is_rit:
        share = pot_total // num_runs
        rem = pot_total - share * num_runs
        tagged = any(sd.get("run") for sd in showdown)
        run_winners_list = []
        for i in range(num_runs):
            if tagged:
                run_sd = [sd for sd in showdown if sd.get("run") == i + 1]
            elif showdown:
                per = max(1, len(showdown) // num_runs)
                run_sd = showdown[i * per:(i + 1) * per] if i < num_runs - 1 else showdown[i * per:]
            else:
                run_sd = []
            ordn = ORDINALS[i] if i < len(ORDINALS) else f"RUN{i + 1}"
            rw = next((sd["player"] for sd in run_sd if sd.get("result") == "wins"), None)
            run_winners_list.append(rw)
            if run_sd:
                lines.append(f"*** {ordn} SHOW DOWN ***")
                for sd in run_sd:
                    cards = sd.get("hole_cards", [])
                    if cards:
                        lines.append(
                            f"{sd['player']}: shows {format_cards(cards)} "
                            f"({sd.get('hand_description', '')})"
                        )
                if rw:
                    amt = share + (rem if i == 0 else 0)
                    lines.append(f"{rw} collected ${amt} from pot")

        winner_name = next((w for w in run_winners_list if w), None)  # per-seat summary

    elif showdown:
        lines.append("*** SHOW DOWN ***")
        for sd in showdown:
            cards = sd.get("hole_cards", [])
            if cards:
                desc = sd.get("hand_description", "")
                lines.append(f"{sd['player']}: shows {format_cards(cards)} ({desc})")
        if pots:
            lines.extend(_render_pot_collections(pots))
        elif chop:
            for w in sd_winners:
                lines.append(f"{w} collected ${won_amounts[w]} from pot")
        elif winner_name:
            lines.append(f"{winner_name} collected ${pot_total} from pot")
    else:
        if winner_name:
            lines.append(f"{winner_name} collected ${pot_total} from pot")

    # ── Summary ───────────────────────────────────────────────────────────────
    lines.append("*** SUMMARY ***")
    if pots:
        lines.append(_pots_summary_line(pots))
    else:
        lines.append(f"Total pot ${pot_total} | Rake $0")

    if is_rit:
        _times = {2: "twice", 3: "three times", 4: "four times"}.get(num_runs, f"{num_runs} times")
        lines.append(f"Hand was run {_times}")
        for i, fb in enumerate(full_boards):
            if fb:
                ordn = ORDINALS[i] if i < len(ORDINALS) else f"RUN{i + 1}"
                lines.append(f"{ordn} Board {format_cards(fb)}")
    else:
        full_board = list(flop_cards) if flop_cards else []
        if turn_card:
            full_board.append(turn_card)
        if river_card:
            full_board.append(river_card)
        if full_board:
            lines.append(f"Board {format_cards(full_board)}")

    # Players who made at least one voluntary in-game action.
    # SB/BB blind posts do NOT count — only calls, bets, raises, all-ins.
    players_who_bet: set[str] = set()
    for street_acts in action.values():
        for a in street_acts:
            if a["action"] in _VOLUNTARY_ACTIONS:
                players_who_bet.add(a["player"].casefold())

    # Pre-compute RIT per-player winnings for summary (pot split across N runs,
    # odd chips to the first run).
    rit_winnings: dict[str, int] = {}
    if is_rit:
        share = pot_total // num_runs
        rem = pot_total - share * num_runs
        for sd in showdown:
            if sd.get("result") == "wins":
                run = sd.get("run", 1)
                amt = share + (rem if run == 1 else 0)
                key = sd["player"].casefold()
                rit_winnings[key] = rit_winnings.get(key, 0) + amt

    # Summary blind/button labels are derived from the ACTUAL blind postings (and
    # button_seat) — not the per-player position field — so they can never
    # disagree with the "posts small/big blind" lines in the hand body.
    sb_poster = next((a["player"] for a in blind_acts if a["action"] == "posts_sb"), None)
    bb_poster = next((a["player"] for a in blind_acts if a["action"] == "posts_bb"), None)
    sb_key = sb_poster.casefold() if sb_poster else None
    bb_key = bb_poster.casefold() if bb_poster else None
    buy_btn_seat = hand.get("buy_button_seat")  # buyer is the button, posts the lone live blind

    for player in players:
        seat = player["_seat"]
        name = player["name"]
        name_key = name.casefold()
        if buy_btn_seat is not None and seat == buy_btn_seat:
            pos_label = "button"
        elif name_key == bb_key:
            pos_label = "big blind"
        elif name_key == sb_key:
            pos_label = "small blind"   # in heads-up the button posts the SB
        elif seat == button_seat:
            pos_label = "button"
        else:
            pos_label = ""
        pos_str = f" ({pos_label})" if pos_label else ""

        # For RIT: use the first showdown entry for hole cards/description
        sd_entry = next((s for s in showdown if s["player"] == name), None)

        if is_rit and sd_entry:
            cards = sd_entry.get("hole_cards", [])
            desc  = sd_entry.get("hand_description", "")
            desc_sfx = f" with {desc}" if desc else ""
            won   = rit_winnings.get(name.casefold(), 0)
            if won:
                lines.append(
                    f"Seat {seat}: {name}{pos_str} showed {format_cards(cards)} "
                    f"and won (${won}){desc_sfx}"
                )
            else:
                lines.append(
                    f"Seat {seat}: {name}{pos_str} showed {format_cards(cards)} "
                    f"and lost{desc_sfx}"
                )
        elif sd_entry and won_amounts is not None:
            # Side-pot hand: winnings come from the per-pot split.
            cards = sd_entry.get("hole_cards", [])
            desc  = sd_entry.get("hand_description", "")
            desc_sfx = f" with {desc}" if desc else ""
            amt = won_amounts.get(name, 0)
            if amt > 0:
                lines.append(
                    f"Seat {seat}: {name}{pos_str} showed {format_cards(cards)} "
                    f"and won (${amt}){desc_sfx}"
                )
            elif cards:
                lines.append(
                    f"Seat {seat}: {name}{pos_str} showed {format_cards(cards)} "
                    f"and lost{desc_sfx}"
                )
        elif sd_entry:
            cards = sd_entry.get("hole_cards", [])
            desc  = sd_entry.get("hand_description", "")
            desc_sfx = f" with {desc}" if desc else ""
            if sd_entry.get("result") == "wins" or name == winner_name:
                if not cards:
                    lines.append(f"Seat {seat}: {name}{pos_str} collected ${pot_total} from pot")
                else:
                    lines.append(
                        f"Seat {seat}: {name}{pos_str} showed {format_cards(cards)} "
                        f"and won (${pot_total}){desc_sfx}"
                    )
            else:
                if cards:
                    lines.append(
                        f"Seat {seat}: {name}{pos_str} showed {format_cards(cards)} "
                        f"and lost{desc_sfx}"
                    )
        elif name == winner_name and not showdown:
            lines.append(f"Seat {seat}: {name}{pos_str} collected ${pot_total} from pot")
        elif _player_folded(name, action):
            fold_street = _get_fold_street(name, action)
            if fold_street == "preflop":
                name_key = name.casefold()
                did_not_bet = name_key not in players_who_bet
                is_blind = name_key in blind_poster_names
                suffix = "" if (not did_not_bet or is_blind) else " (didn't bet)"
                lines.append(f"Seat {seat}: {name}{pos_str} folded before Flop{suffix}")
            else:
                lines.append(
                    f"Seat {seat}: {name}{pos_str} folded on the {fold_street.capitalize()}"
                )
        elif name.casefold() in players_who_bet:
            # Active but no outcome — validation passed, so this shouldn't happen.
            # Emit a safe fallback rather than the wrong "folded before Flop" text.
            lines.append(f"Seat {seat}: {name}{pos_str} mucked hand")
        else:
            # Player posted antes/blinds but never made a voluntary action
            lines.append(f"Seat {seat}: {name}{pos_str} folded before Flop (didn't bet)")

    for player in players:
        player.pop("_seat", None)

    lines.append("")
    lines.append("")
    return "\n".join(lines)


def _validate_hand(
    hand: dict,
    action: dict,
    winner_name: Optional[str],
    showdown: list,
    is_rit: bool = False,
) -> None:
    """Raise HandValidationError describing the specific structural problem."""
    players = hand.get("players", [])

    # 1. Must have a winner (RIT hands allow multiple winners across runs)
    has_winner = bool(winner_name) or any(sd.get("result") == "wins" for sd in showdown)
    if not has_winner:
        raise HandValidationError("No winner — hand cannot be output")

    # 2. BB and straddle cannot be posted by the same player
    all_blind = [a for a in (action.get("preflop") or []) if a.get("action") in _BLIND_ACTIONS]
    bb_poster  = next((a["player"] for a in all_blind if a["action"] == "posts_bb"), None)
    str_poster = next((a["player"] for a in all_blind if a["action"] == "posts_straddle"), None)
    if bb_poster and str_poster and bb_poster.casefold() == str_poster.casefold():
        raise HandValidationError(
            f"{bb_poster!r} posted both BB and straddle — straddle assignment is wrong"
        )

    # 3. Every player who made voluntary actions must have a recorded outcome
    for player in players:
        name = player.get("name", "")
        if not _player_was_active(name, action):
            continue
        if _player_folded(name, action):
            continue
        if any(sd.get("player") == name for sd in showdown):
            continue
        if name == winner_name:
            continue
        # Player made voluntary actions but no recorded fold/showdown — likely a
        # missed fold.  The formatter has a "mucked hand" fallback for this case.
        print(f"  [warn formatter] {name!r} acted voluntarily but no recorded outcome — will show as 'mucked'")


def _format_street_actions(
    acts: list[dict],
    facing_bet: int,
    initial_commitments: dict | None = None,
) -> list[str]:
    """Format one street's voluntary actions with correct call/raise amounts."""
    lines = []
    current_bet = facing_bet
    commitments: dict[str, int] = dict(initial_commitments or {})

    for act in acts:
        player = act["player"]
        action = act["action"]
        amount = act.get("amount") or 0
        pkey   = player.casefold()
        already_in = commitments.get(pkey, 0)

        if action == "folds":
            lines.append(f"{player}: folds")
        elif action == "checks":
            lines.append(f"{player}: checks")
        elif action == "calls":
            call_inc = max(0, current_bet - already_in)
            if call_inc > 0:
                lines.append(f"{player}: calls ${call_inc}")
            commitments[pkey] = already_in + call_inc
        elif action == "calls_allin":
            # Amount was pre-validated and capped by _preprocess_hand
            call_inc = amount
            if call_inc > 0:
                lines.append(f"{player}: calls ${call_inc} and is all-in")
            commitments[pkey] = already_in + call_inc
        elif action == "bets":
            lines.append(f"{player}: bets ${amount}")
            current_bet = already_in + amount
            commitments[pkey] = already_in + amount
        elif action == "bets_allin":
            lines.append(f"{player}: bets ${amount} and is all-in")
            current_bet = already_in + amount
            commitments[pkey] = already_in + amount
        elif action in ("raises", "all_in"):
            suffix = " and is all-in" if action == "all_in" else ""
            if current_bet == 0:
                # No prior bet on this street — it's a bet, not a raise (R27)
                lines.append(f"{player}: bets ${amount}{suffix}")
            else:
                increment = max(1, amount - current_bet)
                lines.append(f"{player}: raises ${increment} to ${amount}{suffix}")
            current_bet = amount
            commitments[pkey] = amount
        else:
            lines.append(f"{player}: {action}" + (f" ${amount}" if amount else ""))

    return lines


def _find_winner_no_showdown(action: dict) -> Optional[str]:
    all_actions = []
    for street in ["preflop", "flop", "turn", "river"]:
        all_actions.extend(action.get(street) or [])
    folders = {a["player"] for a in all_actions if a["action"] == "folds"}
    all_players = {a["player"] for a in all_actions if a["action"] not in _BLIND_ACTIONS}
    non_folders = all_players - folders
    if len(non_folders) == 1:
        return non_folders.pop()
    return None


def _player_was_active(name: str, action: dict) -> bool:
    for street in ["preflop", "flop", "turn", "river"]:
        for act in (action.get(street) or []):
            if act["player"] == name and act["action"] not in _BLIND_ACTIONS:
                return True
    return False


def _player_folded(name: str, action: dict) -> bool:
    for street in ["preflop", "flop", "turn", "river"]:
        for act in (action.get(street) or []):
            if act["player"] == name and act["action"] == "folds":
                return True
    return False


def _get_fold_street(name: str, action: dict) -> str:
    for street in ["preflop", "flop", "turn", "river"]:
        for act in (action.get(street) or []):
            if act["player"] == name and act["action"] == "folds":
                return street
    return "preflop"


def format_session(hands: list[dict], stream_url: str = "") -> str:
    formatted = []
    for i, hand in enumerate(hands):
        if not hand.get("players"):
            continue
        formatted.append(format_hand(hand, stream_url, i))
    return "\n".join(formatted)
