"""The "limit" buddy animation, with Clawd flipping you off: it raises its right arm into a fist with the middle
fingers up with both hands and pumps them in turn while it rages. Patches the limit frames inside clawd_sprites.BLOB (idempotent: the gesture
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


def body_span(f):
    """(left, right) of the body in row 12 (no arms there): the longest solid run ("!" marks are single cells)."""
    row = f[12 * 20:13 * 20]
    best, start = (0, 0, 0), None
    for x in range(21):
        solid = x < 20 and row[x] in (CALM, BODY)
        if solid and start is None:
            start = x
        elif not solid and start is not None:
            best = max(best, (x - start, -start, x - 1))
            start = None
    return -best[1], best[2]


def shift(f, dx):
    """The frame moved dx columns (the original frames shake left and right; both raised arms need it centred)."""
    out = [BG] * 400
    for y in range(20):
        for x in range(20):
            nx = x + dx
            if 0 <= nx < 20:
                out[y * 20 + nx] = f[y * 20 + x]
    return out


def gesture(f, stage, dys=(0, 0), left=5):
    """Both hands up. The body is moved so its left edge is at `left` (5 or 4: a 1-pixel wobble instead of the
    original shake), which leaves room for an arm, a 1-pixel gap and a 3-wide fist on each side. stage 1: both arms
    half up with fists; stage 2: arms up, middle fingers up. dys = (its right hand, its left hand) pump offsets."""
    lb, rb = body_span(f)
    f = shift(f, left - lb)
    lb, rb = body_span(f)
    for y in range(0, 11):                       # clear above the hips on both sides (arm stubs, "!" marks)
        for x in list(range(0, lb)) + list(range(rb + 1, 20)):
            f[y * 20 + x] = BG

    def put(x, y, v=BODY):
        if 0 <= x < 20 and 0 <= y < 20:
            f[y * 20 + x] = v

    for side, dy in ((-1, dys[0]), (1, dys[1])):          # -1: your left (its right hand), +1: your right
        edge = lb if side < 0 else rb
        o = lambda n: edge + side * n                     # n columns out from the body
        put(o(1), 8); put(o(2), 8)                        # shoulder + elbow (1-pixel gap above to the fist)
        if stage == 1:
            for y in (6, 7): put(o(3), y)
            for y in (4, 5):
                for n in (2, 3, 4): put(o(n), y)
            put(o(2), 4, DARK); put(o(4), 4, DARK)
            continue
        for y in range(6 + dy, 9): put(o(3), y)                         # forearm
        for y in range(3 + dy, 6 + dy):
            for n in (2, 3, 4): put(o(n), y)                            # fist, 3 wide
        put(o(2), 3 + dy, DARK); put(o(4), 3 + dy, DARK)                # folded fingers either side
        for y in range(0 + dy, 3 + dy): put(o(3), y)                    # the middle finger
    return f


def build(orig):
    """8 frames: calm, angry, both arms coming up, then both middle fingers pumping in turn."""
    out = [orig[0], orig[1], gesture(orig[2], 1)]
    for i, k in enumerate(range(3, 8)):
        out.append(gesture(orig[k], 2, dys=(i % 2, 1 - i % 2), left=5 - i % 2))
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
