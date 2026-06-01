"""
src/ocr/overlay_reader.py

Reads a single video frame and returns structured poker overlay data.

The WPT Global / Hustler Casino Live overlay layout (640×360 baseline):
  - Bottom-left  (~45% w, ~45% h): player info boxes stacked vertically
  - Bottom-right (~30% w, ~45% h): POT amount + board cards + stakes
  - Bottom-center (~30% w, ~35% h): board card area (stream-dependent)

Each player box has two rows:
  Row 1 (header):  NAME   Card1 Card2   Win%
  Row 2 (action):  ACTION_TEXT   $STACK   POSITION

Return format::
    {
        "players": [
            {
                "name":     str,    # roster-matched display name
                "action":   str,    # checks/calls/bets/raises/folds/all_in
                "amount":   int,    # bet/call amount (0 for checks/folds)
                "stack":    int,    # displayed stack
                "position": str,    # BTN / SB / BB / CO / HJ / UTG / STR …
                "win_pct":  float,  # equity % (0.0 if not visible)
            },
            ...
        ],
        "pot":         int,        # pot amount
        "board_cards": list[str], # e.g. ["5c", "3d", "Ts"] (0, 3, 4, or 5 items)
        "board_count": int,        # len(board_cards)
    }
"""

from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Optional

import numpy as np
from PIL import Image

# ── EasyOCR singleton ─────────────────────────────────────────────────────────

_reader = None


def _get_reader():
    global _reader
    if _reader is None:
        import easyocr  # lazy import — heavy initialisation
        _reader = easyocr.Reader(["en"], gpu=False, verbose=False)
    return _reader


# ── Roster & layout constants ─────────────────────────────────────────────────

_DEFAULT_ROSTER: list[str] = [
    "Airball", "Francisco", "Dylan", "Greedo", "Otto", "Alex", "Steve",
    "Tom", "Nick", "Eric", "Dave", "Phil", "Mike", "Adam",
]

_VALID_POSITIONS: frozenset[str] = frozenset({
    "BTN", "SB", "BB",
    "UTG", "UTG+1", "UTG+2",
    "HJ", "MP", "CO",
    "STR", "STRADDLE", "EP",
})

# OCR → canonical position
_POSITION_FIXES: dict[str, str] = {
    "BTNS": "BTN", "BIN": "BTN", "BN": "BTN", "8TN": "BTN",
    "58": "SB", "SB.": "SB",
    "H1": "HJ", "HJ.": "HJ",
    "UTG1": "UTG+1", "UTG2": "UTG+2",
}

# Multi-word must come before single-word so they match first
_ACTION_KEYWORDS: list[tuple[str, str]] = [
    ("ALL IN",  "all_in"),
    ("ALL-IN",  "all_in"),
    ("ALLIN",   "all_in"),
    ("3-BET",   "raises"),
    ("3 BET",   "raises"),
    ("3BET",    "raises"),
    ("TO CALL", "calls"),
    ("TOCALL",  "calls"),
    ("RAISE",   "raises"),
    ("RAISES",  "raises"),
    ("CALL",    "calls"),
    ("CALLS",   "calls"),
    ("BET",     "bets"),
    ("BETS",    "bets"),
    ("CHECK",   "checks"),
    ("CHECKS",  "checks"),
    ("FOLD",    "folds"),
    ("FOLDS",   "folds"),
]

# OCR prefixes that are clearly non-name overlay text
_NON_NAME_PREFIXES: tuple[str, ...] = (
    "POT", "BET", "CALL", "RAISE", "CHECK", "FOLD",
    "ALL", "3-B", "3 B", "$", "TO ",
)

# Regex helpers
# Only replace S/5/6 that are NOT preceded by comma, dollar-sign, or digit —
# prevents turning the "5" inside "$16,500" into a second dollar sign.
_DOLLAR_FIX = re.compile(r"(?<![,$\d])[S56](?=\d)")
_OCTO_FIX   = re.compile(r"(?<=\d)[Oo](?=\d)|(?<!\w)[Oo](?=\d)")
_WIN_PCT_RE = re.compile(r"\b(\d{1,3})\s*%")

# Layout fractions (fraction of frame width / height)
_PL_X0, _PL_X1 = 0.00, 0.455   # player area: left 45.5%
_PL_Y0, _PL_Y1 = 0.55,  1.00   # player area: bottom 45%
_POT_X0, _POT_X1 = 0.80, 1.00  # pot/board area — tighter than the 30% spec
_POT_Y0, _POT_Y1 = 0.55, 1.00  # because pot box sits in the rightmost ~20%
_BCARD_X0 = 0.70                # board-card crop: slightly wider than pot crop
_BOARD_X0, _BOARD_X1 = 0.35, 0.65  # board-center area
_BOARD_Y0, _BOARD_Y1 = 0.65, 1.00

_OCR_SCALE        = 2    # upscale factor before passing to EasyOCR
_CARD_OCR_SCALE   = 3    # upscale for board-card detection crop
_CARD_BRIGHT_THRESH = 195  # min brightness (max RGB) for "white" card face pixel


# ── Public API ────────────────────────────────────────────────────────────────

def read_frame(
    frame_path: str,
    *,
    roster: list[str] | None = None,
) -> dict:
    """Parse poker overlay data from a video frame.

    Args:
        frame_path: Path to a JPEG/PNG frame file.
        roster:     Known player names for fuzzy matching.

    Returns:
        {"players": [...], "pot": int, "board_count": int}
    """
    roster = roster or _DEFAULT_ROSTER
    reader = _get_reader()
    img    = Image.open(frame_path).convert("RGB")
    w, h   = img.size

    players     = _read_players(reader, img, w, h, roster)
    pot         = _read_pot(reader, img, w, h)
    board_cards = _read_board_cards(reader, img, w, h)

    return {
        "players":     players,
        "pot":         pot,
        "board_cards": board_cards,
        "board_count": len(board_cards),
    }


# ── Player parsing ────────────────────────────────────────────────────────────

def _read_players(
    reader,
    img: Image.Image,
    w: int,
    h: int,
    roster: list[str],
) -> list[dict]:
    box = img.crop((
        int(w * _PL_X0), int(h * _PL_Y0),
        int(w * _PL_X1), int(h * _PL_Y1),
    ))
    box_up = box.resize((box.width * _OCR_SCALE, box.height * _OCR_SCALE), Image.LANCZOS)
    results = reader.readtext(np.array(box_up))

    # Clean and sort by y_top (in upscaled space — only used for ordering)
    entries: list[tuple[float, str, float]] = []
    for bbox, text, conf in results:
        if conf < 0.05:
            continue
        y_top = min(pt[1] for pt in bbox)
        t     = _clean_ocr(text)
        entries.append((y_top, t, conf))
    entries.sort(key=lambda e: e[0])

    # Group into player boxes: each box starts with a player name token.
    # Action-row tokens (following text) are appended to the last box.
    boxes: list[list[tuple[float, str, float]]] = []
    for entry in entries:
        _, text, _ = entry
        if _is_name_token(text, roster):
            boxes.append([entry])
        elif boxes:
            boxes[-1].append(entry)

    players = [_parse_box(box, roster) for box in boxes]
    return [p for p in players if p["name"]]


def _is_name_token(text: str, roster: list[str]) -> bool:
    """Return True if the OCR token looks like a player name."""
    t = text.strip().upper()
    if len(t) < 3:
        return False
    # Must be mostly alphabetic (> 70 %)
    if sum(c.isalpha() for c in t) / len(t) < 0.70:
        return False
    # Reject known overlay keywords that start action rows
    if any(t.startswith(p.upper()) for p in _NON_NAME_PREFIXES):
        return False
    # Reject standalone position labels
    if t in _VALID_POSITIONS or t in _POSITION_FIXES:
        return False
    # Accept if fuzzy-matched against the roster
    roster_upper = [n.upper() for n in roster]
    return bool(get_close_matches(t, roster_upper, n=1, cutoff=0.62))


def _match_name(text: str, roster: list[str]) -> str:
    """Fuzzy-match OCR text to the best roster name; fall back to title-case."""
    t = text.strip().upper()
    # Exact substring match first
    for name in roster:
        if name.upper() == t:
            return name
    roster_upper = [n.upper() for n in roster]
    matches = get_close_matches(t, roster_upper, n=1, cutoff=0.60)
    if matches:
        target = matches[0]
        for name in roster:
            if name.upper() == target:
                return name
    return text.strip().title()


def _parse_box(box: list[tuple], roster: list[str]) -> dict:
    """Parse one player's OCR cluster (name entry + action entries) into a dict."""
    result: dict = {
        "name":     "",
        "action":   "",
        "amount":   0,
        "stack":    0,
        "position": "",
        "win_pct":  0.0,
    }
    if not box:
        return result

    y_name, name_text, _ = box[0]
    result["name"] = _match_name(name_text, roster)

    # Each player box has two visual rows:
    #   Header row: name  |  card1 card2  |  win%   (y ≈ y_name)
    #   Action row: action text  |  stack  |  position  (y ≈ y_name + 30–40 px)
    #
    # Tokens within _HEADER_GAP of the name y are card/win% fragments;
    # tokens further below are the action row.  (Coordinates are 2× upscaled.)
    _HEADER_GAP = 22
    header_entries = [e for e in box[1:] if e[0] <= y_name + _HEADER_GAP]
    action_entries = [e for e in box[1:] if e[0] >  y_name + _HEADER_GAP]

    # Win % may appear in either the name text or header fragments
    for text in [name_text] + [t for _, t, _ in header_entries]:
        pct_m = _WIN_PCT_RE.search(text)
        if pct_m:
            result["win_pct"] = float(pct_m.group(1))
            break

    action_text = " ".join(t for _, t, _ in action_entries).upper()
    result["position"] = _extract_position(action_text)
    result["action"]   = _extract_action(action_text)
    amounts = _extract_amounts(action_text)

    if len(amounts) >= 2:
        result["amount"] = amounts[0]
        result["stack"]  = amounts[-1]
    elif len(amounts) == 1:
        if result["action"] in ("checks", "folds", ""):
            result["stack"] = amounts[0]
        else:
            result["amount"] = amounts[0]
            result["stack"]  = amounts[0]

    return result


# ── Pot parsing ───────────────────────────────────────────────────────────────

def _read_pot(reader, img: Image.Image, w: int, h: int) -> int:
    """Return the pot amount from the bottom-right POT overlay."""
    box = img.crop((
        int(w * _POT_X0), int(h * _POT_Y0),
        int(w * _POT_X1), int(h * _POT_Y1),
    ))
    box_up = box.resize((box.width * _OCR_SCALE, box.height * _OCR_SCALE), Image.LANCZOS)
    results = reader.readtext(np.array(box_up))

    for _, text, conf in sorted(results, key=lambda r: min(pt[1] for pt in r[0])):
        t = _clean_ocr(text).upper()
        # Skip the POT label and the stakes line
        if "POT" in t or "NL" in t or "/" in t or "LIMIT" in t:
            continue
        amounts = _extract_amounts(t)
        if amounts:
            return amounts[0]
    return 0


# ── Board card counting ───────────────────────────────────────────────────────

def _count_board_cards(reader, img: Image.Image, w: int, h: int) -> int:
    """
    Count visible community cards.

    Strategy:
    1. OCR the pot/right area: board cards appear between the POT label row
       and the stakes row.  Count card-rank-like short tokens.
    2. Also OCR the bottom-centre area (stream-specific board display).
    3. Return 0, 3, 4, or 5.
    """
    count = _board_count_from_pot_ocr(reader, img, w, h)
    if count:
        return count
    return _board_count_from_centre(reader, img, w, h)


def _board_count_from_pot_ocr(reader, img: Image.Image, w: int, h: int) -> int:
    box = img.crop((
        int(w * _POT_X0), int(h * _POT_Y0),
        int(w * _POT_X1), int(h * _POT_Y1),
    ))
    box_up = box.resize((box.width * _OCR_SCALE, box.height * _OCR_SCALE), Image.LANCZOS)
    results = reader.readtext(np.array(box_up))

    if not results:
        return 0

    # Locate POT label y and stakes y to bracket the board row
    sorted_r = sorted(results, key=lambda r: min(pt[1] for pt in r[0]))
    pot_y    = None
    stakes_y = None
    for bbox, text, _ in sorted_r:
        t = text.upper()
        y = min(pt[1] for pt in bbox)
        if "POT" in t and pot_y is None:
            pot_y = y
        if ("NL" in t or "/" in t) and stakes_y is None:
            stakes_y = y

    # Fallback brackets if labels not found
    crop_h = box_up.height
    if pot_y is None:
        pot_y = int(crop_h * 0.38)
    if stakes_y is None:
        stakes_y = int(crop_h * 0.75)

    # Gather text in the board row band
    board_tokens: list[str] = []
    for bbox, text, conf in results:
        y = min(pt[1] for pt in bbox)
        if pot_y < y < stakes_y and conf > 0.05:
            board_tokens.append(text.upper())

    return _tokens_to_board_count(board_tokens)


def _board_count_from_centre(reader, img: Image.Image, w: int, h: int) -> int:
    box = img.crop((
        int(w * _BOARD_X0), int(h * _BOARD_Y0),
        int(w * _BOARD_X1), int(h * _BOARD_Y1),
    ))
    box_up = box.resize((box.width * _OCR_SCALE, box.height * _OCR_SCALE), Image.LANCZOS)
    results = reader.readtext(np.array(box_up))
    tokens = [text.upper() for _, text, conf in results if conf > 0.05]
    return _tokens_to_board_count(tokens)


def _tokens_to_board_count(tokens: list[str]) -> int:
    """Infer board card count from raw OCR token list."""
    combined = " ".join(tokens)
    # Tokenise on whitespace
    parts = re.split(r"\s+", combined.strip())
    # A card-like token: 1–3 chars, contains a rank character
    card_re = re.compile(r"^[2-9TJQKA\d]{1,3}$")
    rank_chars = set("23456789TJQKA")
    card_hits = sum(
        1 for p in parts
        if p and len(p) <= 3
        and any(c in rank_chars for c in p)
        and card_re.match(p)
    )
    if card_hits == 0:
        return 0
    # Snap to nearest valid count
    for valid in (3, 4, 5):
        if abs(card_hits - valid) <= 1:
            return valid
    return 3


# ── Board card reading ────────────────────────────────────────────────────────

def _read_board_cards(reader, img: Image.Image, w: int, h: int) -> list[str]:
    """
    Detect board cards from the bottom-right HCL overlay panel.

    The HCL overlay panel layout (top to bottom in y-coordinates):
      POT amount  →  "POT" label  →  board cards row  →  stakes ("50/100 NL")

    Strategy: run one OCR pass on the 3x-scaled crop; find the y-band between
    the bottom of the "Pot" label and the top of the stakes label; extract
    rank-like tokens from that band; detect suit by color analysis of each
    token's surrounding pixels.

    Returns a list like ['5c', '3d', 'Ts'] with 0, 3, 4, or 5 elements.
    """
    # Use a wider crop than _read_pot so all board cards are captured.
    x0 = int(w * _BCARD_X0)
    y0 = int(h * _POT_Y0)
    x1 = int(w * _POT_X1)
    y1 = int(h * _POT_Y1)
    crop = img.crop((x0, y0, x1, y1))

    sc = _CARD_OCR_SCALE
    arr = np.array(
        crop.resize((crop.width * sc, crop.height * sc), Image.LANCZOS),
        dtype=np.uint8,
    )
    ch, cw = arr.shape[:2]

    results = reader.readtext(arr)
    if not results:
        return []

    # Locate the POT label and the stakes row to bracket the card band.
    # Cards sit BELOW the POT label and ABOVE (or at) the stakes in y-coord space.
    pot_bot_y   = None   # bottom edge of the "Pot" label
    stakes_top_y = None  # top edge of the stakes text

    for bbox, text, _conf in sorted(results, key=lambda r: min(pt[1] for pt in r[0])):
        t = text.upper()
        if "POT" in t and pot_bot_y is None:
            pot_bot_y = int(max(pt[1] for pt in bbox))
        if ("NL" in t or ("/" in t and any(c.isdigit() for c in t))) and stakes_top_y is None:
            stakes_top_y = int(min(pt[1] for pt in bbox))

    # Fallback fractions if labels not found
    if pot_bot_y is None:
        pot_bot_y = int(ch * 0.55)
    if stakes_top_y is None:
        stakes_top_y = int(ch * 0.78)

    # Gather rank tokens from the card band.
    # The band is inclusive of stakes_top_y because the stakes text
    # (e.g. "550/100 NL") sits at that y-boundary, and its leading digit
    # coincides with the position of the furthest-left board card.
    card_hits: list[tuple[float, str, int, int, int, int]] = []
    # (center_x, rank, bx0, by0, bx1, by1)
    for bbox, text, conf in results:
        tok_y = min(pt[1] for pt in bbox)
        if not (pot_bot_y <= tok_y <= stakes_top_y):
            continue
        if conf < 0.15:
            continue
        rank = _normalize_rank(text)
        if not rank:
            continue
        bx0 = int(min(pt[0] for pt in bbox))
        by0 = int(min(pt[1] for pt in bbox))
        bx1 = int(max(pt[0] for pt in bbox))
        by1 = int(max(pt[1] for pt in bbox))
        cx  = (bx0 + bx1) / 2.0
        card_hits.append((cx, rank, bx0, by0, bx1, by1))

    # Sort left-to-right
    card_hits.sort(key=lambda t: t[0])

    # Deduplicate tokens at the same x position: playing cards print the rank
    # in both the top-left and bottom-right corners, so OCR can detect the same
    # rank twice at the same x but different y.  Only merge tokens that share
    # the SAME rank — two different ranks at the same x are distinct cards.
    _CARD_X_MERGE_PX = 25   # 3× px — cards are ~40 px wide at 3×, gaps ~15 px
    deduped: list[tuple[float, str, int, int, int, int]] = []
    for hit in card_hits:
        cx, rank = hit[0], hit[1]
        if any(abs(cx - d[0]) < _CARD_X_MERGE_PX and d[1] == rank for d in deduped):
            continue   # same rank at same x — already captured
        deduped.append(hit)
    card_hits = deduped

    if len(card_hits) < 3:
        return []

    # Detect suit for each rank token
    cards: list[str] = []
    for (cx, rank, bx0, by0, bx1, by1) in card_hits[:5]:
        tok_h = max(1, by1 - by0)
        # Include the token row plus room below it for the suit symbol
        suit_y1 = min(ch, by1 + tok_h)
        card_arr = arr[by0:suit_y1, max(0, bx0):bx1]
        if card_arr.size == 0:
            card_arr = arr[by0:by1, max(0, bx0):bx1]
        suit = _detect_suit(card_arr)
        cards.append(rank + suit)

    return cards[:5] if len(cards) >= 3 else []


def _normalize_rank(text: str) -> str:
    """Map raw OCR text to a single canonical poker rank character."""
    t = text.strip().upper().replace(" ", "")
    if t in ("10", "1O", "IO", "I0"):
        return "T"
    if len(t) == 1 and t in "23456789TJQKA":
        return t
    if t and t[0] in "23456789TJQKA":
        return t[0]
    return ""


def _detect_suit(card_arr: np.ndarray) -> str:
    """
    Detect suit via color analysis of the card image.
    Red non-white pixels → hearts (♥) or diamonds (♦).
    Dark pixels → clubs (♣) or spades (♠).
    Within each color group, the vertical position of the peak-width row
    distinguishes the two symbols (heart: wide near top; diamond: wide at middle;
    club: peak near top; spade: peak at upper-middle).
    Returns 'h', 'd', 'c', or 's'.
    """
    h, w = card_arr.shape[:2]
    # Suit symbol sits in the lower-left of the card
    sym = card_arr[max(0, h // 3):, :max(1, w * 2 // 3)]
    if sym.size == 0:
        sym = card_arr

    r = sym[:, :, 0].astype(np.float32)
    g = sym[:, :, 1].astype(np.float32)
    b = sym[:, :, 2].astype(np.float32)

    white = (r > 200) & (g > 200) & (b > 200)
    red   = (r > 150) & (r > g * 1.4) & (r > b * 1.4) & ~white
    dark  = (r < 80)  & (g < 80)  & (b < 80)  & ~white

    sym_h     = sym.shape[0]
    non_white = int((~white).sum())
    red_n     = int(red.sum())

    if non_white == 0:
        return "s"

    if red_n > max(5, non_white * 0.08):
        # Heart (♥): widest in upper ~40% (two bumps); Diamond (♦): widest at middle
        row_w = red.sum(axis=1).astype(np.float32)
        peak  = int(row_w.argmax()) if row_w.max() > 0 else 0
        return "h" if peak < sym_h * 0.45 else "d"
    else:
        # Club (♣): peak near top (three circles); Spade (♠): peak upper-middle
        dark_row = dark.sum(axis=1).astype(np.float32)
        peak = int(dark_row.argmax()) if dark_row.max() > 0 else 0
        return "c" if peak < sym_h * 0.30 else "s"


# ── Text-cleaning helpers ─────────────────────────────────────────────────────

def _clean_ocr(text: str) -> str:
    """Fix common EasyOCR artefacts in overlay text."""
    t = text.strip()
    # S/5/6 before digit → $  (but not when preceded by another digit, e.g. "16,500")
    t = _DOLLAR_FIX.sub("$", t)
    # O → 0 in numeric contexts
    t = _OCTO_FIX.sub("0", t)
    return t


def _extract_amounts(text: str) -> list[int]:
    """Return all dollar amounts found in already-cleaned text."""
    # text has already been through _clean_ocr; just search for $ + digits.
    # Include O (OCR for 0) and period (OCR noise inside numbers) in the match.
    amounts: list[int] = []
    for m in re.finditer(r"\$\s*([\d,O.]+)", text):
        raw = (m.group(1)
               .replace(",", "")
               .replace(".", "")
               .replace("O", "0")
               .replace("o", "0"))
        try:
            val = int(raw)
            if val > 0:
                amounts.append(val)
        except ValueError:
            pass
    return amounts


def _extract_action(text: str) -> str:
    """Extract canonical action keyword from action-row text."""
    t = text.upper()
    for kw, canonical in _ACTION_KEYWORDS:
        if kw in t:
            return canonical
    return ""


def _extract_position(text: str) -> str:
    """Extract position label from action-row text."""
    words = re.split(r"[\s\[\]().,|]+", text.upper())
    for w in reversed(words):
        if w in _VALID_POSITIONS:
            return w
        if w in _POSITION_FIXES:
            return _POSITION_FIXES[w]
    return ""


# ── CLI test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path

    frames_dir = Path(
        sys.argv[1] if len(sys.argv) > 1
        else r"downloads\frames\p0Q7LW64ecM_2300-4100"
    )

    # Frames 80–116: preflop / action (no board cards)
    # Frame 120: transition scene
    # Frames 130–150: post-flop (board cards visible)
    test_nums = list(range(80, 117, 4)) + [130, 140, 150]

    print("Initialising EasyOCR reader…")
    _get_reader()   # warm up once

    for n in test_nums:
        path = frames_dir / f"frame_{n:06d}.jpg"
        if not path.exists():
            print(f"\n[frame {n:06d}] NOT FOUND — skipping")
            continue

        print(f"\n{'='*60}")
        print(f"Frame {n:06d}: {path}")
        print('='*60)

        result = read_frame(str(path))

        print(f"  pot:         ${result['pot']:,}")
        print(f"  board_count: {result['board_count']}")
        print(f"  players ({len(result['players'])}):")
        for p in result["players"]:
            pct = f"  win={p['win_pct']:.0f}%" if p["win_pct"] else ""
            act = p["action"] or "(no action)"
            amt = f" ${p['amount']:,}" if p["amount"] else ""
            stk = f"  stack=${p['stack']:,}" if p["stack"] else ""
            pos = f"  [{p['position']}]" if p["position"] else ""
            print(f"    {p['name']:<12} {act}{amt}{stk}{pos}{pct}")
