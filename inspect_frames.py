import json
with open('output/frames_cache_p0Q7LW64ecM_2000-2120.json') as f:
    frames = json.load(f)
preflop = [(i, fr) for i, fr in enumerate(frames)
           if not fr.get('board') and (fr.get('confidence') or 0) >= 0.5]
for idx, (fi, fr) in enumerate(preflop[:10]):
    print(f"--- Frame {fi} (conf={fr.get('confidence')}) pot={fr.get('pot')} ---")
    for p in (fr.get('players') or []):
        print(f"  {p.get('name')}: pos={p.get('position')} cur_bet={p.get('current_bet')} action={repr(p.get('action_text'))}")
