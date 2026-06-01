import anthropic
import base64
import os
import sys
import winreg

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── API key ───────────────────────────────────────────────────────────────────

def _load_api_key(name: str) -> str:
    val = os.environ.get(name)
    if val:
        return val
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            val, _ = winreg.QueryValueEx(key, name)
        os.environ[name] = val
        return val
    except (FileNotFoundError, OSError):
        raise RuntimeError(f"{name} not set in environment or Windows registry")

_load_api_key("ANTHROPIC_API_KEY")
client = anthropic.Anthropic()

frames_dir = "downloads/frames/test_greedo_kk"
messages = []

# Load all scan frames
def load_frame(path):
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode()

scans = sorted([
    f for f in os.listdir(frames_dir)
    if f.startswith("scan_") and 22 <= int(f.split("_")[1].split(".")[0]) <= 40
])

print(f"Processing {len(scans)} frames sequentially...")

for i, fname in enumerate(scans):
    b64 = load_frame(os.path.join(frames_dir, fname))

    if i == 0:
        text = (
            f"Frame {i+1} of {len(scans)}. This is a $50/100 NL Hold'em hand from "
            "Hustler Casino Live. Players: Airball seat 2, Greedo seat 3, Francisco seat 4, "
            "Dylan seat 5, Otto seat 6, Steve seat 7, Alex seat 9. "
            "What do you see on the overlay? Who is visible, what are their cards, "
            "positions, actions, stacks?"
        )
    elif i == len(scans) - 1:
        text = (
            f"Frame {i+1} of {len(scans)} (final frame). What do you see? "
            "Now compile everything into the complete hand. Give me JSON with players, "
            "all preflop actions in seat order (infer folds from position), board cards, "
            "postflop actions, and winner."
        )
    else:
        text = (
            f"Frame {i+1} of {len(scans)}. What changed from the previous frame? "
            "Any new players visible, new actions, board cards, or pot changes?"
        )

    messages.append({"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
        {"type": "text", "text": text},
    ]})

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=messages,
    )

    reply = response.content[0].text
    messages.append({"role": "assistant", "content": reply})
    print(f"\n--- Frame {i+1} ({fname}) ---")
    print(reply[:300])

print("\n\n=== FINAL RESPONSE ===")
print(messages[-1]["content"])
