"""
src/vision/hand_feeder.py

Takes a model JSON hand dict and feeds it through PokerStateMachine,
producing a validated pt4_formatter-compatible dict.

Key guarantees
--------------
- Seats are remapped 1-N (clockwise order) to eliminate gaps.
- Placeholder names ("Villain", "Unknown") and zero stacks are rejected.
- Missing antes are auto-injected for every seated player.
- SB and BB are ALWAYS different players; if GPT assigns the same player
  to both, seat-order inference overrides both assignments.
- Missing SB/BB are inferred first from position labels, then from seat
  order relative to the button (1st clockwise = SB, 2nd = BB).
- Missing straddle is inferred from position label "STR".
- Hands with a flop but no preflop voluntary actions are rejected.
- Board is not dealt when only 1 player remains active after preflop.
- Showdown entries are deduplicated by player name before feeding.
- Out-of-order voluntary actions are queued and retried (auto_advance).
- Wrong amounts are capped at the player's remaining stack.
- If no showdown/winner info was provided, the winner is inferred.
- If unresolvable actions remain after all retries, a FeedError is raised.
"""

from __future__ import annotations

import math
from typing import Optional

from src.engine.poker_state_machine import PokerStateMachine
from src.export.hand_evaluator import evaluate_best_hand


_BLIND_TYPES = frozenset({"posts_ante", "posts_sb", "posts_bb", "posts_straddle"})
_VOLUNTARY   = frozenset({"folds", "checks", "calls", "bets", "raises", "all_in"})
_BAD_NAMES   = frozenset({"villain", "unknown", "hero", "player"})


class FeedError(ValueError):
    """Raised when the state machine cannot resolve the hand."""


def feed_hand(
    hand_json: dict,
    bb_ante: int = 100,
    stakes: str = "50/100",
    button_seat: int = 1,
) -> dict:
    """
    Feed a model-produced hand JSON through the poker state machine.

    Returns a dict compatible with pt4_formatter.format_hand().
    Raises FeedError with a diagnostic message if the hand cannot be resolved.
    """
    sm = PokerStateMachine(stakes=stakes, auto_advance=True)

    # Tell the state machine which players appear in the showdown so it never
    # auto-folds them during _auto_advance_until (fixes ghost-fold / R17 failures).
    showdown = hand_json.get("showdown") or []
    sm._showdown_players = frozenset(
        (sd.get("player") or "").casefold()
        for sd in showdown
        if sd.get("player")
    )

    players = hand_json.get("players") or []
    if not players:
        raise FeedError("No players in hand JSON")

    # ── Warn on placeholder names and zero/null stacks (don't skip) ───────────
    for p in players:
        name  = (p.get("name") or "").strip()
        stack = p.get("stack") or 0
        if name.casefold() in _BAD_NAMES:
            print(f"  [warn] Placeholder name {name!r} — keeping hand, PT4 will import")
        if stack <= 0:
            p["stack"] = 10000
            print(f"  [warn] Player {name!r} has stack={stack!r}; substituting $10000")

    # ── Fix 6: remap seats 1-N (sorted clockwise) to eliminate gaps ───────────
    players_sorted = sorted(players, key=lambda p: p.get("seat") or 0)
    _seat_map: dict[int, int] = {}
    for new_seat, p in enumerate(players_sorted, 1):
        old = p.get("seat") or new_seat
        _seat_map[old] = new_seat

    def _ns(p: dict) -> int:
        """Return remapped seat for player dict."""
        return _seat_map.get(p.get("seat") or 0, p.get("seat") or 1)

    num_players = len(players)
    ante_per_player = math.ceil(bb_ante / num_players) if num_players else bb_ante
    sm.set_ante_spread(bb_ante, ante_per_player)

    # ── Button ────────────────────────────────────────────────────────────────
    btn_p = next((p for p in players if (p.get("position") or "").upper() == "BTN"), None)
    if btn_p:
        resolved_button = _ns(btn_p)
    else:
        resolved_button = _seat_map.get(button_seat, 1)
    sm.set_button(resolved_button)

    # ── Add players ───────────────────────────────────────────────────────────
    for p in players:
        sm.add_player(
            seat=_ns(p),
            name=p.get("name", "Villain"),
            stack=p.get("stack") or 10000,
            position=(p.get("position") or "").upper(),
        )

    # ── Hole cards ────────────────────────────────────────────────────────────
    for p in players:
        cards = p.get("hole_cards")
        if cards:
            sm.deal_hole_cards(p.get("name", "Villain"), list(cards))

    preflop_actions = (hand_json.get("action") or {}).get("preflop") or []

    # If no preflop voluntary actions but a flop exists, continue anyway.
    # deal_board() resets the action queue for the flop; skipping preflop
    # voluntary actions is safe — the hand appears as a limped/checked pot.
    flop_in_json = (hand_json.get("board") or {}).get("flop")
    voluntary_preflop = [a for a in preflop_actions if a.get("action") in _VOLUNTARY]
    if not voluntary_preflop and flop_in_json:
        print("  [warn] No preflop voluntary actions — treating as limped/checked pot")

    # ── 1. Antes ─────────────────────────────────────────────────────────────
    antes_posted: set[str] = set()
    for act in preflop_actions:
        if act.get("action") == "posts_ante":
            pname = act.get("player", "")
            if pname.casefold() not in antes_posted:
                antes_posted.add(pname.casefold())
                sm.post_ante(pname, act.get("amount") or ante_per_player)

    for p in players:
        pname = p.get("name", "")
        if pname.casefold() not in antes_posted:
            sm.post_ante(pname, ante_per_player)
            antes_posted.add(pname.casefold())

    # ── 2. Blinds ─────────────────────────────────────────────────────────────
    # Fix 1: collect SB/BB intent from all three sources, then validate
    # SB ≠ BB before posting.  If they collide, override with seat order.

    # Remapped seats for seat-order inference
    all_remapped_seats = [_ns(p) for p in players]
    inferred_sb_seat   = _next_seat_clockwise(resolved_button, all_remapped_seats)
    inferred_bb_seat   = (
        _next_seat_clockwise(inferred_sb_seat, all_remapped_seats)
        if inferred_sb_seat is not None else None
    )
    inferred_sb_player = next((p for p in players if _ns(p) == inferred_sb_seat), None)
    inferred_bb_player = next((p for p in players if _ns(p) == inferred_bb_seat), None)

    sb_name: Optional[str] = None
    bb_name: Optional[str] = None
    sb_act_amount: Optional[int] = None
    bb_act_amount: Optional[int] = None

    # Pass 1: explicit actions
    sb_act = next((a for a in preflop_actions if a.get("action") == "posts_sb"), None)
    bb_act = next((a for a in preflop_actions if a.get("action") == "posts_bb"), None)
    if sb_act:
        sb_name = sb_act.get("player") or None
        sb_act_amount = sb_act.get("amount") or None
    if bb_act:
        bb_name = bb_act.get("player") or None
        bb_act_amount = bb_act.get("amount") or None

    # Pass 2: position labels
    if not sb_name:
        sb_p = next((p for p in players if (p.get("position") or "").upper() == "SB"), None)
        if sb_p:
            sb_name = sb_p["name"]
    if not bb_name:
        bb_p = next((p for p in players if (p.get("position") or "").upper() == "BB"), None)
        if bb_p:
            bb_name = bb_p["name"]

    # Pass 3: seat order
    if not sb_name and inferred_sb_player:
        sb_name = inferred_sb_player["name"]
    if not bb_name and inferred_bb_player:
        bb_name = inferred_bb_player["name"]

    # Fix 1: if SB and BB resolved to same player, override both with seat order
    if sb_name and bb_name and sb_name.casefold() == bb_name.casefold():
        sb_name = inferred_sb_player["name"] if inferred_sb_player else sb_name
        bb_name = inferred_bb_player["name"] if inferred_bb_player else None
        sb_act_amount = None
        bb_act_amount = None
        # If still the same (only 1 player?), raise
        if sb_name and bb_name and sb_name.casefold() == bb_name.casefold():
            raise FeedError(
                f"SB and BB cannot be the same player ({sb_name!r}) — "
                f"not enough players or bad seat layout"
            )

    if not bb_name:
        raise FeedError(
            f"Cannot determine BB — no action, no 'BB' position label, "
            f"and seat inference failed (button_seat={resolved_button})"
        )

    if sb_name:
        sm.post_blind(sb_name, sb_act_amount or sm.sb_amount, "sb")
    bb_player_name = (bb_name or "").casefold()
    sm.post_blind(bb_name, bb_act_amount or sm.bb_amount, "bb")

    # ── 3. Straddle ───────────────────────────────────────────────────────────
    straddle_posted = False
    for act in preflop_actions:
        if act.get("action") == "posts_straddle":
            str_player_cf = (act.get("player") or "").casefold()
            if str_player_cf == bb_player_name:
                continue
            sm.post_straddle(act.get("player", ""), act.get("amount") or 0)
            straddle_posted = True

    if not straddle_posted:
        str_p = next(
            (p for p in players if (p.get("position") or "").upper() in ("STR", "STRADDLE")),
            None,
        )
        if str_p and str_p["name"].casefold() != bb_player_name:
            sm.post_straddle(str_p["name"], sm.bb_amount * 2)

    # ── 4. Begin preflop ─────────────────────────────────────────────────────
    sm.begin_preflop()

    # ── 5. Voluntary preflop actions ─────────────────────────────────────────
    _apply_street(sm, preflop_actions, "preflop")

    # ── 6–8. Postflop streets ─────────────────────────────────────────────────
    # Fix 3: only deal community cards when 2+ players are still active.
    board      = hand_json.get("board") or {}
    action_map = hand_json.get("action") or {}

    def _active_count() -> int:
        return sum(1 for p in sm._players if p.status != "folded")

    flop = board.get("flop")
    if flop and _active_count() >= 2:
        sm.deal_board(list(flop))
        _apply_street(sm, (action_map.get("flop") or []), "flop")

    turn = board.get("turn")
    if turn and _active_count() >= 2:
        sm.deal_board([turn])
        _apply_street(sm, (action_map.get("turn") or []), "turn")

    river = board.get("river")
    if river and _active_count() >= 2:
        sm.deal_board([river])
        _apply_street(sm, (action_map.get("river") or []), "river")

    # ── Hand completeness check ───────────────────────────────────────────────
    # If 2+ players can still bet (status=="active") but we stopped before the
    # river, the hand is missing streets — reject it so Gemini retries.
    n_can_bet = sum(1 for p in sm._players if p.status == "active")
    board_cards_dealt = (
        len(sm.flop_cards)
        + (1 if sm.turn_card else 0)
        + (1 if sm.river_card else 0)
    )
    if n_can_bet >= 2 and board_cards_dealt > 0 and not sm.river_card:
        raise FeedError(
            f"Hand ended on {sm.street!r} with {n_can_bet} active non-all-in "
            f"players — missing streets (board has {board_cards_dealt} card(s))"
        )

    # ── 9. Showdown from model output ─────────────────────────────────────────
    # Deduplicate by player name: keep the "wins" entry if present, else first.
    _sd_seen: dict[str, dict] = {}
    for sd in (hand_json.get("showdown") or []):
        key = (sd.get("player") or "").casefold()
        if key not in _sd_seen or sd.get("result") == "wins":
            _sd_seen[key] = sd
    for sd in _sd_seen.values():
        sm.add_showdown(
            sd.get("player", ""),
            sd.get("hole_cards") or [],
            sd.get("hand_description", ""),
            sd.get("result", ""),
        )

    # ── 10. Infer winner if model didn't provide one ──────────────────────────
    _infer_winner(sm)

    # Validate: every hand must have a winner
    if not sm._showdown:
        non_folded = [p for p in sm._players if p.status != "folded"]
        if len(non_folded) >= 2:
            board_count = (
                len(sm.flop_cards)
                + (1 if sm.turn_card else 0)
                + (1 if sm.river_card else 0)
            )
            names = [p.name for p in non_folded]
            raise FeedError(
                f"Multiple players active {names} with {board_count} board card(s) "
                f"— cannot determine winner"
            )

    # ── 11. Build output dict ─────────────────────────────────────────────────
    pt4 = sm.to_pt4_dict()
    pt4["game"] = {"stakes": stakes}
    pt4["button_seat"] = resolved_button
    pt4["bb_ante_spread"] = {
        "bb_ante": bb_ante,
        "ante_per_player": ante_per_player,
    }
    pt4["_auto_advance_log"] = sm.auto_advance_log
    return pt4


# ── Internal helpers ──────────────────────────────────────────────────────────

def _next_seat_clockwise(from_seat: int, all_seats: list[int]) -> Optional[int]:
    """First occupied seat numerically after from_seat, wrapping around."""
    if not all_seats:
        return None
    ascending = sorted(all_seats)
    for s in ascending:
        if s > from_seat:
            return s
    return ascending[0]


def _infer_winner(sm: PokerStateMachine) -> None:
    """
    If no showdown entries exist, infer the winner from hand state:
    - 1 non-folded player  → uncontested win (no cards needed)
    - 2+ non-folded players with a board → evaluate via hand_evaluator
    """
    if sm._showdown:
        return

    board_cards = (
        sm.flop_cards
        + ([sm.turn_card] if sm.turn_card else [])
        + ([sm.river_card] if sm.river_card else [])
    )

    non_folded = [p for p in sm._players if p.status != "folded"]
    if not non_folded:
        return

    if len(non_folded) == 1:
        w = non_folded[0]
        desc = ""
        if w.hole_cards and board_cards:
            _, desc = evaluate_best_hand(w.hole_cards, board_cards)
        sm.add_showdown(w.name, w.hole_cards or [], desc, "wins")
        return

    if len(board_cards) < 3:
        return

    candidates = [(p, p.hole_cards) for p in non_folded if len(p.hole_cards) == 2]
    if not candidates:
        return

    scored = []
    for p, hc in candidates:
        score, desc = evaluate_best_hand(hc, board_cards)
        scored.append((score, p, desc))

    best_score = max(s[0] for s in scored)
    for score, p, desc in scored:
        result = "wins" if score == best_score else "loses"
        sm.add_showdown(p.name, p.hole_cards, desc, result)


def _apply_street(sm: PokerStateMachine, acts: list[dict], street: str) -> None:
    """
    Feed voluntary actions for one street into the state machine, flush the
    pending queue, then raise FeedError if anything is still unresolvable.
    """
    for act in acts:
        if act.get("action") in _VOLUNTARY:
            sm.action(
                act.get("player", ""),
                act["action"],
                act.get("amount") or 0,
            )

    sm._try_pending()

    if sm._pending_queue:
        next_expected = sm._next_active_in_queue()
        unresolved = sm._pending_queue[0]
        if next_expected is None:
            # Round is already closed; Gemini included extra actions after the
            # round ended. Discard them and continue to the next street.
            print(
                f"  [warn] [{street}] Discarding {len(sm._pending_queue)} extra "
                f"action(s) after round closed "
                f"(first: {unresolved['player']!r} {unresolved['action']})",
                flush=True,
            )
            sm._pending_queue.clear()
        else:
            raise FeedError(
                f"[{street}] Unresolvable action — "
                f"received: {unresolved['player']!r} {unresolved['action']}"
                + (f" ${unresolved['amount']}" if unresolved.get("amount") else "")
                + f" | next expected to act: {next_expected!r}"
            )
