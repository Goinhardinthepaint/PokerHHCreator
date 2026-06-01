import sys
sys.path.insert(0, ".")
from src.engine.poker_state_machine import PokerStateMachine

sm = PokerStateMachine("50/100")
sm.set_button(1)
for seat, name, pos, stack in [
    (1, "Steve", "BTN", 15000),
    (2, "Dylan", "SB", 12000),
    (3, "Airball", "BB", 30000),
    (4, "Alex", "UTG", 18000),
    (5, "Francisco", "CO", 22000),
]:
    sm.add_player(seat, name, stack, pos)

for name in ["Steve","Dylan","Airball","Alex","Francisco"]:
    sm.post_ante(name, 20)
sm.post_blind("Dylan", 50, "sb")
sm.post_blind("Airball", 100, "bb")
sm.begin_preflop()

print("Initial queue:", list(sm._players_to_act))

for player, action, amount in [
    ("Alex", "folds", 0),
    ("Francisco", "folds", 0),
    ("Steve", "raises", 300),
    ("Dylan", "folds", 0),
    ("Airball", "folds", 0),
]:
    r = sm.action(player, action, amount)
    print(f"  {player} {action}: applied={r}  hand_complete={sm._hand_complete}  queue={list(sm._players_to_act)}")

print("\nFinal hand_complete:", sm._hand_complete)
print("Non-folded:", [p.name for p in sm._players if p.status != "folded"])
print("Active:", [p.name for p in sm._players if p.status == "active"])
