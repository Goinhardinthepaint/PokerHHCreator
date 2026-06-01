"""
Quick OCR proof-of-concept on HCL overlay frames using EasyOCR.
PaddlePaddle has no Python 3.14 wheel; EasyOCR (same pipeline) is used instead.
"""

import cv2
import easyocr
import numpy as np

FRAME = r"C:\Poker Scaper\downloads\frames\p0Q7LW64ecM_2300-4100\frame_000100.jpg"

img = cv2.imread(FRAME)
h, w = img.shape[:2]
print(f"Frame size: {w}x{h}\n")

reader = easyocr.Reader(["en"], gpu=False, verbose=False)


def ocr_crop(label, y0f, y1f, x0f, x1f):
    y0, y1 = int(h * y0f), int(h * y1f)
    x0, x1 = int(w * x0f), int(w * x1f)
    crop = img[y0:y1, x0:x1]
    cv2.imwrite(f"C:/Poker Scaper/output/ocr_poc_{label}.jpg", crop)

    results = reader.readtext(crop)

    print("=" * 60)
    print(f"  Region: {label}  ({x0},{y0}) -> ({x1},{y1})  [{x1-x0}x{y1-y0}px]")
    print("=" * 60)
    if not results:
        print("  (no text detected)")
    for bbox, text, conf in results:
        tl = bbox[0]
        br = bbox[2]
        print(f"  [{conf:.2f}]  {repr(text)}")
        print(f"         box: ({int(tl[0])},{int(tl[1])}) -> ({int(br[0])},{int(br[1])})")
    print()


# Bottom-left: player boxes
ocr_crop("player_boxes", y0f=0.55, y1f=1.0, x0f=0.0, x1f=0.45)

# Bottom-right: pot / stakes
ocr_crop("pot_stakes",   y0f=0.55, y1f=1.0, x0f=0.70, x1f=1.0)
