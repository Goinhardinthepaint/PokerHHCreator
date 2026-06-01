import json

with open("output/hands_gemini_raw.json") as f:
    data = json.load(f)

for i, h in enumerate(data):
    straddle = h.get("straddle") or {}
    print(f"Hand {i+1}:")
    for p in h.get("players", []):
        seat  = p.get("seat", "?")
        name  = p.get("name", "?")
        pos   = p.get("position", "?")
        stack = p.get("stack", 0)
        hc    = p.get("hole_cards") or "null"
        print(f"  seat {seat}: {name:<12} pos={pos:<8} stack={stack:>7}  hc={hc}")
    if straddle:
        print(f"  straddle: {straddle}")
    board = h.get("board") or {}
    print(f"  board: flop={board.get('flop')}  turn={board.get('turn')}  river={board.get('river')}")
    print()
