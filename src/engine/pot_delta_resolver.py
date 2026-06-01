"""
src/engine/pot_delta_resolver.py

Infers missing player actions when the observed pot total doesn't match
the expected pot read from the overlay.  Only call-sized gaps are resolved;
unresolvable deltas are returned as-is with no inferred actions appended.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Action:
    """A confirmed action observed from the overlay or transcript."""
    player: str
    action: str
    amount: int = 0


@dataclass(frozen=True)
class InferredAction:
    """An action inferred from a pot-delta gap; not directly observed."""
    player: str
    action: str
    amount: int


class PotDeltaResolver:
    """
    Resolve missing actions from a pot delta.

    Usage
    -----
    resolver = PotDeltaResolver()
    result = resolver.resolve(actions, expected_pot=1200, call_amount=300,
                              candidates=["Bob", "Charlie"])
    # result is list[Action | InferredAction]
    """

    def resolve(
        self,
        actions: list[Action],
        expected_pot: int,
        call_amount: int,
        candidates: list[str],
    ) -> list[Action | InferredAction]:
        """
        Parameters
        ----------
        actions      : confirmed actions for this street
        expected_pot : pot size read from the overlay
        call_amount  : cost of a single call this street
        candidates   : players (in order) who could account for the missing chips
        """
        observed_total = sum(a.amount for a in actions)
        missing_delta = expected_pot - observed_total

        if missing_delta == 0:
            return list(actions)

        if call_amount <= 0 or missing_delta < 0:
            return list(actions)

        if missing_delta % call_amount != 0:
            return list(actions)

        n_callers = missing_delta // call_amount
        if n_callers > len(candidates):
            return list(actions)

        inferred: list[InferredAction] = [
            InferredAction(candidates[i], "calls", call_amount)
            for i in range(n_callers)
        ]
        return list(actions) + inferred
