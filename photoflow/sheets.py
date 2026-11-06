"""Contact sheets: the cheapest tier of model review.

One 25-tile sheet costs about as much as one full-size proof but covers 25
photos. That asymmetry is the whole budget strategy:

    tier 0  sheet   pick which frames are worth looking at closely
    tier 1  proof   per-frame verdict + crop + adjust

Cells carry a stable index label ("#07  IMG_1234") so the model can reference
frames unambiguously and we can map an answer back to a file without relying on
positional guessing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from . import util

__all__ = ["build_sheet", "cell_label"]


def cell_label(index: int, name: str, max_chars: int = 18) -> str:
    stem = Path(name).stem
    if len(stem) > max_chars:
        stem = stem[: max_chars - 1] + "\u2026"
    return f"#{index:02d} {stem}"


def build_sheet(items: Sequence[dict[str, Any]], out: str | Path, *,
                tile: int = 360,
                cols: int = 6,
                pointsize: int = 20,
                quality: int = 88,
                start_index: int = 0) -> dict[str, Any]:
    """Montage proofs into a labelled grid + a cell->image map.

    ``items`` entries need ``proof`` (path) and ``image`` (name).
    """
    if not items:
        raise ValueError("build_sheet needs at least one item")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    argv: list[str] = ["magick", "montage"]
    cells: list[dict[str, Any]] = []
    for offset, item in enumerate(items):
        index = start_index + offset
        label = cell_label(index, str(item.get("image") or item["proof"]))
        argv += ["-label", label, str(item["proof"])]
        cells.append({
            "index": index,
            "label": label,
            "image": item.get("image"),
            "proof": str(item["proof"]),
        })

    rows = (len(items) + cols - 1) // cols
    argv += [
        "-tile", f"{cols}x{rows}",
        "-geometry", f"{tile}x{tile}+6+6",
        "-pointsize", str(pointsize),
        "-background", "#111111",
        "-fill", "#e8e8e8",
        "-quality", str(int(quality)),
        str(out),
    ]
    util.require("magick")
    util.run(argv, timeout=600)

    w, h = _size(out)
    return {
        "sheet": str(out),
        "sheet_size": {"w": w, "h": h},
        "tile": tile, "cols": cols, "rows": rows,
        "cells": cells,
    }


def _size(path: Path) -> tuple[int, int]:
    from PIL import Image
    with Image.open(path) as im:
        return im.width, im.height


def plan_sheets(items: Iterable[dict[str, Any]], out_dir: str | Path, *,
               per_sheet: int = 25, **kwargs: Any) -> list[dict[str, Any]]:
    items = list(items)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sheets: list[dict[str, Any]] = []
    for page, start in enumerate(range(0, len(items), per_sheet), start=1):
        chunk = items[start:start + per_sheet]
        sheets.append(build_sheet(
            chunk, out_dir / f"sheet_{page:03d}.jpg",
            start_index=start, **kwargs))
    return sheets
