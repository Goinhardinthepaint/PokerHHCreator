import anthropic
import base64
import os
import sys
import winreg

def _load_env_key(name: str) -> str:
    """Load an API key from the Windows user environment registry."""
    val = os.environ.get(name)
    if val:
        return val
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        val, _ = winreg.QueryValueEx(key, name)
    os.environ[name] = val
    return val

_load_env_key("ANTHROPIC_API_KEY")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
client = anthropic.Anthropic()

# Load the 6 screenshots from the hand we just verified
frames_dir = r"C:\Poker Scaper\downloads\frames\test_greedo_kk"
os.makedirs(frames_dir, exist_ok=True)

# Extract 6 frames at key moments using ffmpeg (skip if already on disk):
_video = "downloads/p0Q7LW64ecM_1080p.webm"
_fd = frames_dir.replace("\\", "/")
for _ts, _n in [(3246, 1), (3258, 2), (3268, 3), (3278, 4), (3288, 5), (3295, 6)]:
    _out = f"{_fd}/frame{_n}.jpg"
    if os.path.exists(_out):
        print(f"Frame {_n} already exists, skipping ffmpeg")
        continue
    os.system(
        f'ffmpeg -ss {_ts} -i "{_video}" -frames:v 1 -q:v 2 "{_out}" -update 1 -y'
    )

# Load frames as base64
images = []
for i in range(1, 7):
    path = f"{frames_dir}/frame{i}.jpg"
    if not os.path.exists(path):
        print(f"Missing: {path}")
        sys.exit(1)
    with open(path, "rb") as f:
        images.append(base64.standard_b64encode(f.read()).decode("utf-8"))
    print(f"Loaded frame {i}: {os.path.getsize(path)} bytes")

# Build the message with all 6 images
content = []
for i, img_b64 in enumerate(images):
    content.append({
        "type": "text",
        "text": f"Frame {i+1}:"
    })
    content.append({
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": img_b64
        }
    })

content.append({
    "type": "text",
    "text": """These are 6 sequential screenshots from a Hustler Casino Live poker hand. $50/$100 NL with $100 BB ante spread across all players.

Players at the table:
Seat 2: Airball
Seat 3: Greedo
Seat 4: Francisco
Seat 5: Dylan
Seat 6: Otto
Seat 7: Steve
Seat 9: Alex

Read the overlay graphics carefully and tell me:
1. Who is the button? (infer from SB/BB positions shown)
2. Each player's hole cards (read rank and suit from the overlay)
3. Complete preflop action in seat order (infer folds from position - if seat 9 acts after seat 3, seats 4-8 folded)
4. Flop cards
5. Flop action in order
6. Turn card
7. Turn action
8. Who wins and how much

Be precise about card suits: red = hearts or diamonds, black = spades or clubs. Look at the symbol shape to distinguish."""
})

print("\nSending to Claude API...")
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=4096,
    messages=[{"role": "user", "content": content}]
)

print("\n" + "=" * 60)
print("CLAUDE API RESPONSE:")
print("=" * 60)
print(response.content[0].text)
