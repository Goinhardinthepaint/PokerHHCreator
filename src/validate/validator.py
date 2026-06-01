"""
Hand History Validator

Validates parsed hand histories for logical consistency before PT4 export.
Catches OCR/parsing errors that would produce invalid hands.
"""

from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_hand(hand: dict) -> ValidationResult:
    """Run all validation checks on a parsed hand.
    
    Returns ValidationResult with any errors/warnings found.
    """
    result = ValidationResult(valid=True)
    
    _check_card_uniqueness(hand, result)
    _check_chip_math(hand, result)
    _check_action_legality(hand, result)
    _check_player_consistency(hand, result)
    _check_board_progression(hand, result)
    
    if result.errors:
        result.valid = False
    
    return result


def _check_card_uniqueness(hand: dict, result: ValidationResult):
    """No card should appear twice across board + all hole cards."""
    all_cards = []
    
    board = hand.get("board", {})
    flop = board.get("flop", [])
    turn = board.get("turn")
    river = board.get("river")
    
    all_cards.extend(flop)
    if turn:
        all_cards.append(turn)
    if river:
        all_cards.append(river)
    
    # Collect hole cards from players and showdown, avoiding double-counting.
    # Showdown often repeats the same cards as players[].hole_cards.
    players_with_cards = set()
    for player in hand.get("players", []):
        cards = player.get("hole_cards")
        if cards:
            all_cards.extend(cards)
            players_with_cards.add(player["name"])
    
    # Only add showdown cards for players not already counted
    for sd in hand.get("showdown", []):
        if sd["player"] not in players_with_cards:
            cards = sd.get("hole_cards", [])
            all_cards.extend(cards)
    
    # Filter out None/empty
    all_cards = [c for c in all_cards if c]
    
    seen = set()
    for card in all_cards:
        if card in seen:
            result.errors.append(f"Duplicate card: {card}")
        seen.add(card)


def _check_chip_math(hand: dict, result: ValidationResult):
    """Verify that pot = sum of all bets from all players across all streets."""
    action = hand.get("action", {})
    total_invested = 0
    
    for street in ["preflop", "flop", "turn", "river"]:
        for act in action.get(street, []):
            amount = act.get("amount", 0)
            if amount and act["action"] in ("calls", "bets", "raises", 
                                              "all_in", "posts_sb", "posts_bb"):
                total_invested += amount
    
    reported_pot = hand.get("pot", {}).get("total", 0)
    
    if reported_pot > 0 and abs(total_invested - reported_pot) > 100:
        # Allow small rounding tolerance for ante/rake
        result.warnings.append(
            f"Pot mismatch: actions sum to ${total_invested}, "
            f"reported pot is ${reported_pot} "
            f"(diff: ${abs(total_invested - reported_pot)})"
        )


def _check_action_legality(hand: dict, result: ValidationResult):
    """Check that actions make sense in sequence."""
    action = hand.get("action", {})
    game = hand.get("game", {})
    stakes = game.get("stakes", "1/2")
    
    try:
        bb_amount = int(stakes.split("/")[1])
    except (ValueError, IndexError):
        bb_amount = 100  # fallback
    
    for street in ["preflop", "flop", "turn", "river"]:
        actions = action.get(street, [])
        current_bet = bb_amount if street == "preflop" else 0
        
        for act in actions:
            if act["action"] == "raises":
                amount = act.get("amount", 0)
                if amount and amount < current_bet:
                    result.warnings.append(
                        f"{street}: {act['player']} raises to ${amount} "
                        f"but current bet is ${current_bet}"
                    )
                if amount:
                    current_bet = amount
            
            elif act["action"] == "bets":
                amount = act.get("amount", 0)
                if street == "preflop" and current_bet > 0:
                    # Can't bet preflop when there's already a blind
                    pass  # This is actually a raise, common parsing issue
            
            elif act["action"] == "calls":
                amount = act.get("amount", 0)
                # A call of 0 is suspicious
                if amount == 0:
                    result.warnings.append(
                        f"{street}: {act['player']} calls $0 — "
                        "should this be a check?"
                    )


def _check_player_consistency(hand: dict, result: ValidationResult):
    """Verify player names are consistent across all sections."""
    player_names = {p["name"] for p in hand.get("players", [])}
    
    action = hand.get("action", {})
    for street in ["preflop", "flop", "turn", "river"]:
        for act in action.get(street, []):
            if act["player"] not in player_names:
                result.errors.append(
                    f"Unknown player in action: '{act['player']}' "
                    f"(known players: {player_names})"
                )
    
    for sd in hand.get("showdown", []):
        if sd["player"] not in player_names:
            result.errors.append(
                f"Unknown player in showdown: '{sd['player']}'"
            )


def _check_board_progression(hand: dict, result: ValidationResult):
    """Verify board cards make sense (3 on flop, 1 on turn, 1 on river)."""
    board = hand.get("board", {})
    flop = board.get("flop", [])
    turn = board.get("turn")
    river = board.get("river")
    
    if flop and len(flop) != 3:
        result.errors.append(f"Flop has {len(flop)} cards, expected 3")
    
    if turn and not flop:
        result.errors.append("Turn card exists but no flop")
    
    if river and not turn:
        result.errors.append("River card exists but no turn")
    
    # Validate card format
    valid_ranks = set("23456789TJQKA")
    valid_suits = set("shdc")
    
    all_board = list(flop) + ([turn] if turn else []) + ([river] if river else [])
    for card in all_board:
        if len(card) != 2:
            result.errors.append(f"Invalid card format: '{card}'")
        elif card[0] not in valid_ranks:
            result.errors.append(f"Invalid rank in card: '{card}'")
        elif card[1] not in valid_suits:
            result.errors.append(f"Invalid suit in card: '{card}'")


def validate_session(hands: list[dict]) -> list[tuple[int, ValidationResult]]:
    """Validate all hands in a session. Returns list of (index, result) for failures."""
    issues = []
    for i, hand in enumerate(hands):
        result = validate_hand(hand)
        if not result.valid or result.warnings:
            issues.append((i, result))
    return issues
