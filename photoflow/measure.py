"""Numeric measurements that travel with the proofs to the reviewing model.

Pixels are expensive, numbers are cheap. Handing the model
"p99=0.999, clipped_high=4.1%, p50=0.71" costs a handful of tokens and turns a
vague "looks a bit bright" into an actionable exposure delta.

Everything here is computed on a downscaled copy (default 1024 px long edge) so
that sharpness numbers are comparable *across* a whole shoot - a 45 MP frame
and a 20 MP frame must not be scored on different scales.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import numpy as np

__all__ = ["measure", "metrics_summary", "srgb_to_linear", "suggest_exposure_ev"]

_LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)
_CLIP_HIGH = 0.995
_CLIP_LOW = 0.004


def _load_rgb01(path: str | Path, long_edge: int, allow_16bit: bool = False) -> np.ndarray:
    """Load an image as float RGB in [0,1] at a comparable scale."""
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        if not allow_16bit and im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.thumbnail((long_edge, long_edge), Image.LANCZOS)
        arr = np.asarray(im)

    if arr.dtype == np.uint8:
        return arr.astype(np.float64) / 255.0
    if arr.dtype == np.uint16:
        return arr.astype(np.float64) / 65535.0
    if np.issubdtype(arr.dtype, np.floating):
        return np.clip(arr.astype(np.float64), 0.0, 1.0)
    raise TypeError(f"unsupported array dtype {arr.dtype} from {path}")


def _to_luma(rgb: np.ndarray) -> np.ndarray:
    if rgb.ndim == 2:
        return rgb
    if rgb.shape[2] == 1:
        return rgb[..., 0]
    return rgb[..., :3] @ _LUMA


def srgb_to_linear(x):
    x = np.asarray(x, dtype=np.float64)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _laplacian_var(luma: np.ndarray) -> float:
    """4-neighbour Laplacian energy: the standard cheap focus metric."""
    c = luma[1:-1, 1:-1]
    lap = (4.0 * c - luma[:-2, 1:-1] - luma[2:, 1:-1]
           - luma[1:-1, :-2] - luma[1:-1, 2:])
    return float(np.var(lap) * 1e4)


def _tenengrad(luma: np.ndarray) -> float:
    gx = np.zeros_like(luma)
    gy = np.zeros_like(luma)
    gx[:, 1:-1] = luma[:, 2:] - luma[:, :-2]
    gy[1:-1, :] = luma[2:, :] - luma[:-2, :]
    return float(np.mean(gx * gx + gy * gy) * 1e4)


def _colorfulness(rgb: np.ndarray) -> float:
    """Hasler & Suesstrunk colourfulness on a 0-100ish scale."""
    if rgb.ndim != 3 or rgb.shape[2] < 3:
        return 0.0
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    return float(np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2))


def suggest_exposure_ev(p50: float, target: float = 0.18, clip: float = 1.5) -> float:
    """Exposure delta that moves the median to 18% linear grey.

    Returned in stops. A suggestion only - a backlit frame with a bright sky
    legitimately wants a different answer, which is why the model gets the raw
    percentiles too and has the final say.
    """
    lin = float(srgb_to_linear(max(p50, 1e-4)))
    ev = float(np.log2(target / lin))
    return float(np.clip(round(ev, 2), -clip, clip))


def measure(path: str | Path, long_edge: int = 1024) -> dict[str, Any]:
    """Measurements used for culling and for steering the reviewing model."""
    rgb = _load_rgb01(path, long_edge)
    luma = _to_luma(rgb)
    h, w = luma.shape

    pct = np.percentile(luma, [0.1, 1, 5, 50, 95, 99, 99.9])
    clipped_high = float(np.mean(luma >= _CLIP_HIGH))
    clipped_low = float(np.mean(luma <= _CLIP_LOW))
    channel_clip = 0.0
    if rgb.ndim == 3 and rgb.shape[2] >= 3:
        channel_clip = float(np.mean(np.any(rgb[..., :3] >= 0.999, axis=2)))

    return {
        "path": str(path),
        "measure_size": [int(w), int(h)],
        "mean_luma": round(float(luma.mean()), 4),
        "std_luma": round(float(luma.std()), 4),
        "percentiles": {
            "p0_1": round(float(pct[0]), 4), "p1": round(float(pct[1]), 4),
            "p5": round(float(pct[2]), 4), "p50": round(float(pct[3]), 4),
            "p95": round(float(pct[4]), 4), "p99": round(float(pct[5]), 4),
            "p99_9": round(float(pct[6]), 4),
        },
        "clipped_high": round(clipped_high, 5),
        "clipped_low": round(clipped_low, 5),
        "channel_clipped": round(channel_clip, 5),
        "sharpness": round(_laplacian_var(luma), 3),
        "tenengrad": round(_tenengrad(luma), 3),
        "colorfulness": round(_colorfulness(rgb), 3),
        "suggest_exposure_ev": suggest_exposure_ev(float(pct[3])),
        "flags": _flags(clipped_high, clipped_low, float(pct[3]), channel_clip),
    }


def _flags(clip_hi: float, clip_lo: float, p50: float, channel_clip: float) -> list[str]:
    flags: list[str] = []
    if clip_hi > 0.05:
        flags.append("highlights_blown")
    elif clip_hi > 0.01:
        flags.append("highlights_hot")
    if clip_lo > 0.05:
        flags.append("shadows_crushed")
    if channel_clip > 0.02:
        flags.append("channel_clipping")  # e.g. red dresses / stage lights
    if p50 > 0.72:
        flags.append("overexposed_subject")
    elif p50 < 0.22:
        flags.append("underexposed_subject")
    return flags


def metrics_summary(m: dict[str, Any]) -> str:
    """One-line, token-cheap form for stuffing into a review prompt."""
    p = m["percentiles"]
    flags = ",".join(m["flags"]) or "-"
    return (f"p1={p['p1']:.3f} p50={p['p50']:.3f} p99={p['p99']:.3f} "
            f"clip+={m['clipped_high']*100:.1f}% clip-={m['clipped_low']*100:.1f}% "
            f"sharp={m['sharpness']:.1f} exp_sugg={m['suggest_exposure_ev']:+.2f}EV "
            f"flags={flags}")
