"""End-to-end fuzz: random hands from the real JS betting engine, formatted by the
real server code path (_autofill_showdown + format_hand) and checked by the PT4
linter. Catches any disagreement between the engine's accounting and the
formatter's (pots, uncalled bets, side pots, summaries).

Needs node on PATH; skipped otherwise. FUZZ_E2E_HANDS sets the count.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

N = int(os.environ.get("FUZZ_E2E_HANDS", "1500"))
SEED = os.environ.get("FUZZ_SEED", "7")


@pytest.fixture(scope="module")
def formatter():
    # Importing server runs init_db(); keep its SQLite file out of the repo.
    tmp = tempfile.mkdtemp()
    os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = tmp
    os.environ.pop("DATABASE_URL", None)
    os.environ["STREAM_REFRESH"] = "0"
    import server
    yield server
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_random_engine_hands_format_and_lint(formatter):
    from src.validate.pt4_linter import lint_hand
    from src.export.pt4_formatter import format_hand

    out = subprocess.run(
        ["node", os.path.join(ROOT, "frontend", "tests", "dump-random-hands.mjs"), str(N), SEED],
        capture_output=True, text=True, check=True, cwd=os.path.join(ROOT, "frontend"),
    ).stdout
    failures = []
    for line in out.splitlines():
        rec = json.loads(line)
        hand = rec["hand"]
        formatter._autofill_showdown(hand)
        try:
            text = format_hand(hand, hand["table_name"], rec["i"], 0, 0)
        except Exception as e:  # formatter rejected a hand the engine produced
            failures.append(f"hand {rec['i']}: formatter raised {type(e).__name__}: {e}")
            continue
        res = lint_hand(text, rec["i"])
        if not res.passed:
            failures.append(f"hand {rec['i']}: " + "; ".join(str(v) for v in res.violations[:3]))
    assert not failures, f"{len(failures)}/{N} hands failed:\n" + "\n".join(failures[:15])
