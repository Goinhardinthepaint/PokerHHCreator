import json
with open('output/frames_cache_p0Q7LW64ecM_2000-2120.json') as f:
    frames = json.load(f)

print('=== All preflop frames with action_text ===')
for i, fr in enumerate(frames):
    if fr.get('board'):
        break
    if (fr.get('confidence') or 0) < 0.5:
        continue
    for p in (fr.get('players') or []):
        at = p.get('action_text') or ''
        if at and 'TO CALL' not in at.upper() and at.upper() != 'OPTION':
            print(f"  Frame {i}: {p.get('name')} action_text={repr(at)} cur_bet={p.get('current_bet')}")
