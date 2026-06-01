"""
Poker hand evaluator.
Finds the best 5-card hand from hole cards + board and returns a score tuple
and a human-readable description compatible with PT4 hand history format.
"""

from itertools import combinations


_RANK_ORDER = "23456789TJQKA"
_RANK_VALUE = {r: i for i, r in enumerate(_RANK_ORDER)}

_RANK_PLURAL = {
    0: "Twos", 1: "Threes", 2: "Fours", 3: "Fives", 4: "Sixes",
    5: "Sevens", 6: "Eights", 7: "Nines", 8: "Tens", 9: "Jacks",
    10: "Queens", 11: "Kings", 12: "Aces",
}
_RANK_SINGULAR = {
    0: "Two", 1: "Three", 2: "Four", 3: "Five", 4: "Six",
    5: "Seven", 6: "Eight", 7: "Nine", 8: "Ten", 9: "Jack",
    10: "Queen", 11: "King", 12: "Ace",
}


def _parse_card(card: str) -> tuple[int, str]:
    """Return (rank_value 0-12, suit char)."""
    rank_str = card[:-1].upper()
    suit = card[-1].lower()
    return _RANK_VALUE.get(rank_str, 0), suit


def _score_five(cards: list[str]) -> tuple:
    """Return a comparable score tuple for exactly 5 cards. Higher = better."""
    parsed = [_parse_card(c) for c in cards]
    ranks = sorted([r for r, s in parsed], reverse=True)
    suits = [s for r, s in parsed]

    rank_set = set(ranks)
    is_flush = len(set(suits)) == 1
    is_straight = len(rank_set) == 5 and (
        ranks[0] - ranks[4] == 4 or rank_set == {12, 0, 1, 2, 3}
    )
    straight_top = 3 if rank_set == {12, 0, 1, 2, 3} else ranks[0]

    rank_counts: dict[int, int] = {}
    for r in ranks:
        rank_counts[r] = rank_counts.get(r, 0) + 1

    # Sort by (count desc, rank desc) so best group comes first
    counts = sorted(rank_counts.items(), key=lambda x: (x[1], x[0]), reverse=True)
    sorted_ranks = [r for r, _c in counts]
    groups = [c for _r, c in counts]

    if is_straight and is_flush:
        return (8, straight_top)
    if groups[0] == 4:
        return (7, sorted_ranks[0], sorted_ranks[1])
    if groups[:2] == [3, 2]:
        return (6, sorted_ranks[0], sorted_ranks[1])
    if is_flush:
        return (5,) + tuple(ranks)
    if is_straight:
        return (4, straight_top)
    if groups[0] == 3:
        return (3, sorted_ranks[0]) + tuple(sorted_ranks[1:])
    if groups[:2] == [2, 2]:
        high_pair = max(sorted_ranks[0], sorted_ranks[1])
        low_pair = min(sorted_ranks[0], sorted_ranks[1])
        return (2, high_pair, low_pair, sorted_ranks[2])
    if groups[0] == 2:
        return (1, sorted_ranks[0]) + tuple(sorted_ranks[1:])
    return (0,) + tuple(ranks)


def _describe(score: tuple) -> str:
    tier = score[0]
    if tier == 8:
        return f"a straight flush, {_RANK_SINGULAR[score[1]]}-high"
    if tier == 7:
        return f"four of a kind, {_RANK_PLURAL[score[1]]}"
    if tier == 6:
        return f"a full house, {_RANK_PLURAL[score[1]]} full of {_RANK_PLURAL[score[2]]}"
    if tier == 5:
        return f"a flush, {_RANK_SINGULAR[score[1]]}-high"
    if tier == 4:
        return f"a straight, {_RANK_SINGULAR[score[1]]}-high"
    if tier == 3:
        return f"three of a kind, {_RANK_PLURAL[score[1]]}"
    if tier == 2:
        return f"two pair, {_RANK_PLURAL[score[1]]} and {_RANK_PLURAL[score[2]]}"
    if tier == 1:
        return f"a pair of {_RANK_PLURAL[score[1]]}"
    return f"high card {_RANK_SINGULAR[score[1]]}"


def evaluate_best_hand(
    hole_cards: list[str], board: list[str]
) -> tuple[tuple, str]:
    """Find the best 5-card hand from hole_cards + board.

    Returns (score_tuple, description). Tuples are directly comparable: higher wins.
    """
    all_cards = [c for c in hole_cards + board if c]
    if len(all_cards) < 5:
        return (0,), "insufficient cards"

    best: tuple | None = None
    for combo in combinations(all_cards, 5):
        s = _score_five(list(combo))
        if best is None or s > best:
            best = s

    return best, _describe(best)
