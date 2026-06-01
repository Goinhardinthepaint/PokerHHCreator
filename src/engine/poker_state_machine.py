"""
src/engine/poker_state_machine.py

Deterministic poker hand state machine.

Tracks seats, stacks, positions, blind/ante/straddle postings,
board cards, per-street betting rounds (including action ordering
and re-open logic), and produces a pt4_formatter-compatible dict.

Key invariants
--------------
- `player.stack` is the remaining chips after every applied action.
- `player.initial_stack` is the overlay stack passed to add_player();
  this is what pt4_formatter.format_hand() expects in the "players" array.
- action() queues out-of-turn actions and retries them after each
  successful in-turn action, so the caller can feed actions in any order.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

_BLIND_ACTIONS = frozenset({"posts_ante", "posts_sb", "posts_bb", "posts_straddle"})
_STREETS = ("preflop", "flop", "turn", "river")


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class Player:
    seat: int
    name: str
    initial_stack: int       # stack value as given to add_player() (overlay value)
    stack: int               # chips remaining after every applied action
    position: str = ""       # BTN / SB / BB / UTG / HJ / CO / STR / ""
    status: str = "active"   # active / folded / all_in
    hole_cards: list[str] = field(default_factory=list)
    street_commitment: int = 0   # chips in on the current street (excl. antes)
    total_invested: int = 0      # chips in across all streets (for validation)


# ── State machine ──────────────────────────────────────────────────────────────

class PokerStateMachine:
    """
    Deterministic poker hand state machine.

    Typical usage
    -------------
    sm = PokerStateMachine("50/100")
    sm.add_player(1, "Greedo", 19000, "BTN")
    ...
    sm.set_button(1)
    sm.post_ante("Greedo", 17)
    ...
    sm.post_blind("Francisco", 50, "sb")
    sm.post_blind("Dylan", 100, "bb")
    sm.post_straddle("Airball", 200)   # optional
    sm.deal_hole_cards("Greedo", ["4s","4h"])
    sm.begin_preflop()
    sm.action("Greedo", "raises", 600)
    ...
    sm.deal_board(["Kd","9s","6s"])    # flop → advances to flop street
    sm.action("Otto", "checks")
    ...
    result = sm.to_pt4_dict()
    """

    def __init__(self, stakes: str = "50/100", auto_advance: bool = False):
        self.stakes = stakes
        sb_str, bb_str = stakes.split("/")
        self.sb_amount = int(sb_str)
        self.bb_amount = int(bb_str)

        self._players: list[Player] = []   # sorted by seat number
        self.button_seat: int = 1
        self.street: str = "preflop"

        # Board
        self.flop_cards: list[str] = []
        self.turn_card: Optional[str] = None
        self.river_card: Optional[str] = None

        # Pot
        self.pot: int = 0

        # Ante spread info (optional — for to_pt4_dict())
        self.bb_ante: int = 0
        self.ante_per_player: int = 0

        # Straddle
        self.straddle_player: Optional[str] = None
        self.straddle_amount: int = 0

        # BB poster seat — used as preflop pivot when no player has position="BB"
        self._bb_poster_seat: Optional[int] = None

        # Guard against same player acting twice in a row (OCR noise)
        self._last_actor: Optional[str] = None

        # True after all active players have matched the current bet on this street;
        # reset to False when deal_board() advances to the next street.
        self._round_closed: bool = False

        # Current betting round
        self.current_bet: int = 0
        self._players_to_act: deque[str] = deque()
        self._pending_queue: list[dict] = []

        self._hand_complete: bool = False
        self._in_pending_flush: bool = False  # recursion guard for _try_pending
        self._in_auto_advance: bool = False   # recursion guard for _auto_advance_until

        # Players known to appear at showdown — never auto-folded by auto_advance
        self._showdown_players: frozenset[str] = frozenset()

        # Auto-advance: when enabled, players absent from the model's output
        # are automatically checked (if no bet) or folded (if facing a bet).
        self.auto_advance: bool = auto_advance
        self._street_mentioned: set[str] = set()  # players seen in action() this street
        self.auto_advance_log: list[str] = []      # human-readable record of every inference

        # Logged actions per street (for to_pt4_dict / formatter)
        self._action_log: dict[str, list[dict]] = {
            "preflop": [], "flop": [], "turn": [], "river": []
        }
        self._showdown: list[dict] = []

    # ── Setup helpers ─────────────────────────────────────────────────────────

    def add_player(self, seat: int, name: str, stack: int, position: str = "") -> None:
        p = Player(
            seat=seat,
            name=name,
            initial_stack=stack,
            stack=stack,
            position=position,
        )
        self._players.append(p)
        self._players.sort(key=lambda x: x.seat)

    def set_button(self, seat: int) -> None:
        self.button_seat = seat

    def set_ante_spread(self, bb_ante: int, ante_per_player: int) -> None:
        """Record ante spread metadata used when exporting to pt4_dict."""
        self.bb_ante = bb_ante
        self.ante_per_player = ante_per_player

    # ── Forced postings ───────────────────────────────────────────────────────

    def post_ante(self, player_name: str, amount: int) -> None:
        p = self._require_player(player_name)
        actual = min(amount, p.stack)
        p.stack -= actual
        p.total_invested += actual
        self.pot += actual
        self._action_log["preflop"].append(
            {"player": p.name, "action": "posts_ante", "amount": actual}
        )

    def post_blind(self, player_name: str, amount: int, blind_type: str = "sb") -> None:
        """blind_type: 'sb' or 'bb'."""
        p = self._require_player(player_name)
        actual = min(amount, p.stack)
        p.stack -= actual
        p.street_commitment += actual
        p.total_invested += actual
        self.pot += actual
        self.current_bet = max(self.current_bet, p.street_commitment)
        action_type = "posts_sb" if blind_type == "sb" else "posts_bb"
        if blind_type == "bb":
            self._bb_poster_seat = p.seat
        self._action_log["preflop"].append(
            {"player": p.name, "action": action_type, "amount": actual}
        )

    def post_straddle(self, player_name: str, amount: int) -> None:
        p = self._require_player(player_name)
        actual = min(amount, p.stack)
        p.stack -= actual
        p.street_commitment += actual
        p.total_invested += actual
        self.pot += actual
        self.current_bet = max(self.current_bet, p.street_commitment)
        self.straddle_player = p.name
        self.straddle_amount = actual
        self._action_log["preflop"].append(
            {"player": p.name, "action": "posts_straddle", "amount": actual}
        )

    def begin_preflop(self) -> None:
        """Call after all antes/blinds/straddle are posted to open the preflop betting round."""
        self._street_mentioned = set()
        self._last_actor = None
        self._round_closed = False
        order = self._preflop_order()
        self._players_to_act = deque(order)

    # ── Hole cards and board ──────────────────────────────────────────────────

    def deal_hole_cards(self, player_name: str, cards: list[str]) -> None:
        p = self._get_player(player_name)
        if p:
            p.hole_cards = list(cards)

    def deal_board(self, cards: list[str] | str) -> None:
        """
        Deal board cards and advance to the next street.
        Pass 3 cards for the flop, 1 card for turn or river.
        Also resets the betting round for the new street.
        """
        if isinstance(cards, str):
            cards = [cards]
        if len(cards) == 3 and not self.flop_cards:
            self.flop_cards = list(cards)
            self.street = "flop"
            self._reset_betting_round()
        elif len(cards) == 1 and self.flop_cards and not self.turn_card:
            self.turn_card = cards[0]
            self.street = "turn"
            self._reset_betting_round()
        elif len(cards) == 1 and self.turn_card and not self.river_card:
            self.river_card = cards[0]
            self.street = "river"
            self._reset_betting_round()

    # ── Core action ───────────────────────────────────────────────────────────

    def action(
        self,
        player_name: str,
        action_type: str,
        amount: int = 0,
    ) -> bool:
        """
        Apply a voluntary action.

        Returns True if the action was applied immediately, False if it was
        queued (wrong turn) or rejected (player already folded / all-in).

        Out-of-turn folds are applied immediately (legal in live poker).
        All other out-of-turn actions are queued and retried automatically
        after every successful in-turn action.
        """
        p = self._get_player(player_name)
        if not p or p.status == "folded" or self._hand_complete:
            return False

        # All-in players cannot act further (including fold — it's already resolved)
        if p.status == "all_in":
            return False

        # Street is closed — all active players matched the bet; no more actions allowed
        if self._round_closed:
            return False

        # Track every player the model mentioned on this street
        self._street_mentioned.add(player_name.casefold())

        next_name = self._next_active_in_queue()

        # Out-of-turn action
        if next_name and next_name.casefold() != player_name.casefold():
            if action_type == "folds":
                return self._apply_fold_ooo(p)
            if not self._in_pending_flush:
                self._pending_queue.append(
                    {"player": player_name, "action": action_type, "amount": amount}
                )
            # Auto-advance: skip over players the model never mentioned
            if (self.auto_advance
                    and not self._in_pending_flush
                    and not self._in_auto_advance):
                self._auto_advance_until(player_name)
                self._try_pending()
            return False

        # In-turn action
        if self._players_to_act:
            self._players_to_act.popleft()

        # Reject same player acting twice in a row (OCR duplicate)
        if (self._last_actor is not None
                and self._last_actor == player_name.casefold()
                and action_type != "folds"):
            return False

        applied = self._apply(p, action_type, amount)
        if applied:
            self._last_actor = player_name.casefold()
            self._try_pending()
            self._check_round()
        return applied

    # ── Showdown recording ────────────────────────────────────────────────────

    def add_showdown(
        self,
        player_name: str,
        hole_cards: list[str],
        hand_description: str,
        result: str,
    ) -> None:
        self._showdown.append(
            {
                "player": player_name,
                "hole_cards": hole_cards,
                "hand_description": hand_description,
                "result": result,
            }
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    def get_next_to_act(self) -> Optional[str]:
        """Return the name of the next player who should act, or None."""
        return self._next_active_in_queue()

    def get_pot(self) -> int:
        return self.pot

    def is_hand_complete(self) -> bool:
        return self._hand_complete

    # ── Export ────────────────────────────────────────────────────────────────

    def to_pt4_dict(self) -> dict:
        """
        Export the hand as a dict compatible with pt4_formatter.format_hand().

        Note: 'stack' is the *initial* overlay stack (what was passed to
        add_player), not the remaining stack.  The formatter applies its own
        ante adjustment from there.
        """
        players_data = [
            {
                "seat": p.seat,
                "name": p.name,
                "position": p.position,
                "stack": p.initial_stack,
                "hole_cards": p.hole_cards if p.hole_cards else None,
            }
            for p in self._players
        ]
        return {
            "players": players_data,
            "action": {
                "preflop": list(self._action_log["preflop"]),
                "flop":    list(self._action_log["flop"]),
                "turn":    list(self._action_log["turn"]),
                "river":   list(self._action_log["river"]),
            },
            "board": {
                "flop":  self.flop_cards if self.flop_cards else [],
                "turn":  self.turn_card,
                "river": self.river_card,
            },
            "showdown": list(self._showdown),
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _get_player(self, name: str) -> Optional[Player]:
        key = name.casefold()
        for p in self._players:
            if p.name.casefold() == key:
                return p
        return None

    def _require_player(self, name: str) -> Player:
        p = self._get_player(name)
        if not p:
            raise ValueError(f"Unknown player: {name!r}")
        return p

    def _clockwise_from(self, start_seat: int) -> list[Player]:
        """
        Return all players in clockwise (ascending seat, wrapping) order,
        starting from the seat AFTER start_seat (inclusive wrap).
        The player at start_seat is the LAST in the returned list.
        """
        seats = [p.seat for p in self._players]
        n = len(seats)
        if not n:
            return []
        try:
            idx = seats.index(start_seat)
        except ValueError:
            idx = 0
        return [self._players[(idx + i) % n] for i in range(1, n + 1)]

    def _preflop_order(self) -> list[str]:
        """
        Returns the preflop voluntary action order (active players only).
        With straddle: left of straddle → … → straddle.
        Without:       UTG → … → BB.
        """
        if self.straddle_player:
            ref = self._get_player(self.straddle_player)
            pivot_seat = ref.seat if ref else self.button_seat
        else:
            bb_p = next((p for p in self._players if p.position == "BB"), None)
            if not bb_p and self._bb_poster_seat is not None:
                bb_p = next(
                    (p for p in self._players if p.seat == self._bb_poster_seat), None
                )
            pivot_seat = bb_p.seat if bb_p else self.button_seat
        return [p.name for p in self._clockwise_from(pivot_seat) if p.status == "active"]

    def _postflop_order(self) -> list[str]:
        """SB → BB → UTG → … → BTN (active players only)."""
        return [
            p.name
            for p in self._clockwise_from(self.button_seat)
            if p.status == "active"
        ]

    def _reset_betting_round(self) -> None:
        """Reset state for a new postflop street."""
        self._street_mentioned = set()
        self._last_actor = None
        self._round_closed = False
        self.current_bet = 0
        for p in self._players:
            p.street_commitment = 0
        order = self._postflop_order()
        self._players_to_act = deque(order)

    def _next_active_in_queue(self) -> Optional[str]:
        """Advance past any folded/all-in players at the front of the queue."""
        while self._players_to_act:
            name = self._players_to_act[0]
            p = self._get_player(name)
            if p and p.status == "active":
                return name
            self._players_to_act.popleft()
        return None

    def _apply_fold_ooo(self, p: Player) -> bool:
        """Apply an out-of-order fold immediately."""
        p.status = "folded"
        self._action_log[self.street].append({"player": p.name, "action": "folds"})
        self._players_to_act = deque(
            n for n in self._players_to_act if n.casefold() != p.name.casefold()
        )
        self._try_pending()
        self._check_round()
        return True

    def _apply(self, p: Player, action_type: str, amount: int) -> bool:
        """Apply a validated, in-turn action. Returns True on success."""
        log = self._action_log[self.street]

        if action_type == "folds":
            p.status = "folded"
            log.append({"player": p.name, "action": "folds"})

        elif action_type == "checks":
            log.append({"player": p.name, "action": "checks"})

        elif action_type == "calls":
            call_inc = max(0, self.current_bet - p.street_commitment)
            actual = min(call_inc, p.stack)
            if actual <= 0:
                # Nothing to call — player already matched the bet; treat as check
                log.append({"player": p.name, "action": "checks"})
                return True
            p.stack -= actual
            p.street_commitment += actual
            p.total_invested += actual
            self.pot += actual
            if p.stack == 0:
                p.status = "all_in"
                log.append({"player": p.name, "action": "calls_allin", "amount": actual})
            else:
                log.append({"player": p.name, "action": "calls", "amount": actual})

        elif action_type == "bets":
            actual = min(amount, p.stack)
            if actual <= 0:
                return False
            p.stack -= actual
            p.street_commitment += actual
            p.total_invested += actual
            self.pot += actual
            self.current_bet = p.street_commitment
            if p.stack == 0:
                p.status = "all_in"
                log.append({"player": p.name, "action": "bets_allin", "amount": actual})
            else:
                log.append({"player": p.name, "action": "bets", "amount": actual})
            self._reopen_action(p.name)

        elif action_type in ("raises", "all_in"):
            # amount = player's desired TOTAL street commitment
            added = max(0, amount - p.street_commitment)
            actual_added = min(added, p.stack)
            if actual_added <= 0:
                # Can't raise; treat as a call
                return self._apply(p, "calls", 0)
            p.stack -= actual_added
            p.street_commitment += actual_added
            p.total_invested += actual_added
            self.pot += actual_added
            self.current_bet = max(self.current_bet, p.street_commitment)
            is_allin = p.stack == 0 or action_type == "all_in"
            if is_allin:
                p.status = "all_in"
            act = "all_in" if is_allin else "raises"
            log.append({"player": p.name, "action": act, "amount": p.street_commitment})
            # Always reopen action so remaining active players can call/fold,
            # even when this player went all-in.
            self._reopen_action(p.name)

        else:
            return False

        return True

    def _reopen_action(self, raiser_name: str) -> None:
        """
        After a bet or raise, rebuild the action queue so all remaining active
        players (excluding the raiser) get another turn, in position order.
        """
        if self.street == "preflop":
            full_order = self._preflop_order()
        else:
            full_order = self._postflop_order()

        lower = raiser_name.casefold()
        lowers = [n.casefold() for n in full_order]
        try:
            idx = lowers.index(lower)
        except ValueError:
            idx = -1

        n = len(full_order)
        new_queue = []
        for i in range(1, n):   # n-1 players: everyone EXCEPT the raiser
            name = full_order[(idx + i) % n]
            p = self._get_player(name)
            if p and p.status == "active":
                new_queue.append(name)
        self._players_to_act = deque(new_queue)

    def _auto_advance_until(self, target: str) -> None:
        """
        Auto-check or auto-fold each consecutive un-mentioned player until
        `target` is next to act or we exhaust the queue.

        Only called when auto_advance=True.  Logs every inference to
        self.auto_advance_log.
        """
        if self._in_auto_advance:
            return
        self._in_auto_advance = True
        try:
            safety = len(self._players) + 1
            while safety > 0:
                safety -= 1
                next_name = self._next_active_in_queue()
                if not next_name:
                    break
                if next_name.casefold() == target.casefold():
                    break   # target is now up — stop
                if next_name.casefold() in self._street_mentioned:
                    break   # player WAS mentioned; don't skip, let pending queue handle it
                if next_name.casefold() in self._showdown_players:
                    break   # player appears in showdown — never auto-fold them
                p = self._get_player(next_name)
                if not p or p.status != "active":
                    self._players_to_act.popleft()
                    continue
                self._players_to_act.popleft()
                if self.current_bet > p.street_commitment:
                    amount_to_call = self.current_bet - p.street_commitment
                    self.auto_advance_log.append(
                        f"[auto-fold]  {p.name} — not in model output, "
                        f"facing ${amount_to_call} bet on {self.street}"
                    )
                    p.status = "folded"
                    self._action_log[self.street].append(
                        {"player": p.name, "action": "folds"}
                    )
                else:
                    self.auto_advance_log.append(
                        f"[auto-check] {p.name} — not in model output, "
                        f"no bet to face on {self.street}"
                    )
                    self._action_log[self.street].append(
                        {"player": p.name, "action": "checks"}
                    )
                self._check_round()
                if self._hand_complete:
                    break
        finally:
            self._in_auto_advance = False

    def _try_pending(self) -> None:
        """Retry queued out-of-order actions until no more can be applied.

        Uses a recursion guard so nested calls (from within action()) are
        no-ops; the outer call's while loop catches any newly-enabled actions.
        """
        if self._in_pending_flush:
            return
        self._in_pending_flush = True
        try:
            changed = True
            while changed and self._pending_queue:
                changed = False
                snapshot = list(self._pending_queue)
                self._pending_queue = []
                for act in snapshot:
                    applied = self.action(
                        act["player"], act["action"], act.get("amount", 0)
                    )
                    if applied:
                        changed = True
                    else:
                        self._pending_queue.append(act)
        finally:
            self._in_pending_flush = False

    def _check_round(self) -> None:
        """After an action, check whether the betting round (or hand) is complete."""
        active = [p for p in self._players if p.status == "active"]

        # If zero active players remain, no more betting is possible;
        # clear the queue so the round ends cleanly.
        # (Do NOT clear when 1 active player remains — they may still need to call.)
        if len(active) == 0:
            self._players_to_act.clear()

        # Round is complete when the queue has no more active players to act
        if self._next_active_in_queue() is not None:
            return  # More players to act this round

        # Check if the hand itself is over
        non_folded = [p for p in self._players if p.status != "folded"]
        if len(non_folded) <= 1:
            # One (or zero) players left — hand is done
            self._hand_complete = True
        elif self.street == "river":
            self._hand_complete = True
        else:
            # Queue is empty. Only close the street when every active (non-all-in)
            # player has matched the current bet. If any active player still owes
            # chips (e.g. they're in the pending queue and haven't called yet),
            # leave the round open so their pending action can be applied.
            _unmatched = [
                p for p in self._players
                if p.status == "active" and p.street_commitment < self.current_bet
            ]
            if not _unmatched:
                self._round_closed = True
