"""
src/pipeline/gemini_pipeline.py

Full Gemini-powered hand history pipeline:

  YouTube URL + session config
    → Gemini video analysis (per hand time window)
    → JSON parsing + action normalization
    → feed_hand() (state machine)
    → format_hand() (PT4 text)
    → pt4_linter (validation)
    → output file

Usage:
    python -m src.pipeline.gemini_pipeline
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from google import genai
from google.genai import types

from src.session.session_config import SessionConfig
from src.vision.hand_feeder import feed_hand, FeedError
from src.export.pt4_formatter import format_hand, HandValidationError


# ── Action name normalisation ─────────────────────────────────────────────────

_VALID_CARD_RE = re.compile(r"^[2-9TJQKA][cdhs]$", re.IGNORECASE)
_RANK_FIX = {"10": "T", "1": "A"}   # Gemini sometimes writes "10x" for tens


def _normalize_card(raw: str | None) -> str | None:
    """Return a valid card token (e.g. 'Th') or None if unreadable."""
    if not raw:
        return None
    s = str(raw).strip()
    # Fix "10x" → "Tx"
    if len(s) == 3 and s[:2] == "10":
        s = "T" + s[2]
    if not _VALID_CARD_RE.match(s):
        return None
    return s[0].upper() + s[1].lower()


def _sanitize_gemini_data(data: dict) -> dict:
    """
    Clean up common Gemini card / result errors before conversion:
    - Normalize and validate card tokens
    - Remove duplicate cards (board takes priority; earlier player takes priority)
    - Normalize result strings ("wins board 1" → "wins", "lost" → "loses")
    - Strip amounts from fold/check actions (they're always 0)
    - Close straddle preflop action when straddle player never acted
    """
    import copy
    data = copy.deepcopy(data)

    # ── 1. Collect & normalize board cards ──────────────────────────────────
    board = data.get("board")
    if not isinstance(board, dict):
        board = {}  # Gemini sometimes returns a list; discard and let retry handle it
    seen: set[str] = set()

    def _norm_list(lst):
        out = []
        for c in (lst or []):
            nc = _normalize_card(c)
            if nc:
                out.append(nc)
        return out or None

    flop  = _norm_list(board.get("flop"))
    turn  = _normalize_card(board.get("turn"))
    river = _normalize_card(board.get("river"))

    for c in (flop or []):
        seen.add(c)
    if turn:
        seen.add(turn)
    if river:
        seen.add(river)

    data["board"] = {"flop": flop, "turn": turn, "river": river}

    # ── 2. Normalize player hole cards (dedup against board + earlier players) ─
    for p in (data.get("players") or []):
        raw_hc = p.get("hole_cards") or []
        normed: list[str] = []
        for c in raw_hc:
            nc = _normalize_card(c)
            if nc and nc not in seen:
                normed.append(nc)
                seen.add(nc)
        p["hole_cards"] = normed if len(normed) == 2 else None

    # ── 3. Normalize showdown hole cards & result strings ───────────────────
    for sd in (data.get("showdown") or []):
        raw_hc = sd.get("hole_cards") or []
        normed = [_normalize_card(c) for c in raw_hc]
        sd["hole_cards"] = [c for c in normed if c] or []

        result_raw = sd.get("result")
        if not isinstance(result_raw, str):
            result_raw = None
        result = (result_raw or "").lower()
        if result.startswith("win"):
            sd["result"] = "wins"
        elif result.startswith("los") or result.startswith("lose"):
            sd["result"] = "loses"

    # ── 4. Strip amounts from fold/check actions ─────────────────────────────
    for street_acts in (data.get("action") or {}).values():
        for act in (street_acts or []):
            if act.get("action") in ("folds", "fold", "checks", "check"):
                act["amount"] = 0

    # ── 5. Handle straddle: inject posts_straddle, set position to STR, ──────
    #       and close the straddle preflop action when needed.
    straddle = data.get("straddle")
    if straddle:
        straddle_player = straddle.get("player") or ""
        straddle_amount = int(straddle.get("amount") or 0)

        if straddle_player and straddle_amount:
            # Ensure straddle player has position "STR"
            for p in (data.get("players") or []):
                if p.get("name") == straddle_player:
                    p["position"] = "STR"

            preflop = data.get("action", {}).get("preflop") or []

            # Inject posts_straddle as the FIRST preflop action if not already there
            has_post = any(a.get("action") == "posts_straddle" for a in preflop)
            if not has_post:
                preflop.insert(0, {
                    "player": straddle_player,
                    "action": "posts_straddle",
                    "amount": straddle_amount,
                })

            # Close the straddle player's preflop option: if no raise above straddle
            # amount occurred, append a check for the straddle player if they haven't acted
            voluntary = [a for a in preflop if a.get("action") not in (
                "posts_ante", "posts_sb", "posts_bb", "posts_straddle"
            )]
            acted   = {a["player"] for a in voluntary}
            max_bet = max((a.get("amount") or 0 for a in voluntary
                           if a.get("action") in ("raises", "bets")), default=0)
            if straddle_player not in acted and max_bet <= straddle_amount:
                preflop.append({"player": straddle_player, "action": "checks", "amount": 0})

            data["action"]["preflop"] = preflop

    # ── 6. Infer missing folds for players who vanish between streets ─────────
    # A player who acted voluntarily and then doesn't appear on the next street
    # and isn't in the showdown MUST have folded — Gemini sometimes omits this.
    _BLIND_SET = {"posts_ante", "posts_sb", "posts_bb", "posts_straddle"}
    streets = ["preflop", "flop", "turn", "river"]
    action_block = data.get("action") or {}
    showdown_players_lower = {
        (sd.get("player") or "").lower()
        for sd in (data.get("showdown") or [])
    }

    # Remove any explicit fold actions for showdown participants — Gemini
    # sometimes both folds a player AND lists them in the showdown, causing R17.
    for st in streets:
        acts = action_block.get(st) or []
        filtered = [
            a for a in acts
            if not (
                a.get("action") == "folds"
                and (a.get("player") or "").lower() in showdown_players_lower
            )
        ]
        if len(filtered) != len(acts):
            action_block[st] = filtered

    # Players active (voluntary) on each street, keyed by lowercase name
    street_vol: dict[str, set[str]] = {}
    for st in streets:
        acts = action_block.get(st) or []
        street_vol[st] = {
            a["player"].lower() for a in acts
            if a.get("action") not in _BLIND_SET
        }

    for st_idx, st in enumerate(streets):
        acts = action_block.get(st) or []
        for player_lower in list(street_vol[st]):
            # Already explicitly folded on this street?
            if any(
                a["player"].lower() == player_lower and a.get("action") == "folds"
                for a in acts
            ):
                continue
            # In the showdown?
            if player_lower in showdown_players_lower:
                continue
            # Appears on any later street?
            if any(player_lower in street_vol[later] for later in streets[st_idx + 1:]):
                continue
            # Must have folded here — find canonical name and inject fold
            player_name = next(
                (a["player"] for a in acts if a["player"].lower() == player_lower),
                player_lower,
            )
            acts.append({"player": player_name, "action": "folds", "amount": 0})
        action_block[st] = acts

    return data


_ACTION_MAP: dict[str, str] = {
    "raise":    "raises", "raises":    "raises",
    "3-bet":    "raises", "3-bets":    "raises",
    "4-bet":    "raises", "4-bets":    "raises",
    "re-raise": "raises", "re-raises": "raises",
    "reraise":  "raises",
    "call":     "calls",  "calls":     "calls",
    "fold":     "folds",  "folds":     "folds",
    "check":    "checks", "checks":    "checks",
    "bet":      "bets",   "bets":      "bets",
    "all-in":   "all_in", "all_in":    "all_in",
    "allin":    "all_in", "goes all-in": "all_in",
    "shoves":   "all_in", "shove":     "all_in",
    "jams":     "all_in", "jam":       "all_in",
}


def _map_action(raw: str) -> str:
    return _ACTION_MAP.get(raw.strip().lower(), raw.strip().lower())


# ── Position assignment ───────────────────────────────────────────────────────

# Position name lists for N-handed tables (BTN always first)
_POS_NAMES: dict[int, list[str]] = {
    2: ["BTN", "BB"],
    3: ["BTN", "SB", "BB"],
    4: ["BTN", "SB", "BB", "UTG"],
    5: ["BTN", "SB", "BB", "UTG", "CO"],
    6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
    7: ["BTN", "SB", "BB", "UTG", "UTG+1", "HJ", "CO"],
    8: ["BTN", "SB", "BB", "UTG", "UTG+1", "MP", "HJ", "CO"],
    9: ["BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO"],
}


def _assign_positions(session: SessionConfig, hand_offset: int, n_active: int) -> dict[str, str]:
    """Return {player_name: position} for all session players given n_active at the table."""
    seats   = session._seats()
    n_total = len(seats)
    btn_seat = session.get_button_seat(hand_offset)
    btn_idx  = seats.index(btn_seat)

    # Clockwise from BTN
    rotation = [seats[(btn_idx + i) % n_total] for i in range(n_total)]
    pos_list = _POS_NAMES.get(n_active, _POS_NAMES.get(min(n_active, 9), ["BTN"]))

    result: dict[str, str] = {}
    for i, seat in enumerate(rotation):
        name = session.get_player_name(seat)
        result[name] = pos_list[i] if i < len(pos_list) else ""
    return result


# ── Prompt template ───────────────────────────────────────────────────────────

_PROMPT_TMPL = """You are an expert poker analyst reviewing a hand from Hustler Casino Live.

Watch the YouTube video ONLY from {start_ts} to {end_ts} (hand #{hand_num}).

Game details:
  Stakes : {stakes} NL Hold'em with ${bb_ante} BB ante
  Players: {n_players} at the table this hand
  Button : {btn_player} (Seat {btn_seat})
  SB     : {sb_player}  (Seat {sb_seat})
  BB     : {bb_player}  (Seat {bb_seat})

Full player roster for the session (some may not be in this hand):
{roster_lines}

CARD READING GUIDE — read all cards directly from overlay graphics:
  Rank: A K Q J T 9 8 7 6 5 4 3 2  (printed on the card face)
  Suit: ♠ spades=BLACK  ♥ hearts=RED  ♦ diamonds=RED  ♣ clubs=BLACK
  If you are uncertain about any card, provide your best guess and note it in "notes".
  Cards appear as e.g. "Ah" (Ace of hearts), "Tc" (Ten of clubs), "2s" (Two of spades).
  IMPORTANT: Read the cards EXACTLY as shown on the overlay. Pay close attention to
  diamonds vs hearts (both red) and spades vs clubs (both black). Get every card right.

FEW-SHOT EXAMPLE — Human-verified extraction from this same stream (33:20–35:20):

  Table: Airball seat 2, Greedo seat 3, Francisco seat 4, Dylan seat 5, Otto seat 6, Alex seat 9
  Button: seat 3 (Greedo)

  Straddle : Airball (CO) $200
  Preflop  : Greedo raises $600 | Francisco folds | Dylan folds | Otto calls $600
             | Alex raises $3000 | Airball folds | Greedo calls | Otto calls
  Flop     : Kc 9c 2h
  Postflop : Otto checks | Alex bets $3000 | Greedo folds | Otto raises all-in $21000
             | Alex calls
  Run it twice — Board 1 turn+river: Kd 7c | Board 2 turn+river: Jc 5d
  Result   : Otto wins both boards with flush (Tc 8c)

  Hole cards (read directly from overlay — note suit carefully):
    Airball:   8h 2d   (eight of HEARTS red, two of DIAMONDS red)
    Greedo:    Ad 4d   (ace of DIAMONDS red, four of DIAMONDS red)
    Francisco: Qd 7d   (queen of DIAMONDS red, seven of DIAMONDS red — not hearts)
    Dylan:     Ts 5c   (ten of SPADES black, five of CLUBS black)
    Otto:      Tc 8c   (ten of CLUBS black, eight of CLUBS black — NOT spades)
    Alex:      Ah 5s   (ace of HEARTS red, five of SPADES black)

CRITICAL — ACTION ORDER RULES:
- Preflop WITH straddle: action starts LEFT of the straddler going clockwise. Each
  player acts exactly once per sub-round. If someone raises, the action reopens
  and goes clockwise starting left of the raiser.
- Preflop WITHOUT straddle: action starts LEFT of the BB (UTG), going clockwise
  around to SB then BB last.
- Postflop: action starts with the first active player LEFT of the button.
- List EVERY player's action on EVERY street in strict seat order. Do not skip anyone.
- If a player folds, STILL list their fold entry in the correct seat-order position.
- A player who already called CANNOT fold unless someone raises after their call.
- Do NOT list any player's action twice on the same sub-round unless they are
  responding to a re-raise that occurred after their earlier call.

Instructions:
1. Identify exactly which players are seated and active for this hand.
2. Note each player's chip stack at the START of the hand (before antes).
3. Is there a straddle? If yes, who and how much?
4. List every preflop action IN STRICT SEAT ORDER starting left of BB (UTG first,
   then left-to-right around the table to SB, then BB last).
   IMPORTANT: do NOT include the ante/blind/straddle posts — only voluntary actions.
   IMPORTANT: every active player who folds must have an explicit "folds" action.
   IMPORTANT: if a player called/raised and then faces a re-raise, list their
   response (call/raise/fold) as a SEPARATE subsequent action.
5. Report the ACTUAL board cards (flop, turn, river) that you see dealt in the video.
   Do NOT leave board cards as null if any postflop action occurred.
   ALL-IN RULE: When players are all-in, the remaining board cards are STILL dealt
   face-up with no betting. Always include the turn and river cards even when no
   betting action occurs on those streets — the cards are run out and shown on screen.
6. List every postflop action on each street in order. Every active player must
   either bet/check/call/raise/fold before the next street begins.
7. Report the showdown: which cards were shown, who won, how much?

AMOUNT RULE: always report the TOTAL chips committed by a player in an
action — not the increment. So "raises to $3,000" → amount=3000.
For calls: report the size of the bet being called.

ACTION TYPES (use only these exact strings):
  raises  calls  folds  checks  bets  all_in

Return ONLY a JSON object — no markdown fences, no extra text:
{{
  "hand_num": {hand_num},
  "players": [
    {{"seat": N, "name": "X", "position": "BTN|SB|BB|UTG|HJ|CO|...",
      "stack": N, "hole_cards": ["Xr","Xs"] or null}}
  ],
  "straddle": {{"player": "X", "amount": N}} or null,
  "action": {{
    "preflop": [{{"player": "X", "action": "raises", "amount": N}}],
    "flop":    [...],
    "turn":    [...],
    "river":   [...]
  }},
  "board": {{
    "flop":  ["Xr","Xs","Xt"] or null,
    "turn":  "Xr" or null,
    "river": "Xr" or null
  }},
  "showdown": [
    {{"player": "X", "hole_cards": ["Xr","Xs"],
      "hand_description": "two pair, Aces and Kings", "result": "wins"}}
  ],
  "pot": N,
  "notes": "any observations about unclear reads"
}}
"""


# Shared JSON schema block reused in verify and arbitrate prompts
_JSON_SCHEMA = """{
  "hand_num": N,
  "players": [
    {"seat": N, "name": "X", "position": "BTN|SB|BB|UTG|HJ|CO|...",
      "stack": N, "hole_cards": ["Xr","Xs"] or null}
  ],
  "straddle": {"player": "X", "amount": N} or null,
  "action": {
    "preflop": [{"player": "X", "action": "raises", "amount": N}],
    "flop":    [...],
    "turn":    [...],
    "river":   [...]
  },
  "board": {
    "flop":  ["Xr","Xs","Xt"] or null,
    "turn":  "Xr" or null,
    "river": "Xr" or null
  },
  "showdown": [
    {"player": "X", "hole_cards": ["Xr","Xs"],
      "hand_description": "two pair, Aces and Kings", "result": "wins"}
  ],
  "pot": N,
  "notes": "any observations about unclear reads"
}"""

_VERIFY_PROMPT_TMPL = """You are an expert poker analyst reviewing a hand from Hustler Casino Live.

Watch the YouTube video ONLY from {start_ts} to {end_ts} (hand #{hand_num}).

A prior analysis produced the following JSON. Re-watch the same clip and verify every detail.

PRIOR ANALYSIS:
{pass1_json}

VERIFICATION CHECKLIST — read each value directly from the overlay graphics:
1. HOLE CARDS: For every player in the showdown, freeze on each card-reveal frame.
   Read rank (A K Q J T 9 8 7 6 5 4 3 2) AND suit (♠ black, ♥ red, ♦ red, ♣ black)
   independently — do not assume suit from color alone.
2. BOARD CARDS: Read each community card from the board display, in order.
3. BET AMOUNTS: Read the dollar figure from each player's action overlay exactly.
4. STACK SIZES: Read from chip-count overlays at the START of the hand.
5. POT SIZE: Read from the pot display after the final action.
6. WINNER: Confirm who won and the exact amount in the WIN graphic.

Return ONLY a JSON object with the SAME structure as the prior analysis.
Keep values you confirmed as correct. Correct only values you can verify are wrong.

Return ONLY raw JSON — no markdown, no explanation:
{json_schema}
"""

_ARBITRATE_PROMPT_TMPL = """You are an expert poker analyst reviewing a hand from Hustler Casino Live.

Watch the YouTube video ONLY from {start_ts} to {end_ts} (hand #{hand_num}).

Two prior analyses of this hand disagree on the following specific fields.
Watch the video once more and give the definitive answer for ONLY those disagreeing values.

DISAGREEMENTS:
{diffs_text}

PASS 1 full JSON:
{pass1_json}

PASS 2 full JSON:
{pass2_json}

Return ONLY a JSON object with the same full structure, but with all disagreeing fields
resolved to the correct values you observe in the video.

Return ONLY raw JSON — no markdown, no explanation:
{json_schema}
"""


# ── Diff helpers ──────────────────────────────────────────────────────────────

def _diff_hand_data(d1: dict, d2: dict) -> list[tuple[str, object, object]]:
    """Return list of (field_path, val1, val2) for fields that differ between two hand dicts."""
    diffs: list[tuple[str, object, object]] = []

    # Players: hole_cards and stack
    p1_map = {(p.get("name") or "").lower(): p for p in (d1.get("players") or [])}
    p2_map = {(p.get("name") or "").lower(): p for p in (d2.get("players") or [])}
    for name in sorted(set(p1_map) & set(p2_map)):
        p1, p2 = p1_map[name], p2_map[name]
        hc1 = sorted(p1.get("hole_cards") or [])
        hc2 = sorted(p2.get("hole_cards") or [])
        if hc1 and hc2 and hc1 != hc2:
            diffs.append((f"players.{name}.hole_cards", hc1, hc2))
        s1, s2 = p1.get("stack"), p2.get("stack")
        if s1 and s2 and s1 != s2:
            diffs.append((f"players.{name}.stack", s1, s2))

    # Board
    b1 = d1.get("board") or {}
    b2 = d2.get("board") or {}
    f1 = sorted(b1.get("flop") or [])
    f2 = sorted(b2.get("flop") or [])
    if f1 and f2 and f1 != f2:
        diffs.append(("board.flop", f1, f2))
    for st in ("turn", "river"):
        c1, c2 = b1.get(st), b2.get(st)
        if c1 and c2 and c1 != c2:
            diffs.append((f"board.{st}", c1, c2))

    # Showdown: hole_cards and result
    sd1 = {(s.get("player") or "").lower(): s for s in (d1.get("showdown") or [])}
    sd2 = {(s.get("player") or "").lower(): s for s in (d2.get("showdown") or [])}
    for name in sorted(set(sd1) & set(sd2)):
        s1, s2 = sd1[name], sd2[name]
        hc1 = sorted(s1.get("hole_cards") or [])
        hc2 = sorted(s2.get("hole_cards") or [])
        if hc1 and hc2 and hc1 != hc2:
            diffs.append((f"showdown.{name}.hole_cards", hc1, hc2))
        r1, r2 = s1.get("result"), s2.get("result")
        if r1 and r2 and r1 != r2:
            diffs.append((f"showdown.{name}.result", r1, r2))

    # Pot
    pot1, pot2 = d1.get("pot"), d2.get("pot")
    if pot1 and pot2 and pot1 != pot2:
        diffs.append(("pot", pot1, pot2))

    # Action amounts: group by (street, player, action_type)
    a1 = d1.get("action") or {}
    a2 = d2.get("action") or {}
    for street in ("preflop", "flop", "turn", "river"):
        def _amap(acts: list) -> dict:
            m: dict = {}
            for a in acts:
                k = ((a.get("player") or "").lower(), a.get("action") or "")
                m.setdefault(k, []).append(a.get("amount") or 0)
            return m
        am1 = _amap(a1.get(street) or [])
        am2 = _amap(a2.get(street) or [])
        for k in sorted(set(am1) & set(am2)):
            if am1[k] != am2[k]:
                diffs.append((f"action.{street}.{k[0]}.{k[1]}", am1[k], am2[k]))

    return diffs


def _print_diffs(diffs: list[tuple[str, object, object]], label1: str = "Pass 1", label2: str = "Pass 2") -> None:
    if not diffs:
        print(f"  [verify] No differences found — passes agree on all fields")
        return
    print(f"  [verify] {len(diffs)} field(s) changed between {label1} and {label2}:")
    for field, v1, v2 in diffs:
        print(f"    {field}")
        print(f"      {label1}: {v1}")
        print(f"      {label2}: {v2}")


# ── Main pipeline class ───────────────────────────────────────────────────────

class GeminiPipeline:
    def __init__(
        self,
        youtube_url: str,
        session_config_path: str,
        model: str = "gemini-2.5-flash",
    ) -> None:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY environment variable not set")
        self.client       = genai.Client(api_key=api_key)
        self.youtube_url  = youtube_url
        self.model        = model
        self.session      = SessionConfig.from_json(session_config_path)

    # ── Prompt building ───────────────────────────────────────────────────────

    def _build_retry_prompt(self, original: str, error: str) -> str:
        """Augment the original prompt with the prior failure so Gemini
        re-watches the same window and focuses on the gap that broke
        feed_hand on the first pass. Per spec: include the literal
        error message, then re-state the completeness requirement so
        no street's action gets skipped a second time."""
        return (
            f"Your previous response had an error: {error}. "
            f"Please re-watch the video and make sure EVERY player's action is "
            f"listed on EVERY street, in seat order.\n\n"
            f"Specifically: do NOT skip checks. If a player checks (especially "
            f"a small-stack or early-position player), that check still needs "
            f"to appear in the JSON. Walk the table left-to-right after each "
            f"new card and account for every active player before moving on.\n\n"
            f"---\n\n"
            f"{original}"
        )

    def _build_prompt(self, hand_num: int, start_sec: int, end_sec: int) -> str:
        hand_offset = hand_num - 1
        n_players   = len(self.session._players)

        btn_seat = self.session.get_button_seat(hand_offset)
        sb_seat  = self.session.get_sb_seat(hand_offset)
        bb_seat  = self.session.get_bb_seat(hand_offset)

        btn_player = self.session.get_player_name(btn_seat)
        sb_player  = self.session.get_player_name(sb_seat)
        bb_player  = self.session.get_player_name(bb_seat)

        positions = _assign_positions(self.session, hand_offset, n_players)

        roster_lines = "\n".join(
            f"  Seat {p.seat}: {p.name} ({positions.get(p.name, '?')})"
            for p in self.session._players
        )

        def _ts(sec: int) -> str:
            return f"{sec // 60}:{sec % 60:02d} ({sec}s)"

        return _PROMPT_TMPL.format(
            hand_num=hand_num,
            start_ts=_ts(start_sec),
            end_ts=_ts(end_sec),
            stakes=self.session.stakes,
            bb_ante=self.session.bb_ante,
            n_players=n_players,
            btn_seat=btn_seat, btn_player=btn_player,
            sb_seat=sb_seat,  sb_player=sb_player,
            bb_seat=bb_seat,  bb_player=bb_player,
            roster_lines=roster_lines,
        )

    def _build_verify_prompt(self, hand_num: int, start_sec: int, end_sec: int, pass1_data: dict) -> str:
        def _ts(sec: int) -> str:
            return f"{sec // 60}:{sec % 60:02d} ({sec}s)"
        return _VERIFY_PROMPT_TMPL.format(
            hand_num=hand_num,
            start_ts=_ts(start_sec),
            end_ts=_ts(end_sec),
            pass1_json=json.dumps(pass1_data, indent=2, ensure_ascii=False),
            json_schema=_JSON_SCHEMA,
        )

    def _build_arbitrate_prompt(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        diffs: list[tuple[str, object, object]],
        pass1_data: dict,
        pass2_data: dict,
    ) -> str:
        diff_lines = []
        for field, v1, v2 in diffs:
            diff_lines.append(f"  {field}:")
            diff_lines.append(f"    Pass 1 said: {v1}")
            diff_lines.append(f"    Pass 2 said: {v2}")
        def _ts(sec: int) -> str:
            return f"{sec // 60}:{sec % 60:02d} ({sec}s)"
        return _ARBITRATE_PROMPT_TMPL.format(
            hand_num=hand_num,
            start_ts=_ts(start_sec),
            end_ts=_ts(end_sec),
            diffs_text="\n".join(diff_lines),
            pass1_json=json.dumps(pass1_data, indent=2, ensure_ascii=False),
            pass2_json=json.dumps(pass2_data, indent=2, ensure_ascii=False),
            json_schema=_JSON_SCHEMA,
        )

    def _run_verify_pass(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        pass1_data: dict,
    ) -> dict | None:
        """
        Run Pass 2 (verification). Calls Gemini again with the Pass 1 JSON embedded
        in the prompt and asks it to re-watch and correct any errors.

        Returns the sanitized Pass 2 data dict, or None on failure.
        """
        prompt = self._build_verify_prompt(hand_num, start_sec, end_sec, pass1_data)
        print("  [verify] Pass 2: re-watching to verify...", end=" ", flush=True)
        try:
            raw = self._call_gemini(start_sec, end_sec, prompt, local_file_path=getattr(self, "_local_file_path", None))
        except Exception as e:
            print(f"FAILED ({e})")
            return None
        try:
            data2 = self._parse_response(raw)
        except ValueError as e:
            print(f"FAILED (parse: {e})")
            return None
        data2 = _sanitize_gemini_data(data2)
        print("OK")
        return data2

    def _run_arbitrate_pass(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        diffs: list[tuple[str, object, object]],
        pass1_data: dict,
        pass2_data: dict,
    ) -> dict | None:
        """
        Run Pass 3 (arbitration) when Pass 1 and Pass 2 disagree. Returns
        the sanitized Pass 3 dict, or None on failure.
        """
        prompt = self._build_arbitrate_prompt(hand_num, start_sec, end_sec, diffs, pass1_data, pass2_data)
        print("  [verify] Pass 3: arbitrating disagreements...", end=" ", flush=True)
        try:
            raw = self._call_gemini(start_sec, end_sec, prompt, local_file_path=getattr(self, "_local_file_path", None))
        except Exception as e:
            print(f"FAILED ({e})")
            return None
        try:
            data3 = self._parse_response(raw)
        except ValueError as e:
            print(f"FAILED (parse: {e})")
            return None
        data3 = _sanitize_gemini_data(data3)
        print("OK")
        return data3

    # ── Gemini API call ───────────────────────────────────────────────────────

    def _call_gemini(
        self,
        start_sec: int,
        end_sec: int,
        prompt: str,
        local_file_path: str | None = None,
    ) -> str:
        """Call Gemini with either a YouTube URL (with time offsets) or an uploaded local file."""
        if local_file_path:
            file_ref = self._upload_local_file(local_file_path)
            video_part = types.Part(
                file_data=types.FileData(
                    file_uri=file_ref.uri,
                    mime_type=file_ref.mime_type or "video/webm",
                ),
            )
        else:
            video_part = types.Part(
                file_data=types.FileData(
                    file_uri=self.youtube_url,
                    mime_type="video/*",
                ),
                video_metadata=types.VideoMetadata(
                    start_offset=f"{start_sec}s",
                    end_offset=f"{end_sec}s",
                ),
            )
        response = self.client.models.generate_content(
            model=self.model,
            contents=[video_part, types.Part(text=prompt)],
        )
        return response.text

    def _upload_local_file(self, file_path: str):
        """Upload a local video file to Gemini Files API and wait until it's ACTIVE.
        Caches the result per path so retries don't re-upload."""
        if not hasattr(self, "_file_cache"):
            self._file_cache: dict = {}
        if file_path in self._file_cache:
            return self._file_cache[file_path]

        import mimetypes
        mime = mimetypes.guess_type(file_path)[0] or "video/mp4"
        print(f"    Uploading {file_path} ({mime})...", end=" ", flush=True)
        file_ref = self.client.files.upload(
            file=file_path,
            config={"mime_type": mime, "display_name": Path(file_path).name},
        )
        # Poll until the file is ACTIVE (processing can take a few seconds)
        import time as _time
        for _ in range(30):
            file_ref = self.client.files.get(name=file_ref.name)
            if file_ref.state and file_ref.state.name == "ACTIVE":
                break
            _time.sleep(2)
        print(f"ready ({file_ref.name})")
        self._file_cache[file_path] = file_ref
        return file_ref

    # ── Response parsing ──────────────────────────────────────────────────────

    def _parse_response(self, raw: str) -> dict:
        """Strip markdown fences and parse JSON."""
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip().rstrip("`").strip()
        # Handle trailing commas (common Gemini quirk)
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"Gemini returned invalid JSON: {e}\n\nRaw:\n{raw[:800]}")

    # ── Convert Gemini JSON → internal hand format ────────────────────────────

    def _to_hand_json(self, data: dict, hand_num: int) -> dict:
        """Convert Gemini's structured output to feed_hand()-compatible format."""
        # ── Players ──────────────────────────────────────────────────────────
        players_raw = data.get("players") or []
        players: list[dict] = []
        for p in players_raw:
            name = p.get("name") or ""
            if not name:
                continue
            # Pull hole cards from showdown if not given on player record
            hole_cards = p.get("hole_cards") or None
            players.append({
                "seat":       int(p.get("seat") or 0),
                "name":       name,
                "position":   str(p.get("position") or "").upper(),
                "stack":      int(p.get("stack") or 0),
                "hole_cards": hole_cards,
            })

        # ── Override positions with session config (session is authoritative) ─
        # Build rotation from only the seats present in this hand, so absent
        # seats don't displace positions for the players who are active.
        hand_offset    = hand_num - 1
        btn_seat       = self.session.get_button_seat(hand_offset)
        session_seats  = self.session._seats()
        straddle_name  = ((data.get("straddle") or {}).get("player") or "").casefold()
        active_seat_set = {p["seat"] for p in players}
        if btn_seat in session_seats:
            btn_idx  = session_seats.index(btn_seat)
            n_total  = len(session_seats)
            rotation = [
                session_seats[(btn_idx + i) % n_total]
                for i in range(n_total)
                if session_seats[(btn_idx + i) % n_total] in active_seat_set
            ]
            pos_list    = _POS_NAMES.get(len(rotation),
                                         _POS_NAMES.get(min(len(rotation), 9), ["BTN"]))
            seat_to_pos = {seat: pos_list[i]
                           for i, seat in enumerate(rotation) if i < len(pos_list)}
            for p in players:
                if p["name"].casefold() == straddle_name:
                    p["position"] = "STR"
                else:
                    new_pos = seat_to_pos.get(p["seat"], "")
                    if new_pos:
                        p["position"] = new_pos

        # ── Actions ──────────────────────────────────────────────────────────
        per_street: dict[str, list[dict]] = {
            "preflop": [], "flop": [], "turn": [], "river": []
        }
        action_raw = data.get("action") or {}
        for street in ("preflop", "flop", "turn", "river"):
            for act in (action_raw.get(street) or []):
                mapped = _map_action(act.get("action") or "")
                if not mapped:
                    continue
                entry: dict = {
                    "player": act.get("player") or "",
                    "action": mapped,
                }
                amt = act.get("amount")
                if amt:
                    entry["amount"] = int(amt)
                per_street[street].append(entry)

        # ── Board ─────────────────────────────────────────────────────────────
        board_raw = data.get("board") or {}
        flop_cards = board_raw.get("flop") or None
        if isinstance(flop_cards, list) and not flop_cards:
            flop_cards = None

        board = {
            "flop":  flop_cards,
            "turn":  board_raw.get("turn") or None,
            "river": board_raw.get("river") or None,
        }

        # ── Showdown ─────────────────────────────────────────────────────────
        showdown: list[dict] = []
        for sd in (data.get("showdown") or []):
            player = sd.get("player") or ""
            if not player:
                continue
            hc = sd.get("hole_cards") or []
            showdown.append({
                "player":           player,
                "hole_cards":       hc if isinstance(hc, list) else [],
                "hand_description": sd.get("hand_description") or "",
                "result":           sd.get("result") or "",
            })

        return {
            "players":  players,
            "action":   per_street,
            "board":    board,
            "showdown": showdown,
        }

    # ── Partial hand summary ──────────────────────────────────────────────────

    def _partial_summary(self, pt4: dict, hand_num: int, gemini_data: dict, reason: str) -> str:
        """Human-readable summary for hands that couldn't be fully formatted (e.g. run-it-twice)."""
        game   = pt4.get("game", {})
        stakes = game.get("stakes", self.session.stakes)
        notes  = gemini_data.get("notes") or ""
        lines  = [
            f"# PARTIAL HAND: Hand {hand_num} [{self.youtube_url}]",
            f"# Note: {reason}",
            f"# Stakes: {stakes}",
        ]
        if notes:
            lines.append(f"# {notes[:200]}")
        lines.append("#")
        for p in pt4.get("players") or []:
            pos = f" [{p['position']}]" if p.get("position") else ""
            lines.append(f"# Seat {p['seat']}: {p['name']}{pos}  stack=${p.get('stack', 0):,}")
        action = pt4.get("action") or {}
        for street in ("preflop", "flop", "turn", "river"):
            acts = action.get(street) or []
            if acts:
                lines.append(f"# {street.upper()}:")
                for a in acts:
                    amt = f" ${a['amount']:,}" if a.get("amount") else ""
                    lines.append(f"#   {a['player']}: {a['action']}{amt}")
        board = pt4.get("board") or {}
        if board.get("flop"):
            lines.append(f"# BOARD: {' '.join(board['flop'])} {board.get('turn','') or ''} {board.get('river','') or ''}".rstrip())
        for sd in (gemini_data.get("showdown") or []):
            hc = sd.get("hole_cards") or []
            res = sd.get("result") or ""
            lines.append(f"# SHOWDOWN: {sd['player']} [{' '.join(hc)}] — {res}")
        pot = gemini_data.get("pot") or 0
        if pot:
            lines.append(f"# POT: ${pot:,}")
        return "\n".join(lines) + "\n"

    # ── Single hand processing ────────────────────────────────────────────────

    def process_hand(
        self,
        hand_num: int,
        start_sec: int,
        end_sec: int,
        max_retries: int = 2,
        enable_multipass: bool = False,
        local_file_path: str | None = None,
    ) -> tuple[str | None, dict | None, str | None]:
        """
        Analyze one hand with Gemini and run it through the full pipeline.

        When feed_hand fails with a recoverable error (missing action,
        broken state machine), re-query Gemini with the error message
        prepended to the prompt so it re-watches the video and fills
        the gap. Up to `max_retries` retries are attempted before
        giving up on the hand.

        Returns (hhtext, gemini_data, error_msg).
        """
        hand_offset = hand_num - 1
        btn_seat    = self.session.get_button_seat(hand_offset)

        print(f"\n{'─'*60}")
        print(f"Hand {hand_num}  [{start_sec}s – {end_sec}s]  BTN=seat {btn_seat}")
        print(f"{'─'*60}")

        base_prompt = self._build_prompt(hand_num, start_sec, end_sec)

        last_err: str | None = None
        last_gdata: dict | None = None
        last_was_api_error = False  # True when last failure was transient (503)

        for attempt in range(max_retries + 1):
            # ── Step 1: Gemini analysis ───────────────────────────────────────
            if attempt == 0:
                prompt = base_prompt
                print("Querying Gemini...", end=" ", flush=True)
            elif last_was_api_error:
                # Transient failure — re-use the same prompt, not the error-feedback one
                prompt = base_prompt
                print(f"Retry {attempt}/{max_retries}: Re-querying after API error...", end=" ", flush=True)
            else:
                prompt = self._build_retry_prompt(base_prompt, last_err or "unknown error")
                print(f"Retry {attempt}/{max_retries}: Querying Gemini with error feedback...", end=" ", flush=True)

            try:
                raw = self._call_gemini(start_sec, end_sec, prompt, local_file_path=local_file_path)
                last_was_api_error = False
            except Exception as e:
                err = f"Gemini API error: {e}"
                print(f"FAILED\n  {err}")
                # Transient errors: retry with backoff.
                # 503 UNAVAILABLE = server overload (15s wait)
                # 429 RESOURCE_EXHAUSTED per-minute = rate limit (use retryDelay if given)
                err_str = str(e)
                is_transient = "503" in err_str or "UNAVAILABLE" in err_str
                is_rate_limit = "429" in err_str or "RESOURCE_EXHAUSTED" in err_str
                if is_transient or is_rate_limit:
                    if is_rate_limit and "retryDelay" in err_str:
                        import re as _re
                        m = _re.search(r"'retryDelay':\s*'(\d+)s'", err_str)
                        delay = int(m.group(1)) + 5 if m else 60
                    else:
                        delay = 15
                    last_err = err
                    last_was_api_error = True
                    if attempt < max_retries:
                        print(f"  Waiting {delay}s before retry...")
                        time.sleep(delay)
                    continue
                return None, last_gdata, err
            print("OK")

            # ── Step 2: Parse + convert ───────────────────────────────────────
            try:
                gemini_data = self._parse_response(raw)
            except ValueError as e:
                last_err = f"JSON parse error: {e}"
                last_gdata = None
                print(f"  {last_err}")
                continue   # try a retry if budget allows

            gemini_data = _sanitize_gemini_data(gemini_data)
            last_gdata = gemini_data

            print(f"  Players: {[p['name'] for p in gemini_data.get('players') or []]}")
            if gemini_data.get("notes"):
                print(f"  Notes  : {gemini_data['notes'][:120]}")

            # ── Step 2b: Detect null board with post-flop actions ─────────────
            _gboard = gemini_data.get("board") or {}
            _gaction = gemini_data.get("action") or {}
            _has_postflop = any(_gaction.get(s) for s in ("flop", "turn", "river"))
            _has_board = any(_gboard.get(s) for s in ("flop", "turn", "river"))
            if _has_postflop and not _has_board:
                last_err = (
                    "Post-flop actions exist but all board cards are null. "
                    "Please provide the actual flop, turn, and river cards you saw in the video."
                )
                print(f"  [warn] {last_err}")
                continue

            # ── Step 2c: Multi-pass verification (Pass 2 + optional Pass 3) ────
            # Run on the first attempt that produces valid data, whether that's
            # attempt 0 or a retry after transient 503 errors.
            if enable_multipass and last_was_api_error is False and not getattr(self, "_multipass_ran", False):
                pass1_raw = gemini_data
                data2 = self._run_verify_pass(hand_num, start_sec, end_sec, pass1_raw)
                if data2 is not None:
                    diffs12 = _diff_hand_data(pass1_raw, data2)
                    _print_diffs(diffs12, "Pass 1", "Pass 2")
                    if diffs12:
                        # Pass 3 — arbitrate the specific disagreements
                        data3 = self._run_arbitrate_pass(
                            hand_num, start_sec, end_sec, diffs12, pass1_raw, data2
                        )
                        if data3 is not None:
                            diffs23 = _diff_hand_data(data2, data3)
                            _print_diffs(diffs23, "Pass 2", "Pass 3")
                            # Fields where 2 of 3 passes agree become authoritative
                            # (for simplicity: use data3 as it saw both prior attempts)
                            gemini_data = data3
                        else:
                            gemini_data = data2   # arbitration failed, fall back to Pass 2
                    else:
                        gemini_data = data2   # passes agree — use the verified data
                    last_gdata = gemini_data

            hand_json = self._to_hand_json(gemini_data, hand_num)

            # ── Step 3: State machine ─────────────────────────────────────────
            try:
                pt4 = feed_hand(
                    hand_json,
                    bb_ante=self.session.bb_ante,
                    stakes=self.session.stakes,
                    button_seat=btn_seat,
                )
            except FeedError as e:
                last_err = f"feed_hand error: {e}"
                print(f"  {last_err}")
                continue   # retry with error feedback
            except Exception as e:
                last_err = f"feed_hand unexpected: {e}"
                print(f"  {last_err}")
                continue

            # ── Step 4: Format ────────────────────────────────────────────────
            try:
                hhtext = format_hand(
                    pt4,
                    stream_url=self.youtube_url,
                    hand_index=hand_num,
                    start_sec=start_sec,
                    end_sec=end_sec,
                )
                print(f"  Formatted: {len(hhtext)} chars{' (after retry)' if attempt > 0 else ''}")
                return hhtext, gemini_data, None
            except HandValidationError as exc:
                exc_str = str(exc)
                if "No winner" in exc_str:
                    # Run-it-twice or ambiguous winner — emit as partial hand.
                    # Don't retry; the data was complete enough to walk every
                    # street, the format step just can't pick a single winner.
                    hhtext = self._partial_summary(pt4, hand_num, gemini_data, exc_str)
                    print(f"  Partial (no winner): {len(hhtext)} chars")
                    return hhtext, gemini_data, None
                last_err = f"format_hand error: {exc}"
                print(f"  {last_err}")
                continue
            except Exception as e:
                last_err = f"format_hand unexpected: {e}"
                print(f"  {last_err}")
                continue

        # All retries exhausted.
        print(f"  Hand {hand_num} failed after {max_retries + 1} attempt(s)")
        return None, last_gdata, last_err

    # ── Batch runner ──────────────────────────────────────────────────────────

    def run(
        self,
        hand_windows: list[tuple[int, int]],
        output_path: str,
        gemini_json_path: str | None = None,
        enable_multipass: bool = False,
    ) -> list[str]:
        """
        Process a list of (start_sec, end_sec) windows and write output.

        Returns list of successful hand history strings.
        """
        all_texts: list[str] = []
        all_gemini: list[dict] = []
        errors: list[str] = []

        for i, (start_sec, end_sec) in enumerate(hand_windows, start=1):
            hhtext, gdata, err = self.process_hand(i, start_sec, end_sec, enable_multipass=enable_multipass)
            if gdata:
                all_gemini.append(gdata)
            if hhtext:
                all_texts.append(hhtext)
            elif err:
                errors.append(f"Hand {i}: {err}")

        # Save Gemini raw data
        if gemini_json_path and all_gemini:
            Path(gemini_json_path).parent.mkdir(parents=True, exist_ok=True)
            with open(gemini_json_path, "w", encoding="utf-8") as f:
                json.dump(all_gemini, f, indent=2)
            print(f"\nGemini data saved to {gemini_json_path}")

        # Write hand histories
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            for hhtext in all_texts:
                f.write(hhtext)
                # Ensure blank-line separation between hands
                if not hhtext.endswith("\n\n"):
                    f.write("\n\n")

        # ── Pass rate summary ────────────────────────────────────────────────
        total = len(hand_windows)
        passed = len(all_texts)
        failed = len(errors)
        rate = (passed / total * 100) if total else 0.0
        print(f"\n{'='*60}")
        print(f"PASS RATE: {passed}/{total} ({rate:.1f}%)")
        print(f"{'='*60}")
        print(f"Wrote {passed} hand(s) to {output_path}")
        if errors:
            print(f"Failures ({failed}):")
            for e in errors:
                print(f"  {e}")

        return all_texts


# ── CLI ───────────────────────────────────────────────────────────────────────

def _replay_from_cache(
    pipeline: "GeminiPipeline",
    cache_path: str,
    output_path: str,
    gemini_json: str,
    enable_multipass: bool = False,
    multipass_hands: list[int] | None = None,
) -> list[str]:
    """Re-process cached Gemini JSON. With enable_multipass=True, runs Pass 2+3 via fresh
    Gemini calls using the cached data as Pass 1. multipass_hands limits which hand numbers
    get the multipass treatment (None = all)."""
    with open(cache_path, encoding="utf-8") as f:
        cached = json.load(f)

    all_texts: list[str] = []
    errors: list[str] = []

    for raw_data in cached:
        hand_num    = int(raw_data.get("hand_num") or 0)
        hand_offset = hand_num - 1
        btn_seat    = pipeline.session.get_button_seat(hand_offset)

        print(f"\n{'─'*60}")
        print(f"Hand {hand_num}  [replaying from cache]  BTN=seat {btn_seat}")
        print(f"{'─'*60}")

        gemini_data = _sanitize_gemini_data(raw_data)
        print(f"  Players: {[p['name'] for p in gemini_data.get('players') or []]}")
        if gemini_data.get("notes"):
            print(f"  Notes  : {gemini_data['notes'][:120]}")

        # ── Optional multipass: verify cached data with fresh Gemini calls ────
        run_mp = enable_multipass and (multipass_hands is None or hand_num in multipass_hands)
        if run_mp:
            hand_windows_map = {
                1: (2000, 2120), 2: (2120, 2300), 3: (2300, 2520),
                4: (2520, 2700), 5: (2700, 2880), 6: (2880, 3060),
                7: (3060, 3240), 8: (3240, 3420), 9: (3420, 3600), 10: (3600, 3780),
            }
            start_sec, end_sec = hand_windows_map.get(hand_num, (0, 180))
            pass1_data = gemini_data

            data2 = pipeline._run_verify_pass(hand_num, start_sec, end_sec, pass1_data)
            if data2 is not None:
                diffs12 = _diff_hand_data(pass1_data, data2)
                _print_diffs(diffs12, "Pass 1 (cached)", "Pass 2")
                if diffs12:
                    data3 = pipeline._run_arbitrate_pass(
                        hand_num, start_sec, end_sec, diffs12, pass1_data, data2
                    )
                    if data3 is not None:
                        diffs23 = _diff_hand_data(data2, data3)
                        _print_diffs(diffs23, "Pass 2", "Pass 3")
                        gemini_data = data3
                    else:
                        gemini_data = data2
                else:
                    gemini_data = data2

        hand_json = pipeline._to_hand_json(gemini_data, hand_num)

        try:
            pt4 = feed_hand(
                hand_json,
                bb_ante=pipeline.session.bb_ante,
                stakes=pipeline.session.stakes,
                button_seat=btn_seat,
            )
        except FeedError as e:
            err = f"feed_hand error: {e}"
            print(f"  {err}")
            errors.append(f"Hand {hand_num}: {err}")
            continue
        except Exception as e:
            err = f"feed_hand unexpected: {e}"
            print(f"  {err}")
            errors.append(f"Hand {hand_num}: {err}")
            continue

        try:
            hhtext = format_hand(pt4, stream_url=pipeline.youtube_url, hand_index=hand_num)
            print(f"  Formatted: {len(hhtext)} chars")
            all_texts.append(hhtext)
        except HandValidationError as exc:
            if "No winner" in str(exc):
                hhtext = pipeline._partial_summary(pt4, hand_num, gemini_data, str(exc))
                print(f"  Partial (no winner): {len(hhtext)} chars")
                all_texts.append(hhtext)
            else:
                err = f"format_hand error: {exc}"
                print(f"  {err}")
                errors.append(f"Hand {hand_num}: {err}")
        except Exception as e:
            err = f"format_hand unexpected: {e}"
            print(f"  {err}")
            errors.append(f"Hand {hand_num}: {err}")

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        for hhtext in all_texts:
            f.write(hhtext)
            if not hhtext.endswith("\n\n"):
                f.write("\n\n")

    print(f"\nWrote {len(all_texts)} hand(s) to {output_path}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors:
            print(f"  {e}")
    return all_texts


def main() -> None:
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    config_path = "src/session/session_config.json"
    output_path = "output/hands_gemini.txt"
    gemini_json = "output/hands_gemini_raw.json"

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)

    youtube_url = cfg.get("stream_url", "")
    if not youtube_url:
        print("stream_url not set in session_config.json")
        sys.exit(1)

    pipeline = GeminiPipeline(
        youtube_url=youtube_url,
        session_config_path=config_path,
    )

    args = sys.argv[1:]
    multipass   = "--multipass" in args
    # --multipass-replay N [N ...] runs Pass 2+3 against cached Pass 1 data for
    # the listed hand numbers (or all cached hands if no numbers given)
    mp_replay   = "--multipass-replay" in args
    mp_hands: list[int] | None = None
    if mp_replay:
        idx = args.index("--multipass-replay")
        nums = []
        for tok in args[idx + 1:]:
            if tok.startswith("--"):
                break
            try:
                nums.append(int(tok))
            except ValueError:
                pass
        mp_hands = nums or None

    hand_windows = [
        (2000, 2120),   # Hand 1
        (2120, 2300),   # Hand 2
        (2300, 2520),   # Hand 3
        (2520, 2700),   # Hand 4
        (2700, 2880),   # Hand 5
        (2880, 3060),   # Hand 6
        (3060, 3240),   # Hand 7
        (3240, 3420),   # Hand 8
        (3420, 3600),   # Hand 9
        (3600, 3780),   # Hand 10
    ]

    # --hand N [N ...] restricts the run to specific hand numbers
    single_hands: list[int] | None = None
    if "--hand" in args:
        idx = args.index("--hand")
        nums = []
        for tok in args[idx + 1:]:
            if tok.startswith("--"):
                break
            try:
                nums.append(int(tok))
            except ValueError:
                pass
        single_hands = nums or None

    # --local-file PATH  — analyze a local video file (entire file = one hand)
    local_file: str | None = None
    if "--local-file" in args:
        idx = args.index("--local-file")
        if idx + 1 < len(args):
            local_file = args[idx + 1]

    # Auto-replay from cache unless the user explicitly asked for fresh queries
    # (--multipass or --fresh forces a new Gemini run even if a cache exists)
    force_fresh = multipass or "--fresh" in args
    replay = ("--replay" in args or mp_replay) or (not force_fresh and Path(gemini_json).exists())

    if local_file:
        # Single-hand analysis from local file (bypasses YouTube URL & time offsets)
        hand_num = (single_hands[0] if single_hands else 1)
        start_sec, end_sec = hand_windows[hand_num - 1]
        print(f"Analyzing Hand {hand_num} from local file: {local_file}")
        pipeline._local_file_path = local_file
        hhtext, gdata, err = pipeline.process_hand(
            hand_num, start_sec, end_sec,
            enable_multipass=multipass,
            local_file_path=local_file,
        )
        if hhtext:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(hhtext, encoding="utf-8")
            if gdata:
                all_gemini = [gdata]
                Path(gemini_json).parent.mkdir(parents=True, exist_ok=True)
                with open(gemini_json, "w", encoding="utf-8") as f:
                    json.dump(all_gemini, f, indent=2)
            texts = [hhtext]
        else:
            print(f"  Error: {err}")
            texts = []
    elif replay and Path(gemini_json).exists():
        print(f"Replaying from cache: {gemini_json}")
        if mp_replay:
            hand_label = f"hands {mp_hands}" if mp_hands else "all cached hands"
            print(f"Multi-pass verification enabled for {hand_label}")
        texts = _replay_from_cache(
            pipeline, gemini_json, output_path, gemini_json,
            enable_multipass=mp_replay,
            multipass_hands=mp_hands,
        )
    else:
        windows = hand_windows
        if single_hands:
            windows = [hand_windows[n - 1] for n in single_hands if 1 <= n <= len(hand_windows)]
        texts = pipeline.run(
            hand_windows=windows,
            output_path=output_path,
            gemini_json_path=gemini_json,
            enable_multipass=multipass,
        )

    if not texts:
        print("\nNo hands produced — nothing to lint.")
        sys.exit(1)

    # ── Lint the output ──────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("LINTING output/hands_gemini.txt")
    print("=" * 60)

    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "src.validate.pt4_linter", output_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    print(result.stdout)
    if result.stderr:
        print(result.stderr[:500])


if __name__ == "__main__":
    main()
