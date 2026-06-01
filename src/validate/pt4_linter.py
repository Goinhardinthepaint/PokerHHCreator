"""
src/validate/pt4_linter.py

Lints a PokerStars-format hand history .txt file against PT4 import rules.

Usage:
    python -m src.validate.pt4_linter output/hands_fullframe.txt
    python -m src.validate.pt4_linter output/hands_fullframe.txt --out output/hands_validated.txt

Output:
    PASS/FAIL per hand with specific rule violations printed to stdout.
    Passing hands written to --out (default: <input>_validated.txt).
"""

from __future__ import annotations

import re
import sys
import os
import argparse
from dataclasses import dataclass, field


# ── Card validation ────────────────────────────────────────────────────────────

_VALID_RANKS = set("AKQJT98765432")
_VALID_SUITS = set("shdc")
_CARD_RE     = re.compile(r"\b([2-9AKQJT][shdc])\b")


def _valid_card(c: str) -> bool:
    return len(c) == 2 and c[0] in _VALID_RANKS and c[1] in _VALID_SUITS


def _cards_in(text: str) -> list[str]:
    """Extract all card tokens from a line (e.g. 'Ah 2c Kd')."""
    return _CARD_RE.findall(text)


# ── Hand block splitting ───────────────────────────────────────────────────────

def _split_hands(raw: str) -> list[str]:
    """Split raw file text into individual hand blocks."""
    blocks: list[str] = []
    current: list[str] = []
    for line in raw.splitlines():
        if line.startswith("PokerStars Hand #") and current:
            blocks.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current))
    return [b for b in blocks if b.strip()]


# ── Rule checks ────────────────────────────────────────────────────────────────

@dataclass
class LintResult:
    hand_num: int
    header: str
    violations: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return len(self.violations) == 0

    def fail(self, msg: str) -> None:
        self.violations.append(msg)


def lint_hand(block: str, hand_num: int) -> LintResult:
    lines = block.splitlines()
    res   = LintResult(hand_num=hand_num, header=lines[0] if lines else "")

    # ── Split sections ────────────────────────────────────────────────────────
    section = "header"
    header_lines: list[str]   = []
    preflop_lines: list[str]  = []
    postflop_lines: list[str] = []
    showdown_lines: list[str] = []
    summary_lines: list[str]  = []

    for line in lines:
        if line.startswith("*** HOLE CARDS ***"):
            section = "preflop"
            continue
        if line.startswith("*** FLOP ***") or line.startswith("*** TURN ***") or line.startswith("*** RIVER ***"):
            section = "postflop"
            postflop_lines.append(line)
            continue
        if (line.startswith("*** SHOW DOWN ***")
                or line.startswith("*** FIRST SHOW DOWN ***")
                or line.startswith("*** SECOND SHOW DOWN ***")):
            section = "showdown"
            continue
        if line.startswith("*** SUMMARY ***"):
            section = "summary"
            continue
        if section == "header":
            header_lines.append(line)
        elif section == "preflop":
            preflop_lines.append(line)
        elif section == "postflop":
            postflop_lines.append(line)
        elif section == "showdown":
            showdown_lines.append(line)
        elif section == "summary":
            summary_lines.append(line)

    # ── Rule 1: Header starts with "PokerStars Hand #" ────────────────────────
    if not lines or not lines[0].startswith("PokerStars Hand #"):
        res.fail("R1: header does not start with 'PokerStars Hand #'")

    # ── Rule 2: Every seat line in header ends with "in chips)" ───────────────
    seat_players: list[str] = []
    for line in header_lines:
        m = re.match(r"^Seat \d+: (.+?) \(\$[\d]+ in chips\)$", line)
        if m:
            seat_players.append(m.group(1))
        elif re.match(r"^Seat \d+:", line) and "in chips" not in line and "is the button" not in line:
            res.fail(f"R2: seat line malformed: {line!r}")

    # ── Rule 11: No commas in dollar amounts (check all lines) ────────────────
    comma_re = re.compile(r"\$[\d,]*,[\d,]+")
    for line in lines:
        if comma_re.search(line):
            res.fail(f"R11: comma in dollar amount: {line.strip()!r}")
            break

    # ── Parse blind/ante postings from header + preflop ───────────────────────
    all_prehand = header_lines + preflop_lines
    posting_order: list[tuple[str, str]] = []  # (player, type)
    sb_player: str | None = None
    bb_player: str | None = None

    for line in all_prehand:
        if ": posts the ante" in line:
            name = line.split(":")[0].strip()
            posting_order.append((name, "ante"))
        elif ": posts small blind" in line:
            name = line.split(":")[0].strip()
            posting_order.append((name, "sb"))
            if sb_player is None:
                sb_player = name
        elif ": posts big blind" in line:
            name = line.split(":")[0].strip()
            posting_order.append((name, "bb"))
            if bb_player is None:
                bb_player = name
        elif ": posts straddle" in line:
            name = line.split(":")[0].strip()
            posting_order.append((name, "straddle"))

    # ── Rule 3: SB and BB are different players ────────────────────────────────
    if sb_player and bb_player and sb_player.casefold() == bb_player.casefold():
        res.fail(f"R3: SB and BB are the same player ({sb_player!r})")

    # ── Rule 4: SB posting comes before BB posting ────────────────────────────
    types = [t for _, t in posting_order]
    if "sb" in types and "bb" in types:
        if types.index("sb") > types.index("bb"):
            res.fail("R4: BB posted before SB")

    # ── Rule 5: Antes come before blinds ─────────────────────────────────────
    if "ante" in types and "sb" in types:
        last_ante = max(i for i, t in enumerate(types) if t == "ante")
        first_sb  = min(i for i, t in enumerate(types) if t == "sb")
        if last_ante > first_sb:
            res.fail("R5: ante posted after blind")

    # ── Rule 6: No player acts after folding ─────────────────────────────────
    folded: set[str] = set()
    action_lines = preflop_lines + postflop_lines
    for line in action_lines:
        m = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): (.+)", line)
        if not m:
            continue
        player, act = m.group(1).strip(), m.group(2).strip()
        if act == "folds":
            folded.add(player.casefold())
        elif player.casefold() in folded:
            skip_keywords = ("posts", "Dealt to")
            if not any(line.startswith(k) for k in skip_keywords):
                res.fail(f"R6: {player!r} acts after folding: {line.strip()!r}")

    # ── Rule 7: No duplicate cards ────────────────────────────────────────────
    # Collect: dealt hole cards (one set per player) + board cards only.
    # Showdown / summary lines intentionally repeat the same cards — don't
    # count those or they create false positives.
    hole_cards: dict[str, list[str]] = {}   # player → cards
    board_cards: list[str] = []
    for line in lines:
        dealt_m = re.match(r"^Dealt to (.+?) \[([^\]]+)\]", line)
        if dealt_m:
            pname = dealt_m.group(1).strip()
            hole_cards[pname] = _cards_in(dealt_m.group(2))
            continue
        # Board cards from street headers only (not summary Board line)
        if re.match(r"^\*\*\* (?:FLOP|TURN|RIVER) \*\*\*", line):
            bm = re.search(r"\[([^\]]+)\]", line)
            if bm:
                board_cards.extend(_cards_in(bm.group(1)))
    # Check inter-player duplicates
    flat: list[str] = board_cards[:]
    for pname, cards in hole_cards.items():
        for c in cards:
            if c in flat:
                ctx = "board" if c in board_cards else next(
                    (p for p, cs in hole_cards.items() if c in cs and p != pname), "?"
                )
                res.fail(f"R7: card {c} dealt to {pname!r} also appears in {ctx}")
            flat.append(c)

    # ── Rule 8: Every hand has a winner (collected line) ─────────────────────
    collected_re = re.compile(r"collected \$\d+ from pot")
    if not any(collected_re.search(line) for line in lines):
        res.fail("R8: no 'collected $X from pot' line found")

    # ── Rule 9: Pot total > 0 ─────────────────────────────────────────────────
    pot_m = re.search(r"Total pot \$(\d+)", "\n".join(summary_lines))
    if pot_m:
        if int(pot_m.group(1)) == 0:
            res.fail("R9: Total pot is $0")
    else:
        res.fail("R9: 'Total pot' line not found in summary")

    # ── Rule 10: All card tokens are valid 2-char ─────────────────────────────
    for line in lines:
        for m in re.finditer(r"\[([^\]]+)\]", line):
            for tok in m.group(1).split():
                if not _valid_card(tok):
                    res.fail(f"R10: invalid card token {tok!r} in: {line.strip()!r}")

    # ── Rule 12: "| Rake $0" present ─────────────────────────────────────────
    if not any("| Rake $0" in line for line in summary_lines):
        res.fail("R12: '| Rake $0' not found in summary")

    # ── Rule 13: Board line present if postflop action exists ─────────────────
    has_postflop_action = any(
        re.match(r"^[A-Za-z].+?: (bets|calls|raises|checks|folds)", line)
        for line in postflop_lines
    )
    has_board_line = any(line.startswith("Board [") for line in summary_lines)
    if has_postflop_action and not has_board_line:
        res.fail("R13: postflop actions exist but no 'Board [...]' in summary")

    # ── Rule 14: No empty "showed [] and won" lines ───────────────────────────
    for line in lines:
        if re.search(r"showed \[\]", line):
            res.fail(f"R14: empty showed cards: {line.strip()!r}")

    # ── Rule 15: Raise format "raises $X to $Y" where Y > X ──────────────────
    # All-in raises may have Y == X (full stack committed); skip those.
    raise_re = re.compile(r"raises \$(\d+) to \$(\d+)")
    for line in lines:
        if "and is all-in" in line:
            continue
        m = raise_re.search(line)
        if m:
            x, y = int(m.group(1)), int(m.group(2))
            if y <= x:
                res.fail(f"R15: raise to ${y} not > raise-by ${x}: {line.strip()!r}")

    # ── Rule 16: Summary has entry for every seated player ────────────────────
    _STOP_WORDS = frozenset({"folded", "showed", "collected", "mucked", "and", "didn't"})
    summary_seats = set()
    for line in summary_lines:
        sm = re.match(r"^Seat \d+: (.+)", line)
        if not sm:
            continue
        tokens = sm.group(1).split()
        name_tokens: list[str] = []
        for tok in tokens:
            if tok.startswith("(") or tok.casefold() in _STOP_WORDS:
                break
            name_tokens.append(tok)
        if name_tokens:
            summary_seats.add(" ".join(name_tokens).casefold())
    seated_cf = {p.casefold() for p in seat_players}
    missing = seated_cf - summary_seats
    if missing:
        res.fail(f"R16: seated players missing from summary: {sorted(missing)}")

    # ── Parse button player (needed for R22) ──────────────────────────────────
    btn_seat_num: int | None = None
    btn_player:   str | None = None
    for line in header_lines:
        bm = re.search(r"Seat #(\d+) is the button", line)
        if bm:
            btn_seat_num = int(bm.group(1))
            break
    if btn_seat_num is not None:
        for line in header_lines:
            bm = re.match(rf"^Seat {btn_seat_num}: (.+?) \(", line)
            if bm:
                btn_player = bm.group(1).strip()
                break

    # ── Rule 17: Folded player appears in SHOW DOWN ───────────────────────────
    _show_sd_re = re.compile(r"^([A-Za-z][A-Za-z0-9 _\-']+?): shows \[")
    for line in showdown_lines:
        m17 = _show_sd_re.match(line)
        if m17:
            _p17 = m17.group(1).strip()
            if _p17.casefold() in folded:
                res.fail(f"R17: {_p17!r} folded but appears in SHOW DOWN: {line.strip()!r}")

    # ── Rule 18: Empty showdown — showed with no cards between showed and and ──
    _empty_sd_re = re.compile(r"showed\s+and\s+(won|lost)", re.IGNORECASE)
    for line in lines:
        if _empty_sd_re.search(line):
            res.fail(f"R18: empty showdown (no cards): {line.strip()!r}")
            break

    # ── Rule 19: Flush/straight claimed with < 5 board cards ─────────────────
    _n_board = 0
    for line in summary_lines:
        if line.startswith("Board ["):
            bm19 = re.search(r"\[([^\]]+)\]", line)
            if bm19:
                _n_board = len(_cards_in(bm19.group(1)))
            break
    if _n_board < 5:
        _hand_claim_re = re.compile(r"\b(flush|straight)\b", re.IGNORECASE)
        for line in showdown_lines + summary_lines:
            if _hand_claim_re.search(line):
                res.fail(
                    f"R19: board has {_n_board} card(s) but flush/straight"
                    f" claimed: {line.strip()!r}"
                )
                break

    # ── Rule 20: Uncalled bet returned to X, but someone else collected ───────
    _uncalled_re20 = re.compile(r"^Uncalled bet \(\$[\d,]+\) returned to (.+)$")
    _uncalled_to: str | None = None
    for line in lines:
        mu = _uncalled_re20.match(line)
        if mu:
            _uncalled_to = mu.group(1).strip()
            break
    if _uncalled_to:
        for line in lines:
            mc = re.match(r"^(.+?) collected \$", line)
            if mc:
                _collector = mc.group(1).strip()
                if _collector.casefold() != _uncalled_to.casefold():
                    _allin_pat = re.compile(
                        r"^" + re.escape(_collector) + r": .+and is all-in"
                    )
                    if not any(_allin_pat.match(ln) for ln in lines):
                        res.fail(
                            f"R20: uncalled bet to {_uncalled_to!r} but"
                            f" {_collector!r} collected (not all-in)"
                        )
                break

    # ── Rule 21: Action after all active players checked on a street ──────────
    def _chk_street(name: str, acts: list[str], active: set[str]) -> str | None:
        checked:   set[str] = set()
        remaining: set[str] = set(active)
        closed = False
        for ln in acts:
            mo = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): (.+)", ln)
            if not mo:
                continue
            pcf = mo.group(1).strip().casefold()
            act = mo.group(2).strip()
            if pcf not in remaining:
                continue
            if closed:
                return f"R21: action after all players checked on {name}: {ln.strip()!r}"
            is_allin = "and is all-in" in act
            if act.startswith("check"):
                checked.add(pcf)
                if checked >= remaining:
                    closed = True
            elif re.match(r"bet|raise", act):
                checked.clear()
                if is_allin:
                    remaining.discard(pcf)
            elif act.startswith("call") and is_allin:
                remaining.discard(pcf)
            elif act == "folds":
                remaining.discard(pcf)
                checked.discard(pcf)
        return None

    _fl_acts: list[str] = []
    _tu_acts: list[str] = []
    _ri_acts: list[str] = []
    _cur_st: list[str] | None = None
    for _pl in postflop_lines:
        if _pl.startswith("*** FLOP ***"):
            _cur_st = _fl_acts
        elif _pl.startswith("*** TURN ***"):
            _cur_st = _tu_acts
        elif _pl.startswith("*** RIVER ***"):
            _cur_st = _ri_acts
        elif _cur_st is not None:
            _cur_st.append(_pl)

    def _folds_of(act_lines: list[str]) -> set[str]:
        s: set[str] = set()
        for _l in act_lines:
            _m = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): folds", _l)
            if _m:
                s.add(_m.group(1).strip().casefold())
        return s

    def _allins_of(act_lines: list[str]) -> set[str]:
        s: set[str] = set()
        for _l in act_lines:
            if "and is all-in" in _l:
                _m = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): ", _l)
                if _m:
                    s.add(_m.group(1).strip().casefold())
        return s

    _folds_pf  = _folds_of(preflop_lines)
    _allins_pf = _allins_of(preflop_lines)
    _active_fl = seated_cf - _folds_pf  - _allins_pf
    _active_tu = _active_fl - _folds_of(_fl_acts) - _allins_of(_fl_acts)
    _active_ri = _active_tu - _folds_of(_tu_acts) - _allins_of(_tu_acts)

    for _sname, _sacts, _sactive in [
        ("preflop", preflop_lines, seated_cf),
        ("Flop",    _fl_acts,      _active_fl),
        ("Turn",    _tu_acts,      _active_tu),
        ("River",   _ri_acts,      _active_ri),
    ]:
        if _sacts:
            _v21 = _chk_street(_sname, _sacts, _sactive)
            if _v21:
                res.fail(_v21)

    # ── Rule 22: Preflop action order — BTN acts before SB in non-HU ──────────
    if (btn_player and sb_player
            and btn_player.casefold() != sb_player.casefold()
            and len(seat_players) > 2):
        _act22_re = re.compile(
            r"^([A-Za-z][A-Za-z0-9 _\-']+?): (?:folds|checks|calls|bets|raises)"
        )
        _btn22_idx: int | None = None
        _sb22_idx:  int | None = None
        for _i22, _ln22 in enumerate(preflop_lines):
            _m22 = _act22_re.match(_ln22)
            if not _m22:
                continue
            _pcf22 = _m22.group(1).strip().casefold()
            if _btn22_idx is None and _pcf22 == btn_player.casefold():
                _btn22_idx = _i22
            if _sb22_idx is None and _pcf22 == sb_player.casefold():
                _sb22_idx = _i22
        if (_btn22_idx is not None and _sb22_idx is not None
                and _sb22_idx < _btn22_idx):
            res.fail(
                f"R22: SB ({sb_player!r}) acts before BTN ({btn_player!r}) preflop"
            )

    # ── Rule 23: No consecutive same-player actions on any street ─────────────
    def _check_consecutive_23(street_name: str, act_lines: list[str]) -> str | None:
        last_cf: str | None = None
        for _ln in act_lines:
            _mo = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): (.+)", _ln)
            if not _mo:
                continue
            _pcf = _mo.group(1).strip().casefold()
            if _pcf == last_cf:
                return (
                    f"R23: {_mo.group(1).strip()!r} acts twice consecutively "
                    f"on {street_name}: {_ln.strip()!r}"
                )
            last_cf = _pcf
        return None

    for _sn23, _sa23 in [
        ("preflop", preflop_lines),
        ("Flop",    _fl_acts),
        ("Turn",    _tu_acts),
        ("River",   _ri_acts),
    ]:
        _v23 = _check_consecutive_23(_sn23, _sa23)
        if _v23:
            res.fail(_v23)

    # ── Rule 24: Illegal check after raise in preflop-only hand ───────────────
    _has_flop_24 = any(ln.startswith("*** FLOP ***") for ln in postflop_lines)
    if not _has_flop_24:
        _raise_seen_24 = False
        for _ln24 in preflop_lines:
            _m24 = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): (.+)", _ln24)
            if not _m24:
                continue
            _act24 = _m24.group(2).strip()
            if _act24.startswith("raises") or _act24.startswith("bets"):
                _raise_seen_24 = True
            elif _raise_seen_24 and _act24.startswith("checks"):
                res.fail(
                    f"R24: illegal check after raise in preflop-only hand: {_ln24.strip()!r}"
                )
                break

    # ── Rule 25: Cannot fold after calling with no subsequent raise ────────────
    def _check_fold_after_call_25(street_name: str, act_lines: list[str]) -> str | None:
        called_no_raise: set[str] = set()
        for _ln in act_lines:
            _mo = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): (.+)", _ln)
            if not _mo:
                continue
            _pcf = _mo.group(1).strip().casefold()
            _pname = _mo.group(1).strip()
            _act = _mo.group(2).strip()
            if _act.startswith("calls"):
                called_no_raise.add(_pcf)
            elif _act.startswith("raises") or _act.startswith("bets"):
                called_no_raise.clear()
            elif _act == "folds" and _pcf in called_no_raise:
                return (
                    f"R25: {_pname!r} called on {street_name} and folded "
                    f"without a subsequent raise: {_ln.strip()!r}"
                )
        return None

    for _sn25, _sa25 in [
        ("preflop", preflop_lines),
        ("Flop",    _fl_acts),
        ("Turn",    _tu_acts),
        ("River",   _ri_acts),
    ]:
        _v25 = _check_fold_after_call_25(_sn25, _sa25)
        if _v25:
            res.fail(_v25)

    # ── Rule 26: All-in player cannot fold on a subsequent street ─────────────
    _STREETS_26 = [
        ("preflop", preflop_lines),
        ("flop",    _fl_acts),
        ("turn",    _tu_acts),
        ("river",   _ri_acts),
    ]
    _S26_IDX: dict[str, int] = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}
    _allin_on: dict[str, str] = {}   # player_cf → street where they went all-in
    for _sname26, _sacts26 in _STREETS_26:
        for _ln26 in _sacts26:
            if "and is all-in" in _ln26:
                _m26 = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): ", _ln26)
                if _m26:
                    _pcf26 = _m26.group(1).strip().casefold()
                    if _pcf26 not in _allin_on:
                        _allin_on[_pcf26] = _sname26
    for _sname26, _sacts26 in _STREETS_26:
        _sidx26 = _S26_IDX[_sname26]
        for _ln26 in _sacts26:
            _m26 = re.match(r"^([A-Za-z][A-Za-z0-9 _\-']+?): folds", _ln26)
            if _m26:
                _pcf26 = _m26.group(1).strip().casefold()
                _pname26 = _m26.group(1).strip()
                if (_pcf26 in _allin_on
                        and _sidx26 > _S26_IDX[_allin_on[_pcf26]]):
                    res.fail(
                        f"R26: {_pname26!r} went all-in on {_allin_on[_pcf26]}"
                        f" but folds on {_sname26}: {_ln26.strip()!r}"
                    )

    # ── Rule 27: First voluntary action on a postflop street must be bet/check ──
    _ACT_RE27 = re.compile(r"^([A-Za-z][A-Za-z0-9 _\-']+?): (.+)")
    for _sname27, _sacts27 in [("Flop", _fl_acts), ("Turn", _tu_acts), ("River", _ri_acts)]:
        for _ln27 in _sacts27:
            _m27 = _ACT_RE27.match(_ln27)
            if not _m27:
                continue
            _act27 = _m27.group(2).strip()
            if _act27.startswith("raises"):
                res.fail(
                    f"R27: first voluntary action on {_sname27} is 'raises'"
                    f" (must be 'bets' or 'checks'): {_ln27.strip()!r}"
                )
            break   # only check the first player action

    # ── Rule 28: "collected" lines must not use parentheses ──────────────────
    _COLL_PAREN_RE = re.compile(r"\bcollected \(\$\d+\)")
    for _ln28 in lines:
        if _COLL_PAREN_RE.search(_ln28):
            res.fail(f"R28: 'collected' with parentheses: {_ln28.strip()!r}")
            break

    # ── Rule 29: Bet/raise on a street must receive a response before it ends ──
    # A bare "bets $X" or "raises $X to $Y" (not all-in) as the last player
    # action on a street is invalid if no uncalled-bet line follows.
    # An "Uncalled bet" line is the legitimate explanation for an uncontested bet.
    _has_uncalled = any("Uncalled bet" in _lu for _lu in lines)
    _LAST_ACT_RE29 = re.compile(r"^[A-Za-z][A-Za-z0-9 _\-']+?: (bets|raises)\b")
    for _sname29, _sacts29 in [("Flop", _fl_acts), ("Turn", _tu_acts), ("River", _ri_acts)]:
        if not _sacts29:
            continue
        # Find the last player-action line on this street
        _last29 = None
        for _ln29 in reversed(_sacts29):
            if _ACT_RE27.match(_ln29):
                _last29 = _ln29
                break
        if _last29 and _LAST_ACT_RE29.match(_last29) and "and is all-in" not in _last29:
            if not _has_uncalled:
                res.fail(
                    f"R29: last action on {_sname29} is an unanswered bet/raise: "
                    f"{_last29.strip()!r}"
                )

    # ── Rule 30: Street with 2+ active non-all-in players must have actions ────
    # Active sets (_active_fl/_tu/_ri) exclude preflop folds and all-ins; players
    # who entered a street are expected to produce at least one action line.
    _HAS_FLOP30  = any(ln.startswith("*** FLOP ***")  for ln in postflop_lines)
    _HAS_TURN30  = any(ln.startswith("*** TURN ***")  for ln in postflop_lines)
    _HAS_RIVER30 = any(ln.startswith("*** RIVER ***") for ln in postflop_lines)
    _ACT_LINE_RE30 = re.compile(r"^[A-Za-z][A-Za-z0-9 _\-']+?: ")

    for _sname30, _has30, _active30, _sacts30 in [
        ("Flop",  _HAS_FLOP30,  _active_fl, _fl_acts),
        ("Turn",  _HAS_TURN30,  _active_tu, _tu_acts),
        ("River", _HAS_RIVER30, _active_ri, _ri_acts),
    ]:
        if not _has30:
            continue
        if len(_active30) < 2:
            continue   # ≤1 non-all-in player — empty street is valid
        if not any(_ACT_LINE_RE30.match(ln) for ln in _sacts30):
            res.fail(
                f"R30: {_sname30} dealt with {len(_active30)} active players"
                f" but has no action lines"
            )

    # ── Rule 31: Showdown with 2+ card-shows but board has fewer than 5 cards ──
    _show_sd_count = sum(1 for ln in showdown_lines if _show_sd_re.match(ln))
    if _show_sd_count >= 2 and _n_board < 5:
        res.fail(
            f"R31: Showdown with {_show_sd_count} players showing cards but "
            f"board has only {_n_board} card(s) — missing turn/river"
        )

    # ── Rule 32: Empty hand description — trailing "with" in summary ──────────
    _empty_desc_re32 = re.compile(r"\band (won|lost) with\s*$", re.IGNORECASE)
    for _ln32 in summary_lines:
        if _empty_desc_re32.search(_ln32):
            res.fail(f"R32: empty hand description (trailing 'with'): {_ln32.strip()!r}")
            break

    # ── Rule 33: Dealt-to consistency — every showdown shower must be dealt to ─
    _dealt_to_players33: set[str] = set()
    for _ln33 in preflop_lines:
        _m33dt = re.match(r"^Dealt to (.+?) \[", _ln33)
        if _m33dt:
            _dealt_to_players33.add(_m33dt.group(1).strip().casefold())
    for _ln33 in showdown_lines:
        _m33sd = _show_sd_re.match(_ln33)
        if _m33sd:
            _pname33 = _m33sd.group(1).strip()
            if _pname33.casefold() not in _dealt_to_players33:
                res.fail(
                    f"R33: {_pname33!r} shows cards in SHOW DOWN but has no "
                    f"'Dealt to' line in HOLE CARDS: {_ln33.strip()!r}"
                )

    return res


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Lint a PT4-format hand history file"
    )
    parser.add_argument("input", help="Path to .txt hand history file")
    parser.add_argument(
        "--out",
        default=None,
        metavar="PATH",
        help="Output path for validated hands (default: <input>_validated.txt)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: file not found: {args.input}")
        sys.exit(1)

    out_path = args.out or args.input.replace(".txt", "_validated.txt")
    if out_path == args.input:
        out_path = args.input + "_validated.txt"

    with open(args.input, encoding="utf-8") as fh:
        raw = fh.read()

    hands = _split_hands(raw)
    if not hands:
        print("No hands found in file.")
        sys.exit(0)

    print(f"Linting {len(hands)} hand(s) from {args.input}\n")

    passed: list[str] = []
    failed: list[LintResult] = []

    for i, block in enumerate(hands, 1):
        result = lint_hand(block, i)
        tag = "PASS" if result.passed else "FAIL"
        # Show just the hand number and ID from the header
        hdr_short = result.header[:70] if result.header else f"Hand {i}"
        print(f"[{tag}] Hand {i:>3}: {hdr_short}")
        if not result.passed:
            for v in result.violations:
                print(f"         {v}")
            failed.append(result)
        else:
            passed.append(block)

    print(f"\n{'='*60}")
    print(f"  PASSED: {len(passed)} / {len(hands)}")
    print(f"  FAILED: {len(failed)} / {len(hands)}")
    print(f"  Failures: {[r.hand_num for r in failed]}")

    if passed:
        combined = "\n\n\n".join(passed)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(combined)
        print(f"\nCleaned output ({len(passed)} hand(s)) -> {out_path}")
    else:
        print("\nNo hands passed — output file not written.")


if __name__ == "__main__":
    main()
