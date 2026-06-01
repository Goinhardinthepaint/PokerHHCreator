"""
src/export/hh_cleaner.py

Post-process Claude-generated PokerStars hand histories into strict PT4 format.

Fixes applied (in order):
  1.  "Seat N: Player antes $X"    → "Player: posts the ante $X"
  1b. "Player antes $X"            → "Player: posts the ante $X"
  2a. "posts ante $X"              → "posts the ante $X"
  2b. "posts the small/big blind"  → "posts small/big blind"
  3.  "Player: bets" (no amount)   → line removed
  4.  "raises $0 to $X"            → "raises $X to $X"
  5.  "Player: wins $X"            → "Player collected $X from pot"
  6.  "collected $X and won with…" → "collected $X from pot"
  7.  Invalid card notation        → hand rejected
  8.  "posts straddle" after       → line removed
      *** HOLE CARDS ***
  9.  Multiple straddle lines      → keep only first
  10. Summary "bet/called/raised"  → "mucked"
  11. No winner in SUMMARY         → hand rejected
  12. "| No rake"                  → "| Rake $0"
  13. Cards not exactly 2 chars    → hand rejected

Usage:
    python src/export/hh_cleaner.py output/hands_onecall.txt output/hands_cleaned.txt
"""

import re
import sys
from pathlib import Path

# ── Card validation ────────────────────────────────────────────────────────────
_VALID_RANKS = frozenset("23456789TJQKA")
_VALID_SUITS = frozenset("shdc")

# Lines where card brackets actually contain hole/board cards
_CARD_LINE_PATTERNS = re.compile(
    r"Dealt to |: shows \[|mucked \[|\*\*\* (?:FLOP|TURN|RIVER) \*\*\*|^Board \[",
    re.IGNORECASE,
)


def _is_valid_card(token: str) -> bool:
    return len(token) == 2 and token[0] in _VALID_RANKS and token[1] in _VALID_SUITS


def _validate_cards(hand_text: str, issues: list[str]) -> bool:
    """Return False (and append issue) if any card token on a card-line is invalid."""
    for line in hand_text.splitlines():
        if not _CARD_LINE_PATTERNS.search(line):
            continue
        for bracket_m in re.finditer(r"\[([^\]]+)\]", line):
            for token in bracket_m.group(1).split():
                if len(token) != 2:
                    continue
                if not _is_valid_card(token):
                    issues.append(
                        f"  [REJECT-7/13] invalid card '{token}' on line: {line!r}"
                    )
                    return False
    return True


# ── Line-by-line fixes ─────────────────────────────────────────────────────────

def _fix_lines(lines: list[str], issues: list[str]) -> list[str]:
    result: list[str] = []
    in_hole_cards = False
    in_summary = False
    straddle_count = 0

    for raw_line in lines:
        line = raw_line

        # Section tracking
        if "*** HOLE CARDS ***" in line:
            in_hole_cards = True
        if "*** SUMMARY ***" in line:
            in_summary = True

        # Fix 1: "Seat N: Player antes $X" → "Player: posts the ante $X"
        m = re.match(r"^Seat \d+: ([\w][\w ]*?) antes \$(\d+)\s*$", line)
        if m:
            line = f"{m.group(1)}: posts the ante ${m.group(2)}"
            issues.append(f"  [fix1 ] {raw_line!r} → {line!r}")

        # Fix 1b: "Player antes $X" (no colon) → "Player: posts the ante $X"
        m = re.match(r"^([\w][\w ]*?) antes \$(\d+)\s*$", line)
        if m and ":" not in line:
            line = f"{m.group(1)}: posts the ante ${m.group(2)}"
            issues.append(f"  [fix1b] {raw_line!r} → {line!r}")

        # Fix 2a: "posts ante $X" → "posts the ante $X"
        if re.search(r"\bposts ante \$", line):
            line = re.sub(r"\bposts ante \$", "posts the ante $", line)
            issues.append(f"  [fix2a] posts ante → posts the ante")

        # Fix 2b: "posts the small blind" / "posts the big blind" → remove "the"
        for blind in ("small blind", "big blind"):
            if f"posts the {blind}" in line:
                line = line.replace(f"posts the {blind}", f"posts {blind}")
                issues.append(f"  [fix2b] 'the {blind}' → '{blind}'")

        # Fix 3: "Player: bets" with no dollar amount → remove line entirely
        if re.match(r"^[\w][\w ]*:\s+bets\s*$", line):
            issues.append(f"  [fix3 ] removed empty bets line: {raw_line!r}")
            continue

        # Fix 4: "raises $0 to $X" → "raises $X to $X" (treat as min-raise)
        m = re.search(r"raises \$0 to \$(\d+)", line)
        if m:
            amt = m.group(1)
            line = re.sub(r"raises \$0 to \$\d+", f"raises ${amt} to ${amt}", line)
            issues.append(f"  [fix4 ] raises $0 → raises ${amt} to ${amt}")

        # Fix 5: "Player: wins $X" → "Player collected $X from pot"
        m = re.match(r"^([\w][\w ]*): wins \$(\d+)(.*)", line)
        if m:
            line = f"{m.group(1)} collected ${m.group(2)} from pot"
            issues.append(f"  [fix5 ] wins → collected from pot")

        # Fix 6: "collected $X and won with…" → "collected $X from pot"
        if re.search(r"collected \$\d+ and won\b", line):
            line = re.sub(r"(collected \$\d+) and won\b.*", r"\1 from pot", line)
            issues.append(f"  [fix6 ] 'collected and won' → 'collected from pot'")

        # Fix 8 & 9: straddle management
        if "posts straddle" in line and not in_summary:
            if in_hole_cards:
                issues.append(
                    f"  [fix8 ] removed straddle after *** HOLE CARDS ***: {line!r}"
                )
                continue
            straddle_count += 1
            if straddle_count > 1:
                issues.append(
                    f"  [fix9 ] removed duplicate straddle #{straddle_count}: {line!r}"
                )
                continue

        # Fix 10: summary seat lines with raw action verbs → "mucked"
        if in_summary:
            m = re.match(
                r"^(Seat \d+: [\w][\w ]*(?:\s+\((?:button|small blind|big blind)\))?)\s+"
                r"(?:raised?(?:\s+\$\d+)?|called?(?:\s+\$\d+)?(?:\s+and\s+lost)?|bet\s+\$\d+)\s*$",
                line,
            )
            if m:
                line = f"{m.group(1)} mucked"
                issues.append(f"  [fix10] summary action → mucked: {raw_line!r}")

        # Fix 12: "| No rake" → "| Rake $0"
        if "| No rake" in line:
            line = line.replace("| No rake", "| Rake $0")
            issues.append("  [fix12] No rake → Rake $0")

        result.append(line)

    return result


# ── Summary validation ─────────────────────────────────────────────────────────

def _validate_summary(hand_text: str, issues: list[str]) -> bool:
    """Fix 11: reject if SUMMARY has no seat lines or no winner."""
    m = re.search(r"\*\*\* SUMMARY \*\*\*\n?(.*)", hand_text, re.DOTALL)
    if not m:
        issues.append("  [REJECT-11] no *** SUMMARY *** section")
        return False
    seat_lines = re.findall(r"^Seat \d+:.*$", m.group(1), re.MULTILINE)
    if not seat_lines:
        issues.append("  [REJECT-11] SUMMARY has no seat lines")
        return False
    has_winner = any(
        "collected" in ln or ("showed" in ln and "won" in ln)
        for ln in seat_lines
    )
    if not has_winner:
        issues.append("  [REJECT-11] SUMMARY has no winner (no 'collected' or 'showed…won')")
        return False
    return True


# ── Public API ─────────────────────────────────────────────────────────────────

def clean_hand(raw: str) -> tuple[str | None, list[str]]:
    """
    Clean a single PokerStars hand history string.

    Returns:
        (cleaned_text, issues)  — cleaned_text is None if the hand is rejected.
    """
    issues: list[str] = []
    lines = raw.strip().splitlines()

    # Line-by-line fixes
    fixed = _fix_lines(lines, issues)
    hand_text = "\n".join(fixed)

    # Card validation (fix 7 / 13)
    if not _validate_cards(hand_text, issues):
        return None, issues

    # Summary validation (fix 11)
    if not _validate_summary(hand_text, issues):
        return None, issues

    return hand_text, issues


def parse_hands(text: str) -> list[str]:
    """Split a hand history file into individual PokerStars hand strings."""
    parts = re.split(r"(?=^PokerStars Hand #)", text, flags=re.MULTILINE)
    return [p.strip() for p in parts if p.strip().startswith("PokerStars Hand #")]


def clean_file(input_path: str, output_path: str) -> int:
    """
    Read input_path, clean every hand, write surviving hands to output_path.
    Returns the number of hands that survived.
    """
    text = Path(input_path).read_text(encoding="utf-8")
    raw_hands = parse_hands(text)
    print(f"Parsed {len(raw_hands)} hands from {input_path}")

    cleaned: list[str] = []
    total_fixes = 0

    for i, raw in enumerate(raw_hands, 1):
        issues: list[str] = []
        result, issues = clean_hand(raw)
        rejected = result is None

        if issues:
            header = raw.splitlines()[0][:80] if raw else f"Hand {i}"
            label = "REJECTED" if rejected else f"{len(issues)} fix(es)"
            print(f"\nHand {i} [{label}]  {header}")
            for iss in issues:
                print(iss)
            if not rejected:
                total_fixes += len(issues)

        if result is not None:
            cleaned.append(result)

    kept = len(cleaned)
    rejected_count = len(raw_hands) - kept
    print(f"\n{'─'*60}")
    print(f"  Input  : {len(raw_hands)} hands")
    print(f"  Kept   : {kept} hands  ({total_fixes} lines auto-fixed)")
    print(f"  Removed: {rejected_count} hands (invalid cards or no winner)")
    print(f"{'─'*60}")

    output_text = "\n\n\n".join(cleaned)
    Path(output_path).write_text(output_text, encoding="utf-8")
    print(f"Saved {kept} hand(s) → {output_path}")
    return kept


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.stderr.write(
            f"Usage: python {sys.argv[0]} <input_hands.txt> <output_cleaned.txt>\n"
        )
        sys.exit(1)
    clean_file(sys.argv[1], sys.argv[2])
