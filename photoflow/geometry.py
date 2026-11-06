"""Resolution-independent geometry.

Coordinate frames
-----------------
source  full-resolution pixel grid of the *developed* image, (frame_w, frame_h)
proof   the downscaled copy handed to the reviewing model, (pw, ph)
norm    recipe space: floats in [0, 1] relative to the source frame

The invariant this module exists to protect:

    norm coordinates are the only thing that crosses resolutions.

A model looking at an 800 px proof says "crop from 6% to 58% of the width".
That is stored as-is in the recipe and renders correctly on the 6240 px master,
with no per-resolution bookkeeping and no rounding drift.

All rotation is "in place": the frame size does not change, so a crop rect
means the same thing before and after straightening.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Union

Aspect = Union[str, float, None]

__all__ = [
    "Rect",
    "Scale",
    "parse_aspect",
    "proof_dims",
    "norm_to_px",
    "px_to_norm",
    "proof_to_norm",
    "norm_to_proof",
    "fit_aspect",
    "clamp_into_frame",
    "is_sane",
]


@dataclass(frozen=True)
class Rect:
    """A rectangle in some frame. Interpretation depends on `space`."""

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "w", "h"):
            v = getattr(self, name)
            if not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"Rect.{name} must be a finite number, got {v!r}")
        if self.w <= 0 or self.h <= 0:
            raise ValueError(f"Rect must have positive extent, got {self.w}x{self.h}")

    # -- derived -----------------------------------------------------------
    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    def aspect(self, frame_w: float, frame_h: float) -> float:
        """Width/height ratio in real pixels (frames may not be square)."""
        return (self.w * frame_w) / (self.h * frame_h)

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.w, self.h)

    def to_dict(self) -> dict:
        return {"x": round(self.x, 6), "y": round(self.y, 6),
                "w": round(self.w, 6), "h": round(self.h, 6)}

    def approx(self, other: "Rect", tol: float = 1e-6) -> bool:
        return all(abs(a - b) <= tol for a, b in zip(self.as_tuple(), other.as_tuple()))

    def __str__(self) -> str:  # pragma: no cover - debug aid
        return f"Rect(x={self.x:.4f}, y={self.y:.4f}, w={self.w:.4f}, h={self.h:.4f})"


@dataclass(frozen=True)
class Scale:
    """Mapping between source pixels and proof pixels.

    ``sx`` and ``sy`` are kept separate because a proof is rounded to whole
    pixels; they differ by at most one pixel and are never forced equal.
    """

    sx: float
    sy: float
    frame_w: int
    frame_h: int
    proof_w: int
    proof_h: int

    def to_dict(self) -> dict:
        return {
            "frame_w": self.frame_w, "frame_h": self.frame_h,
            "proof_w": self.proof_w, "proof_h": self.proof_h,
            "sx": round(self.sx, 8), "sy": round(self.sy, 8),
        }


# --------------------------------------------------------------------------
# aspect ratios
# --------------------------------------------------------------------------
def parse_aspect(ar: Aspect) -> Optional[float]:
    """'4:5' -> 0.8, '3x2' -> 1.5, 0.8 -> 0.8, 'free'/None -> None."""
    if ar is None:
        return None
    if isinstance(ar, (int, float)):
        if ar <= 0:
            raise ValueError(f"aspect ratio must be positive, got {ar!r}")
        return float(ar)
    text = str(ar).strip().lower()
    if text in ("", "free", "none", "auto", "open"):
        return None
    for sep in (":", "x", "/"):
        if sep in text:
            a, b = text.split(sep, 1)
            a_f, b_f = float(a), float(b)
            if a_f <= 0 or b_f <= 0:
                raise ValueError(f"aspect ratio must be positive, got {ar!r}")
            return a_f / b_f
    value = float(text)
    if value <= 0:
        raise ValueError(f"aspect ratio must be positive, got {ar!r}")
    return value


# --------------------------------------------------------------------------
# proof scaling
# --------------------------------------------------------------------------
def proof_dims(frame_w: int, frame_h: int, long_edge: int) -> tuple[int, int, Scale]:
    """Proof size that fits inside long_edge, preserving aspect (contain)."""
    if frame_w <= 0 or frame_h <= 0:
        raise ValueError(f"bad frame size {frame_w}x{frame_h}")
    if long_edge <= 0:
        raise ValueError(f"bad long_edge {long_edge}")
    scale = long_edge / max(frame_w, frame_h)
    pw = max(1, int(round(frame_w * scale)))
    ph = max(1, int(round(frame_h * scale)))
    return pw, ph, Scale(pw / frame_w, ph / frame_h, frame_w, frame_h, pw, ph)


def norm_to_px(rect: Rect, frame_w: int, frame_h: int, min_size: int = 1) -> tuple[int, int, int, int]:
    """norm -> integer pixels, clamped so the result is always inside the frame."""
    if frame_w <= 0 or frame_h <= 0:
        raise ValueError(f"bad frame size {frame_w}x{frame_h}")
    left = int(round(rect.x * frame_w))
    top = int(round(rect.y * frame_h))
    width = int(round(rect.w * frame_w))
    height = int(round(rect.h * frame_h))

    left = min(max(left, 0), frame_w - 1)
    top = min(max(top, 0), frame_h - 1)
    width = min(max(width, min(min_size, frame_w - left)), frame_w - left)
    height = min(max(height, min(min_size, frame_h - top)), frame_h - top)
    return left, top, width, height


def px_to_norm(left: int, top: int, width: int, height: int,
               frame_w: int, frame_h: int) -> Rect:
    if frame_w <= 0 or frame_h <= 0:
        raise ValueError(f"bad frame size {frame_w}x{frame_h}")
    return Rect(left / frame_w, top / frame_h, width / frame_w, height / frame_h)


def proof_to_norm(rect_px: Rect, proof_w: int, proof_h: int) -> Rect:
    """Pixel rect measured on the proof -> norm. This is the model-facing path."""
    if proof_w <= 0 or proof_h <= 0:
        raise ValueError(f"bad proof size {proof_w}x{proof_h}")
    return Rect(rect_px.x / proof_w, rect_px.y / proof_h,
                rect_px.w / proof_w, rect_px.h / proof_h)


def norm_to_proof(rect: Rect, proof_w: int, proof_h: int) -> Rect:
    return Rect(rect.x * proof_w, rect.y * proof_h, rect.w * proof_w, rect.h * proof_h)


# --------------------------------------------------------------------------
# crop shaping
# --------------------------------------------------------------------------
def clamp_into_frame(rect: Rect, frame_w: Optional[int] = None,
                     frame_h: Optional[int] = None) -> Rect:
    """Slide a too-large rect back inside [0,1]^2, shrinking only if needed."""
    w = min(rect.w, 1.0)
    h = min(rect.h, 1.0)
    # keep the crop centred while sliding in, so a small overshoot is harmless
    x = rect.x
    if x + w > 1.0:
        x = 1.0 - w
    x = max(0.0, x)
    y = rect.y
    if y + h > 1.0:
        y = 1.0 - h
    y = max(0.0, y)
    return Rect(x, y, w, h)


def fit_aspect(rect: Rect, frame_w: int, frame_h: int, ar: Aspect) -> Rect:
    """Grow/shrink `rect` about its centre so its pixel aspect matches `ar`.

    Growing is the default (never silently throw away the model's framing
    intent); the result is then pulled back inside the frame.
    """
    target = parse_aspect(ar)
    if target is None:
        return clamp_into_frame(rect)

    w_px = rect.w * frame_w
    h_px = rect.h * frame_h
    if w_px / h_px > target:
        w_px = h_px * target
    else:
        h_px = w_px / target

    cx, cy = rect.center
    out = Rect(cx - (w_px / frame_w) / 2.0, cy - (h_px / frame_h) / 2.0,
               w_px / frame_w, h_px / frame_h)
    return clamp_into_frame(out)


def is_sane(rect: Rect, tol: float = 1e-6) -> bool:
    return (rect.w > 0 and rect.h > 0
            and rect.x > -tol and rect.y > -tol
            and rect.right <= 1.0 + tol and rect.bottom <= 1.0 + tol)
