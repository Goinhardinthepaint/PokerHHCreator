import json
with open('output/frames_cache_p0Q7LW64ecM_2000-2120.json') as f:
    frames = json.load(f)

f12 = frames[12]
print('Frame 12 confidence:', f12.get('confidence'))
print('Frame 12 board:', f12.get('board'))
for p in (f12.get('players') or []):
    print('  player:', p)

print()
print('--- Postflop frames (conf>=0.5) player names ---')
postflop = [(i, fr) for i, fr in enumerate(frames)
            if len(fr.get('board') or []) > 0 and (fr.get('confidence') or 0) >= 0.5]
print(f'Total: {len(postflop)}')
for fi, fr in postflop[:6]:
    names = [p.get('name') for p in (fr.get('players') or [])]
    board = fr.get('board')
    print(f'  Frame {fi} board={board}: {names}')

print()
print('--- All preflop frames with Dylan ---')
for i, fr in enumerate(frames):
    if fr.get('board'):
        break
    for p in (fr.get('players') or []):
        if (p.get('name') or '').lower() == 'dylan':
            print(f'  Frame {i} conf={fr.get("confidence")}: {p}')

print()
print('--- Raw hands JSON preflop actions ---')
try:
    with open('output/hands_raw.json') as f:
        raw = json.load(f)
    h = raw['hands'][0]
    print('preflop actions:')
    for a in h['action']['preflop']:
        print(' ', a)
except Exception as e:
    print('Error:', e)
