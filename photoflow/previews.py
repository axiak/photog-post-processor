"""Proof generation - the cost control surface of the whole pipeline.

Policy (deliberate, and the reason review stays cheap):

tier 0  contact sheet   25 tiles of ~360 px, ONE image per 25 photos
tier 1  proof           ~800 px long edge, one image per photo under review
tier 2  master          full resolution, rendered only after the proof is approved

The model never sees tier 2. A 45 MP frame and its 800 px proof carry the same
norm coordinates, so approving a proof approves the master.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

from . import develop, geometry, render, util

__all__ = ["DEFAULT_PROOF_EDGE", "make_proof", "make_proofs", "render_proof"]

DEFAULT_PROOF_EDGE = 800


def _stem(path: str | Path) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(path).stem)


def make_proof(src: str | Path, work_root: str | Path, *,
               long_edge: int = DEFAULT_PROOF_EDGE,
               mode: str = "develop",
               xmp: Optional[str | Path] = None,
               master: Optional[str | Path] = None,
               worker: int = 0,
               quality: int = 92,
               style: Optional[str] = None) -> dict[str, Any]:
    """One source file -> one small proof + its exact scale record.

    mode="develop"  darktable develops it (faithful look, ~1-2 s/frame)
    mode="embedded" camera JPEG preview (fastest, but ungraded - good for culling;
                    silently falls back to develop when the preview is too small)

    Pass ``master`` to proof an already-developed file instead of developing
    the RAW a second time.
    """
    src = Path(src)
    work = Path(work_root)
    if master is not None:
        # the master is already rotated and cropped the way darktable sees it,
        # so it - not EXIF - is the authority on the frame
        frame_w, frame_h = render.image_size(master)
    else:
        frame_w, frame_h = develop.source_dims(src)
    out = work / "proofs" / f"{_stem(src)}.jpg"

    if mode not in ("develop", "embedded"):
        raise ValueError(f"mode must be 'develop' or 'embedded', got {mode!r}")

    if master is not None:
        develop.decode_raster(master, out, long_edge)
    else:
        # the embedded path can bail out (a real NEF exposes only a 160x112 EXIF
        # thumbnail), and then we develop with darktable - so always have a
        # configdir ready, even in embedded mode.
        cfg = develop.worker_configdir(work, worker,
                                       template=work / "dtcfg" / "template")
        develop.to_viewable(
            src, out,
            long_edge=long_edge,
            prefer_embedded=(mode == "embedded"),
            configdir=cfg,
            worker=worker,
            quality=quality,
            style=style,
        )

    actual_w, actual_h = render.image_size(out)
    # Guard: the recorded frame must describe the same orientation as the
    # artefact we actually produced. EXIF orientation and darktable's export
    # rotate can disagree (a real A7 II ARW: 6000x4000 stored, 4024x6022
    # exported), so trust the pixels.
    note = None
    frame_ar = frame_w / frame_h
    actual_ar = actual_w / actual_h
    if abs(actual_ar - frame_ar) / frame_ar > 0.02:
        if abs(actual_ar - (frame_h / frame_w)) / (frame_h / frame_w) <= 0.02:
            frame_w, frame_h = frame_h, frame_w
            note = "frame swapped to match the rendered orientation"
        else:
            frame_w, frame_h = actual_w, actual_h
            note = (f"frame replaced with the rendered size {actual_w}x{actual_h} "
                    f"(EXIF said something else)")
    pw, ph, scale = geometry.proof_dims(frame_w, frame_h, long_edge)

    record = {
        "image": src.name,
        "source": str(src),
        "proof": str(out),
        "frame": {"w": frame_w, "h": frame_h},
        "proof_size": {"w": actual_w, "h": actual_h},
        "scale": {"sx": actual_w / frame_w, "sy": actual_h / frame_h},
        "mode": mode,
        "declared": {"proof_w": pw, "proof_h": ph, "sx": scale.sx, "sy": scale.sy},
    }
    if master is not None:
        record["master"] = str(master)
    if note:
        record["note"] = note
    return record


def make_proofs(sources: Iterable[str | Path], work_root: str | Path, *,
                long_edge: int = DEFAULT_PROOF_EDGE,
                mode: str = "develop",
                jobs: int = 1,
                quality: int = 92,
                from_masters: bool = False,
                style: Optional[str] = None) -> list[dict[str, Any]]:
    """Proof a whole shoot. Each worker gets its own darktable configdir."""
    from .paths import Workdir

    wd = Workdir.create(work_root)
    sources = list(sources)
    # one configdir per concurrent worker: darktable takes a database lock
    slots = max(1, min(jobs, len(sources) or 1))

    def one(item: tuple[int, str | Path], worker: int) -> dict[str, Any]:
        idx, raw = item
        src = Path(raw)
        master = wd.find_master(src.name, style=style) if from_masters else None
        if from_masters and master is None:
            master = develop.make_master(src, wd.master_path(src.name),
                                         work_root=wd.root, worker=worker,
                                         style=style)
        return make_proof(src, wd.root, long_edge=long_edge, mode=mode,
                          master=master, worker=worker, quality=quality,
                          style=style)

    results = util.parallel_map_slot(one, list(enumerate(sources)), slots)
    util.write_json(wd.proof_index, results)
    return results


def render_proof(recipe, master: str | Path, work_root: str | Path, *,
                 long_edge: int = DEFAULT_PROOF_EDGE, tag: str = "") -> Path:
    """Re-render the proof through the *current* recipe.

    Always show the model the straightened/cropped frame, otherwise its crop
    coordinates refer to a frame it never saw.
    """
    suffix = f".{tag}" if tag else ""
    out = Path(work_root) / "proofs" / f"{_stem(master)}{suffix}.jpg"
    return render.render(master, recipe, out, long_edge=long_edge, quality=92)
