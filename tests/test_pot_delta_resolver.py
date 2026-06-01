"""
tests/test_pot_delta_resolver.py

Unit tests for PotDeltaResolver.

Run:
    python tests/test_pot_delta_resolver.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.engine.pot_delta_resolver import Action, InferredAction, PotDeltaResolver

failures: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        msg = f"  FAIL  {label}" + (f": {detail}" if detail else "")
        print(msg)
        failures.append(msg)


resolver = PotDeltaResolver()


def test_exact_match() -> None:
    print("\n-- Exact match: no inference needed --")
    actions = [
        Action("Alice", "raises", 200),
        Action("Bob",   "calls",  200),
    ]
    result = resolver.resolve(actions, expected_pot=400, call_amount=200, candidates=["Charlie"])

    check("exact/len",         len(result) == 2, str(len(result)))
    check("exact/no-inferred", not any(isinstance(a, InferredAction) for a in result))
    check("exact/alice",       result[0] == Action("Alice", "raises", 200))
    check("exact/bob",         result[1] == Action("Bob", "calls", 200))


def test_one_missing_caller() -> None:
    print("\n-- One missing caller --")
    actions = [Action("Alice", "raises", 300)]
    result = resolver.resolve(
        actions,
        expected_pot=600,
        call_amount=300,
        candidates=["Bob", "Charlie"],
    )

    check("one/len",              len(result) == 2, str(len(result)))
    check("one/original-present", result[0] == Action("Alice", "raises", 300))
    check("one/inferred-type",    isinstance(result[1], InferredAction))
    check("one/inferred-player",  result[1].player == "Bob",   result[1].player)
    check("one/inferred-action",  result[1].action == "calls", result[1].action)
    check("one/inferred-amount",  result[1].amount == 300,     str(result[1].amount))


def test_two_missing_callers() -> None:
    print("\n-- Two missing callers --")
    actions = [Action("Alice", "raises", 300)]
    result = resolver.resolve(
        actions,
        expected_pot=900,
        call_amount=300,
        candidates=["Bob", "Charlie"],
    )

    check("two/len",          len(result) == 3, str(len(result)))
    check("two/bob-inferred", isinstance(result[1], InferredAction) and result[1].player == "Bob")
    check("two/bob-amount",   result[1].amount == 300, str(result[1].amount))
    check("two/charlie",      isinstance(result[2], InferredAction) and result[2].player == "Charlie")
    check("two/charlie-amt",  result[2].amount == 300, str(result[2].amount))
    total = sum(a.amount for a in result)
    check("two/total",        total == 900, str(total))


def test_unresolvable_delta() -> None:
    print("\n-- Unresolvable delta (not divisible by call_amount) --")
    actions = [Action("Alice", "raises", 200)]
    result = resolver.resolve(
        actions,
        expected_pot=450,   # delta=250, not divisible by 200
        call_amount=200,
        candidates=["Bob", "Charlie"],
    )

    check("unres/len",         len(result) == 1, str(len(result)))
    check("unres/no-inferred", not any(isinstance(a, InferredAction) for a in result))
    check("unres/original",    result[0] == Action("Alice", "raises", 200))


def test_not_enough_candidates() -> None:
    print("\n-- Not enough candidates for inferred callers --")
    actions = [Action("Alice", "raises", 300)]
    result = resolver.resolve(
        actions,
        expected_pot=1200,  # delta=900, needs 3 callers
        call_amount=300,
        candidates=["Bob", "Charlie"],  # only 2
    )

    check("nocan/len",         len(result) == 1, str(len(result)))
    check("nocan/no-inferred", not any(isinstance(a, InferredAction) for a in result))


def test_all_accounted_for() -> None:
    print("\n-- All chips accounted for, empty candidates list --")
    actions = [
        Action("Alice", "raises", 500),
        Action("Bob",   "calls",  500),
        Action("Carol", "calls",  500),
    ]
    result = resolver.resolve(
        actions,
        expected_pot=1500,
        call_amount=500,
        candidates=[],
    )

    check("allok/len",         len(result) == 3, str(len(result)))
    check("allok/no-inferred", not any(isinstance(a, InferredAction) for a in result))


def test_negative_delta_ignored() -> None:
    print("\n-- Negative delta (observed > expected) returns actions as-is --")
    actions = [
        Action("Alice", "raises", 600),
        Action("Bob",   "calls",  600),
    ]
    result = resolver.resolve(
        actions,
        expected_pot=1000,  # less than observed 1200
        call_amount=600,
        candidates=["Carol"],
    )

    check("neg/len",         len(result) == 2, str(len(result)))
    check("neg/no-inferred", not any(isinstance(a, InferredAction) for a in result))


def test_zero_call_amount_ignored() -> None:
    print("\n-- Zero call_amount: no inference attempted --")
    actions = [Action("Alice", "bets", 0)]
    result = resolver.resolve(
        actions,
        expected_pot=300,
        call_amount=0,
        candidates=["Bob"],
    )

    check("zero/len",         len(result) == 1, str(len(result)))
    check("zero/no-inferred", not any(isinstance(a, InferredAction) for a in result))


# ---------------------------------------------------------------------------

def main() -> None:
    print("=== test_pot_delta_resolver ===")

    test_exact_match()
    test_one_missing_caller()
    test_two_missing_callers()
    test_unresolvable_delta()
    test_not_enough_candidates()
    test_all_accounted_for()
    test_negative_delta_ignored()
    test_zero_call_amount_ignored()

    print()
    if failures:
        print(f"RESULT: FAIL  ({len(failures)} assertion(s) failed)")
        for f in failures:
            print(f"  {f}")
        sys.exit(1)
    else:
        print("RESULT: PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
