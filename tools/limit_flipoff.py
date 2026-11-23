"""The "limit" buddy animation, with Clawd flipping you off: it raises its right arm into a fist with the middle
finger up and pumps it while it rages (its right hand: your left, since it faces you). Patches the limit frames inside clawd_sprites.BLOB (idempotent: the gesture
area is cleared and redrawn on every run).

    .venv/Scripts/python.exe tools/limit_flipoff.py [--preview out.png] [--write]

The firmware copy comes from the same blob: run Clawdmeter's tools/add_csb_anims.py afterwards.
"""
import argparse
import base64
import json
import re
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SPRITES = HERE / "clawd_sprites.py"
BG, CALM, BODY, DARK = 1, 3, 4, 5  # palette indices used by "limit"


def load():
    src = SPRITES.read_text(encoding="utf-8")
    blob = re.search(r'BLOB = "([^"]+)"', src).group(1)
    return src, blob, json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))


def body_left(f):
    """Left edge of the body (row 12 has no arms); the frames shake, so it moves."""
    row = f[12 * 20:13 * 20]
    best, start = (0, 0), None   # the longest solid run is the body ("!" marks are single cells)
    for x in range(21):
        solid = x < 20 and row[x] in (CALM, BODY)
        if solid and start is None:
            start = x
        elif not solid and start is not None:
            best = max(best, (x - start, -start))
            start = None
    return -best[1]


def gesture(f, stage, dy=0):
    """Clawd's right hand (your left: it faces you). stage 1: arm half up with a fist; stage 2: arm up, middle
    finger up. dy shifts it down (the pump)."""
    f = list(f)
    lb = body_left(f)
    col = BODY
    # clear that side above the hips (arm stub, hand, the "!" marks there); the other side keeps its marks
    for y in range(0, 11):
        for x in range(0, lb):
            f[y * 20 + x] = BG

    def put(x, y, v=col):
        if 0 <= x < 20 and 0 <= y < 20:
            f[y * 20 + x] = v
    # a 1-pixel gap between the raised arm and the body, so the fist reads as a hand, not a horn
    put(lb - 1, 8); put(lb - 2, 8)                         # shoulder + elbow
    if stage == 1:
        for y in (6, 7): put(lb - 3, y)                    # forearm, half up
        for y in (4, 5):
            for x in range(lb - 4, lb - 1): put(x, y)      # fist
        put(lb - 4, 4, DARK); put(lb - 2, 4, DARK)
        return f
    for y in range(6 + dy, 9): put(lb - 3, y)                           # forearm
    for y in range(3 + dy, 6 + dy):
        for x in range(lb - 4, lb - 1): put(x, y)                       # fist, 3 wide
    put(lb - 4, 3 + dy, DARK); put(lb - 2, 3 + dy, DARK)                # folded fingers either side
    for y in range(0 + dy, 3 + dy): put(lb - 3, y)                      # the middle finger
    return f


def build(orig):
    """8 frames: calm, angry, arm coming up, then the finger pumping (up, down, up, down, up)."""
    out = [orig[0], orig[1], gesture(orig[2], 1)]
    for i, k in enumerate(range(3, 8)):
        out.append(gesture(orig[k], 2, dy=i % 2))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    src, blob, arr = load()
    lim = next(x for x in arr if x["n"] == "limit")
    orig = lim.get("_orig") or lim["f"]
    frames = build(orig)
    if a.preview:
        from PIL import Image
        pal = [tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in lim["p"]]
        S = 14
        sheet = Image.new("RGB", (len(frames) * (20 * S + 10), 20 * S), (60, 60, 60))
        for k, fr in enumerate(frames):
            im = Image.new("RGB", (20, 20)); im.putdata([pal[v] for v in fr])
            sheet.paste(im.resize((20 * S, 20 * S), Image.NEAREST), (k * (20 * S + 10), 0))
        sheet.save(a.preview); print("preview", a.preview)
    if a.write:
        lim["_orig"] = orig   # kept so a re-run starts from the original, not from the patched frames
        lim["f"] = frames
        new = base64.b64encode(zlib.compress(json.dumps(arr, separators=(",", ":")).encode("utf-8"), 9)).decode("ascii")
        SPRITES.write_text(src.replace(blob, new), encoding="utf-8")
        print("written", SPRITES)


if __name__ == "__main__":
    main()
