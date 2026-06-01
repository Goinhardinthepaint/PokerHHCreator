"""
Quick diagnostic: analyze a handful of key frames and dump the raw JSON Claude returns.
Run: python debug_frames.py
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.vision.analyzer import FrameAnalyzer

FRAMES_DIR = r"downloads\frames\p0Q7LW64ecM_2000-2120"
CHECK = [1, 11, 12, 44, 57, 106]  # frame numbers (1-based)

analyzer = FrameAnalyzer()

for n in CHECK:
    path = os.path.join(FRAMES_DIR, f"frame_{n:06d}.jpg")
    print(f"\n{'='*60}")
    print(f"FRAME {n:03d}: {path}")
    result = analyzer.analyze_frame(path)
    print(json.dumps(result, indent=2))
