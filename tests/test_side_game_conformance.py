"""The SIDE GAME block the server writes must satisfy the spec Cole's parser is
built from (docs/SIDE_GAME_HH_FORMAT.md): every line matches exactly one of the
spec's regexes, display names are base + suffixes for the counts entering the
hand, the bounty and squid invariants hold, and with the block stripped the hand
is ordinary PokerStars that passes the PT4 linter. The 7-2 game runs too.

Hands come from a long random session (frontend/tests/dump-side-game-hands.mjs)
with both games running. Needs node; skipped otherwise.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
N = int(os.environ.get("SIDE_GAME_HANDS", "1500"))

SPEC = open(os.path.join(ROOT, "docs", "SIDE_GAME_HH_FORMAT.md"), encoding="utf-8").read()
PATS = {k: re.compile(v) for k, v in re.findall(r'^\s+(\w+)\s+= r"(.+)"$', SPEC, re.M)}


def _amount(streak, levels, step):
    if streak < min(levels):
        return 0
    if streak in levels:
        return levels[streak]
    top = max(levels)
    return levels[top] + (streak - top) * step


def check_hand(text):
    """Return a list of spec violations for one formatted hand."""
    problems = []
    head, sep, block = text.partition("*** SIDE GAME ***")
    if not sep:
        return ["no SIDE GAME section"]
    lines = [l for l in block.strip("\n").split("\n") if l.strip()]
    parsed = []
    for line in lines:
        hits = [(k, p.match(line)) for k, p in PATS.items() if k != "SECTION" and p.match(line)]
        if len(hits) != 1:
            problems.append(f"line matches {len(hits)} spec regexes: {line!r}")
            continue
        parsed.append(hits[0])

    seat_names = dict(re.findall(r"^Seat (\d+): (.+?) \(\$\d+ in chips\)", head, re.M))
    bounty = {int(m.group(1)): m for k, m in parsed if k == "B_STREAK"}
    squid = {m.group(2) if m.group(1) is None else int(m.group(1)): m for k, m in parsed if k == "S_BUTTONS"}

    # Display names = base + bounty suffix + squid suffix, for counts entering the hand.
    for seat, shown in seat_names.items():
        seat = int(seat)
        b = bounty.get(seat)
        q = squid.get(seat)
        base = (b or q).group(2) if (b or q) else None
        if base is None:
            problems.append(f"seat {seat} has no side-game line")
            continue
        streak = int(b.group(3)) if b else 0
        buttons = int(q.group(3)) if q else 0
        want = base + (f"{streak}bounty" if streak else "") + (f"{buttons}squid" if buttons else "")
        if shown != want:
            problems.append(f"seat {seat}: name {shown!r} != {want!r}")

    # Bounty invariants.
    sched = next((m for k, m in parsed if k == "B_SCHED"), None)
    if sched:
        levels = {int(a): int(b) for a, b in re.findall(r"(\d+)=\$(\d+)", sched.group(1))}
        step = int(sched.group(2))
        for seat, m in bounty.items():
            before, after, res = int(m.group(3)), int(m.group(4)), m.group(5)
            if after != (0 if res == "lost" else before + 1):
                problems.append(f"bounty seat {seat}: {before}->{after} {res}")
        pays = [m for k, m in parsed if k == "B_PAYOUT"]
        winners = [s for s, m in bounty.items() if m.group(5) == "won" and _amount(int(m.group(4)), levels, step) > 0]
        if len(pays) != len(winners) * (len(bounty) - 1):
            problems.append(f"bounty: {len(pays)} payouts for {len(winners)} qualifying winners")
        for m in pays:
            w = int(m.group(3))
            if w not in winners or int(m.group(5)) != _amount(int(m.group(6)), levels, step):
                problems.append(f"bad bounty payout: {m.group(0)!r}")

    # Squid invariants.
    rnd = next((m for k, m in parsed if k == "S_ROUND"), None)
    if rnd:
        total, value = int(rnd.group(2)), int(rnd.group(3))
        left = next(m for k, m in parsed if k == "S_LEFT")
        lb, la = int(left.group(1)), int(left.group(2))
        rows = [m for k, m in parsed if k == "S_BUTTONS"]
        if sum(int(m.group(4)) for m in rows) != total - la:
            problems.append(f"squid: buttons held {sum(int(m.group(4)) for m in rows)} != {total} - {la}")
        if lb - la != sum(1 for m in rows if m.group(5)):
            problems.append("squid: buttons left doesn't match ' won' flags")
        settled = any(k == "S_SETTLED" for k, _ in parsed)
        if settled != (lb > 0 and la == 0):
            problems.append(f"squid: settled={settled} with left {lb}->{la}")
        if settled:
            payers = [m for m in rows if m.group(1) is not None and int(m.group(4)) == 0]
            holders = [m for m in rows if int(m.group(4)) > 0]
            pays = [m for k, m in parsed if k == "S_PAYOUT"]
            if len(pays) != len(payers) * len(holders):
                problems.append(f"squid: {len(pays)} payouts for {len(payers)}x{len(holders)}")
            for m in pays:
                if int(m.group(5)) != value * int(m.group(6)):
                    problems.append(f"bad squid payout: {m.group(0)!r}")

    # 7-2 invariants: the game line comes with holder lines; only a sole winner
    # holding 7-2 is paid, by every other seat, the full value.
    game = next((m for k, m in parsed if k == "T_GAME"), None)
    holders = [m for k, m in parsed if k == "T_HAND"]
    if bool(game) != bool(holders):
        problems.append("7-2: game line without holders, or holders without it")
    if game:
        value = int(game.group(1))
        won = {int(m.group(1)) for m in holders if m.group(5) == "won"}
        for m in holders:
            if not ({m.group(3)[0], m.group(4)[0]} == {"7", "2"}):
                problems.append(f"7-2 holder without 7-2: {m.group(0)!r}")
        pays = [m for k, m in parsed if k == "T_PAYOUT"]
        if len(pays) != len(won) * (len(seat_names) - 1):
            problems.append(f"7-2: {len(pays)} payouts for {len(won)} winners")
        for m in pays:
            if int(m.group(3)) not in won or int(m.group(5)) != value or m.group(1) == m.group(3):
                problems.append(f"bad 7-2 payout: {m.group(0)!r}")
    return problems


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_side_game_block_matches_the_parser_spec(tmp_path):
    os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = str(tmp_path)
    os.environ["STREAM_REFRESH"] = "0"
    os.environ.pop("DATABASE_URL", None)
    import server
    from src.export.pt4_formatter import format_hand
    from src.validate.pt4_linter import lint_hand

    assert {"B_SCHED", "B_STREAK", "B_PAYOUT", "S_ROUND", "S_LEFT", "S_BUTTONS", "S_SETTLED", "S_PAYOUT",
            "T_GAME", "T_HAND", "T_PAYOUT"} <= set(PATS)
    out = subprocess.run(["node", os.path.join(ROOT, "frontend", "tests", "dump-side-game-hands.mjs"), str(N)],
                         capture_output=True, text=True, check=True, cwd=os.path.join(ROOT, "frontend")).stdout
    failures, settled, away, chops, sevens, sevens_paid = [], 0, 0, 0, 0, 0
    for line in out.splitlines():
        rec = json.loads(line)
        hand = rec["hand"]
        server._autofill_showdown(hand)
        text = format_hand(hand, hand["table_name"], rec["i"], 0, 0)
        problems = check_hand(text)
        poker_only = text.split("*** SIDE GAME ***")[0]
        lint = lint_hand(poker_only, rec["i"])
        if not lint.passed:
            problems += [f"lint: {v}" for v in lint.violations[:2]]
        if problems:
            failures.append(f"hand {rec['i']}: " + "; ".join(problems[:3]))
        settled += "Squid settled:" in text
        away += "Away (" in text
        chops += " chopped" in text
        sevens += "7-2 hand:" in text
        sevens_paid += "7-2 payout:" in text
    assert not failures, f"{len(failures)}/{N} hands broke the spec:\n" + "\n".join(failures[:12])
    # The random session must actually exercise the interesting paths.
    assert settled >= 3 and away >= 3 and chops >= 1 and sevens >= 20 and sevens_paid >= 2, (settled, away, chops, sevens, sevens_paid)
