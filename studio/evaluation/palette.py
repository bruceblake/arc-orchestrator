"""The colour bible: a palette contract every render is measured against.

"Every model must use this. It's a contract. Otherwise every model could use
just a different shade of grey." — the space-game build this studio borrows
the practice from (2026-09). Bucket B's prose ("cold fluorescent over warm
rust") is for the visual judge; the palette is the part of it a PROGRAM can
check, so it is checked by one.

The check: sample a render's pixels, and measure what share of them sits
within a tolerance of SOME palette colour. A lit scene is never a flat swatch
— shading, fog and light falloff move every pixel — so the tolerance is
deliberately generous and the requirement is a share, not all of them. What
this catches is drift: a corridor that came back teal because one model
reached for a default material.

No imaging library is installed here, so this carries a minimal PNG reader
for exactly what the Godot render harness writes: 8-bit, non-interlaced,
RGB or RGBA. It refuses anything else rather than guessing.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

import config


class PaletteError(ValueError):
    pass


def parse_hex(c):
    s = str(c).strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise PaletteError(f"not a hex colour: {c!r}")
    try:
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        raise PaletteError(f"not a hex colour: {c!r}")


def load_palette(target):
    """Bucket B's palette as RGB tuples; [] when the target declares none."""
    pal = (target.get("bucket_b") or {}).get("palette_hex") or []
    return [parse_hex(c) for c in pal]


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else (b if pb <= pc else c)


def read_png(path):
    """(width, height, channels, rows) — rows are bytes of packed pixels."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PaletteError(f"{path}: not a PNG")
    i, idat, hdr = 8, b"", None
    while i < len(data):
        (n,) = struct.unpack(">I", data[i:i + 4])
        kind, chunk = data[i + 4:i + 8], data[i + 8:i + 8 + n]
        if kind == b"IHDR":
            hdr = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
        i += 12 + n
    if not hdr:
        raise PaletteError(f"{path}: no IHDR")
    w, h, depth, ctype, _comp, _filt, interlace = hdr
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise PaletteError(f"{path}: unsupported PNG (depth {depth}, type {ctype}, "
                           f"interlace {interlace}); expected 8-bit RGB/RGBA")
    ch = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * ch
    rows, prev, pos = [], bytearray(stride), 0
    for _ in range(h):
        f = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        for x in range(stride):
            a = line[x - ch] if x >= ch else 0
            b = prev[x]
            c = prev[x - ch] if x >= ch else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif f == 4:
                line[x] = (line[x] + _paeth(a, b, c)) & 255
        rows.append(bytes(line))
        prev = line
    return w, h, ch, rows


def conformance(path, palette, *, tolerance=None, step=6):
    """Share (0-1) of sampled pixels within `tolerance` of a palette colour."""
    tolerance = config.STUDIO_PALETTE_TOLERANCE if tolerance is None else tolerance
    if not palette:
        return None
    w, h, ch, rows = read_png(path)
    tol2 = tolerance * tolerance
    hit = total = 0
    for y in range(0, h, step):
        row = rows[y]
        for x in range(0, w, step):
            o = x * ch
            r, g, b = row[o], row[o + 1], row[o + 2]
            total += 1
            for pr, pg, pb in palette:
                if (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2 <= tol2:
                    hit += 1
                    break
    return hit / total if total else None


def check_images(images, target, *, minimum=None):
    """[(name, share)] for every image, and the failures below `minimum`."""
    minimum = config.STUDIO_PALETTE_MIN if minimum is None else minimum
    palette = load_palette(target)
    if not palette:
        return [], []
    results, failures = [], []
    for img in images:
        try:
            share = conformance(img["path"], palette)
        except (OSError, PaletteError) as exc:
            failures.append(f"{img.get('name')}: could not read the render ({exc})")
            continue
        results.append({"name": img.get("name"), "share": round(share or 0, 3)})
        if share is not None and share < minimum:
            failures.append(
                f"palette: camera '{img.get('name')}' is only {share:.0%} on the colour "
                f"bible (needs {minimum:.0%}) — something drifted off the palette")
    return results, failures
