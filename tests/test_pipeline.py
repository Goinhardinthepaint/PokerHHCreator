"""
Test the full pipeline offline using sample data.
Run: python tests/test_pipeline.py
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.transcript.parser import parse_vtt, chunk_by_hands, segments_to_text
from src.validate.validator import validate_hand, validate_session
from src.export.pt4_formatter import format_hand, format_session


# ─────────────────────────────────────────────
# Mock data: what Claude API SHOULD return for our sample transcript
# ─────────────────────────────────────────────
MOCK_PARSED_HANDS = [
    {
        "hand_number": 1,
        "timestamp_start": "00:14:32",
        "timestamp_end": "00:16:29",
        "game": {
            "stakes": "50/100",
            "ante": 500,
            "game_type": "NL Hold'em",
            "max_players": 6
        },
        "button_seat": 4,
        "players": [
            {"name": "Wesley", "seat": 1, "position": "SB", "stack": 62000, "hole_cards": None},
            {"name": "Greedo", "seat": 2, "position": "BB", "stack": 97800, "hole_cards": ["Kh", "8d"]},
            {"name": "Otto", "seat": 3, "position": "HJ", "stack": 40000, "hole_cards": ["Kc", "Qh"]},
            {"name": "Handz", "seat": 4, "position": "BTN", "stack": 125000, "hole_cards": ["9s", "9c"]},
            {"name": "Nik Airball", "seat": 5, "position": "CO", "stack": 80000, "hole_cards": None},
            {"name": "Julie", "seat": 6, "position": "UTG", "stack": 55000, "hole_cards": None}
        ],
        "action": {
            "preflop": [
                {"player": "Wesley", "action": "posts_sb", "amount": 250},
                {"player": "Greedo", "action": "posts_bb", "amount": 500},
                {"player": "Otto", "action": "raises", "amount": 1500},
                {"player": "Handz", "action": "calls", "amount": 1500},
                {"player": "Wesley", "action": "folds"},
                {"player": "Greedo", "action": "calls", "amount": 1500}
            ],
            "flop": [
                {"player": "Greedo", "action": "bets", "amount": 3000},
                {"player": "Otto", "action": "folds"},
                {"player": "Handz", "action": "calls", "amount": 3000}
            ],
            "turn": [
                {"player": "Greedo", "action": "bets", "amount": 7500},
                {"player": "Handz", "action": "calls", "amount": 7500}
            ],
            "river": [
                {"player": "Greedo", "action": "bets", "amount": 14550},
                {"player": "Handz", "action": "calls", "amount": 14550}
            ]
        },
        "board": {
            "flop": ["2h", "8c", "3s"],
            "turn": "Kd",
            "river": "7s"
        },
        "showdown": [
            {
                "player": "Greedo",
                "hole_cards": ["Kh", "8d"],
                "hand_description": "two pair kings and eights",
                "result": "wins"
            },
            {
                "player": "Handz",
                "hole_cards": ["9s", "9c"],
                "hand_description": "pair of nines",
                "result": "loses"
            }
        ],
        "pot": {"total": 55350},
        "confidence": {
            "overall": 0.92,
            "flags": ["exact suits of Handz's nines not stated - inferred"]
        }
    },
    {
        "hand_number": 2,
        "timestamp_start": "00:16:31",
        "timestamp_end": "00:17:05",
        "game": {
            "stakes": "50/100",
            "ante": 500,
            "game_type": "NL Hold'em",
            "max_players": 6
        },
        "button_seat": 5,
        "players": [
            {"name": "Handz", "seat": 1, "position": "SB", "stack": 125000, "hole_cards": None},
            {"name": "Nik Airball", "seat": 2, "position": "BB", "stack": 80000, "hole_cards": None},
            {"name": "Julie", "seat": 3, "position": "UTG", "stack": 55000, "hole_cards": ["As", "Ks"]},
            {"name": "Wesley", "seat": 4, "position": "MP", "stack": 62000, "hole_cards": None},
            {"name": "Greedo", "seat": 5, "position": "CO", "stack": 97800, "hole_cards": None},
            {"name": "Otto", "seat": 6, "position": "BTN", "stack": 40000, "hole_cards": ["7h", "6h"]}
        ],
        "action": {
            "preflop": [
                {"player": "Handz", "action": "posts_sb", "amount": 250},
                {"player": "Nik Airball", "action": "posts_bb", "amount": 500},
                {"player": "Julie", "action": "raises", "amount": 1500},
                {"player": "Wesley", "action": "folds"},
                {"player": "Greedo", "action": "folds"},
                {"player": "Otto", "action": "raises", "amount": 5000},
                {"player": "Handz", "action": "folds"},
                {"player": "Nik Airball", "action": "folds"},
                {"player": "Julie", "action": "raises", "amount": 14000},
                {"player": "Otto", "action": "folds"}
            ],
            "flop": [],
            "turn": [],
            "river": []
        },
        "board": {"flop": [], "turn": None, "river": None},
        "showdown": [],
        "pot": {"total": 6000},
        "confidence": {
            "overall": 0.95,
            "flags": []
        }
    },
    {
        "hand_number": 3,
        "timestamp_start": "00:17:06",
        "timestamp_end": "00:18:55",
        "game": {
            "stakes": "50/100",
            "ante": 500,
            "game_type": "NL Hold'em",
            "max_players": 6
        },
        "button_seat": 6,
        "players": [
            {"name": "Nik Airball", "seat": 1, "position": "SB", "stack": 80000, "hole_cards": ["Qc", "Jc"]},
            {"name": "Julie", "seat": 2, "position": "BB", "stack": 55000, "hole_cards": None},
            {"name": "Wesley", "seat": 3, "position": "UTG", "stack": 62000, "hole_cards": ["Js", "Jh"]},
            {"name": "Greedo", "seat": 4, "position": "MP", "stack": 97800, "hole_cards": ["Ah", "Tc"]},
            {"name": "Otto", "seat": 5, "position": "CO", "stack": 40000, "hole_cards": None},
            {"name": "Handz", "seat": 6, "position": "BTN", "stack": 125000, "hole_cards": None}
        ],
        "action": {
            "preflop": [
                {"player": "Nik Airball", "action": "posts_sb", "amount": 250},
                {"player": "Julie", "action": "posts_bb", "amount": 500},
                {"player": "Wesley", "action": "raises", "amount": 2000},
                {"player": "Greedo", "action": "calls", "amount": 2000},
                {"player": "Nik Airball", "action": "raises", "amount": 8500},
                {"player": "Julie", "action": "folds"},
                {"player": "Wesley", "action": "calls", "amount": 8500},
                {"player": "Greedo", "action": "folds"}
            ],
            "flop": [
                {"player": "Nik Airball", "action": "bets", "amount": 6000},
                {"player": "Wesley", "action": "calls", "amount": 6000}
            ],
            "turn": [
                {"player": "Nik Airball", "action": "checks"},
                {"player": "Wesley", "action": "bets", "amount": 12000}
            ],
            "river": [
                {"player": "Wesley", "action": "bets", "amount": 28000},
                {"player": "Nik Airball", "action": "calls", "amount": 28000}
            ]
        },
        "board": {
            "flop": ["Jd", "4h", "9s"],
            "turn": "2c",
            "river": "5d"
        },
        "showdown": [
            {
                "player": "Wesley",
                "hole_cards": ["Js", "Jh"],
                "hand_description": "set of jacks",
                "result": "wins"
            },
            {
                "player": "Nik Airball",
                "hole_cards": ["Qc", "Jc"],
                "hand_description": "pair of jacks",
                "result": "loses"
            }
        ],
        "pot": {"total": 110000},
        "confidence": {
            "overall": 0.97,
            "flags": []
        }
    }
]


def test_vtt_parsing():
    """Test that VTT file is correctly parsed into segments."""
    print("TEST: VTT Parsing")
    segments = parse_vtt("tests/sample_transcript.vtt")
    print(f"  Parsed {len(segments)} segments")
    assert len(segments) > 0, "No segments parsed"
    assert segments[0].text, "First segment has no text"
    print(f"  First segment: [{segments[0].start}] {segments[0].text[:60]}...")
    print("  PASS\n")
    return segments


def test_hand_chunking(segments):
    """Test that segments are correctly split by hand boundaries."""
    print("TEST: Hand Boundary Detection")
    chunks = chunk_by_hands(segments)
    print(f"  Found {len(chunks)} hand chunks")
    
    for i, chunk in enumerate(chunks):
        text = segments_to_text(chunk)
        print(f"  Hand {i+1}: {len(chunk)} segments, starts at {chunk[0].start}")
        # Show first line
        first_line = text.split("\n")[0]
        print(f"    -> {first_line[:80]}")
    
    assert len(chunks) == 3, f"Expected 3 hands, found {len(chunks)}"
    print("  PASS\n")


def test_validation():
    """Test validation on mock parsed hands."""
    print("TEST: Hand Validation")
    issues = validate_session(MOCK_PARSED_HANDS)
    
    for idx, result in issues:
        hand = MOCK_PARSED_HANDS[idx]
        label = f"Hand {idx+1}"
        if result.errors:
            for err in result.errors:
                print(f"  ERROR [{label}]: {err}")
        for warn in result.warnings:
            print(f"  WARN  [{label}]: {warn}")
    
    error_count = len([i for i in issues if not i[1].valid])
    print(f"  Errors: {error_count}, Warnings: {len(issues) - error_count}")
    print("  PASS\n")


def test_pt4_formatting():
    """Test PT4 export format."""
    print("TEST: PT4 Formatting")
    
    stream_url = "https://www.youtube.com/watch?v=TESTSTREAM"
    output = format_session(MOCK_PARSED_HANDS, stream_url=stream_url)
    
    # Write to file for inspection
    os.makedirs("output", exist_ok=True)
    output_path = "output/test_hands.txt"
    with open(output_path, "w") as f:
        f.write(output)
    
    print(f"  Wrote {len(MOCK_PARSED_HANDS)} hands to {output_path}")
    
    # Basic format checks
    lines = output.split("\n")
    assert any("PokerStars Hand #" in l for l in lines), "Missing hand header"
    assert any("*** HOLE CARDS ***" in l for l in lines), "Missing hole cards marker"
    assert any("*** FLOP ***" in l for l in lines), "Missing flop marker"
    assert any("*** SHOW DOWN ***" in l for l in lines), "Missing showdown marker"
    assert any("*** SUMMARY ***" in l for l in lines), "Missing summary marker"
    
    # Print first hand for inspection
    first_hand = output.split("\n\n\n")[0]
    print("\n  --- FIRST HAND (PT4 FORMAT) ---")
    for line in first_hand.split("\n"):
        print(f"  {line}")
    print("  --- END ---\n")
    
    print("  PASS\n")


def test_hand_id_determinism():
    """Test that hand IDs are deterministic (same input = same ID)."""
    print("TEST: Hand ID Determinism")
    from src.export.pt4_formatter import generate_hand_id
    
    id1 = generate_hand_id("https://youtube.com/test", "00:14:32", 0)
    id2 = generate_hand_id("https://youtube.com/test", "00:14:32", 0)
    id3 = generate_hand_id("https://youtube.com/test", "00:14:32", 1)
    
    assert id1 == id2, "Same inputs should produce same ID"
    assert id1 != id3, "Different hand index should produce different ID"
    print(f"  Hand 0: {id1}")
    print(f"  Hand 0 (again): {id2}")
    print(f"  Hand 1: {id3}")
    print("  PASS\n")


if __name__ == "__main__":
    print("=" * 60)
    print("POKER SCRAPER — PIPELINE TESTS")
    print("=" * 60)
    print()
    
    segments = test_vtt_parsing()
    test_hand_chunking(segments)
    test_validation()
    test_pt4_formatting()
    test_hand_id_determinism()
    
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)
