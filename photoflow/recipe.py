"""The recipe: the single source of truth for how one photo is rendered.

A recipe is plain JSON, human-diffable, and resolution-independent. It is what
the reviewing model is asked to produce, what gets validated before anything is
executed, and what is replayed later to regenerate the master.

Two halves
----------
``geometry`` + ``adjust``   applied locally by :mod:`photoflow.render`
``develop``                 darktable-side (white balance, denoise, tone).
                            Recorded here so the recipe stays complete, but the
                            local renderer refuses to fake them.

Keeping the local half small is deliberate: every op in ``adjust`` is
measurable, so the agent can close the loop (measure -> change -> re-measure)
instead of guessing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import geometry

SCHEMA = "photoflow.recipe/1"

# Ranges are hard limits, not style advice: values outside are rejected so a
# hallucinated "exposure_ev: 40" can never reach the renderer.
ADJUST_RANGES: dict[str, tuple[float, float]] = {
    "exposure_ev": (-3.0, 3.0),
    "contrast": (-1.0, 1.0),
    "saturation": (-1.0, 1.0),
    "sharpening": (0.0, 2.0),
    # fraction of corner luminance removed by the radial vignette; 0.10 is
    # deliberately subtle, 0.25 is a visible edge burn.
    "vignette": (0.0, 0.5),
}

# Accepted, recorded, but NOT applied by the local renderer.
DEVELOP_KEYS = {
    "white_balance", "denoise", "highlights", "shadows",
    "tone_curve", "clarity", "lens_correction", "base_style",
}

ROTATE_RANGE = (-45.0, 45.0)
WARN_ROTATE = 5.0
MIN_CROP_FRACTION = 0.02  # never let the model emit a 3-pixel sliver


class RecipeError(ValueError):
    """Raised when a recipe (usually model output) fails validation."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class Adjust:
    exposure_ev: float = 0.0
    contrast: float = 0.0
    saturation: float = 0.0
    sharpening: float = 0.0
    vignette: float = 0.0

    def to_dict(self) -> dict:
        return {
            "exposure_ev": round(self.exposure_ev, 4),
            "contrast": round(self.contrast, 4),
            "saturation": round(self.saturation, 4),
            "sharpening": round(self.sharpening, 4),
            "vignette": round(self.vignette, 4),
        }

    def is_identity(self) -> bool:
        return all(abs(v) < 1e-6 for v in
                   (self.exposure_ev, self.contrast, self.saturation,
                    self.sharpening, self.vignette))


@dataclass
class Geometry:
    rotate_deg: float = 0.0
    crop: geometry.Rect = field(default_factory=lambda: geometry.Rect(0.0, 0.0, 1.0, 1.0))
    aspect: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "rotate_deg": round(self.rotate_deg, 4),
            "aspect": self.aspect,
            "crop": self.crop.to_dict(),
        }

    def is_full_frame(self) -> bool:
        return self.crop.approx(geometry.Rect(0.0, 0.0, 1.0, 1.0)) and abs(self.rotate_deg) < 1e-6


@dataclass
class Recipe:
    image: str
    frame_w: int
    frame_h: int
    geometry: Geometry = field(default_factory=Geometry)
    adjust: Adjust = field(default_factory=Adjust)
    develop: dict[str, Any] = field(default_factory=dict)
    review: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # -- construction ------------------------------------------------------
    @classmethod
    def new(cls, image: str | Path, frame_w: int, frame_h: int) -> "Recipe":
        return cls(image=Path(str(image)).name, frame_w=int(frame_w), frame_h=int(frame_h))

    @classmethod
    def from_dict(cls, data: dict) -> "Recipe":
        if not isinstance(data, dict):
            raise RecipeError([f"recipe must be an object, got {type(data).__name__}"])
        schema = data.get("schema", SCHEMA)
        if schema != SCHEMA:
            raise RecipeError([f"unsupported schema {schema!r}, expected {SCHEMA!r}"])

        src = data.get("source") or {}
        frame_w = int(src.get("w") or data.get("frame_w") or 0)
        frame_h = int(src.get("h") or data.get("frame_h") or 0)
        if frame_w <= 0 or frame_h <= 0:
            raise RecipeError(["source.w / source.h must be positive integers"])

        g = data.get("geometry") or {}
        crop = g.get("crop") or {}
        rect = geometry.Rect(
            float(crop.get("x", 0.0)), float(crop.get("y", 0.0)),
            float(crop.get("w", 1.0)), float(crop.get("h", 1.0)),
        )
        geo = Geometry(rotate_deg=float(g.get("rotate_deg", 0.0)),
                       crop=rect, aspect=g.get("aspect"))

        a = data.get("adjust") or {}
        adjust = Adjust(
            exposure_ev=float(a.get("exposure_ev", 0.0)),
            contrast=float(a.get("contrast", 0.0)),
            saturation=float(a.get("saturation", 0.0)),
            sharpening=float(a.get("sharpening", 0.0)),
            vignette=float(a.get("vignette", 0.0)),
        )

        unknown = set(a) - set(ADJUST_RANGES) - DEVELOP_KEYS
        if unknown:
            raise RecipeError([f"unknown adjust key(s): {', '.join(sorted(unknown))}"])

        develop = {k: v for k, v in a.items() if k in DEVELOP_KEYS}
        develop.update(data.get("develop") or {})

        rec = cls(
            image=str(data.get("image") or ""),
            frame_w=frame_w, frame_h=frame_h,
            geometry=geo, adjust=adjust, develop=develop,
            review=data.get("review") or {},
        )
        rec.validate()
        return rec

    def to_dict(self) -> dict:
        out = {
            "schema": SCHEMA,
            "image": self.image,
            "source": {"w": self.frame_w, "h": self.frame_h},
            "geometry": self.geometry.to_dict(),
            "adjust": self.adjust.to_dict(),
            "develop": self.develop,
            "review": self.review,
        }
        if self.warnings:
            out["warnings"] = list(self.warnings)
        return out

    # -- io ----------------------------------------------------------------
    @classmethod
    def load(cls, path: str | Path) -> "Recipe":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def dump(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return p

    # -- validation --------------------------------------------------------
    def validate(self) -> "Recipe":
        problems: list[str] = []
        self.warnings = []

        lo, hi = ROTATE_RANGE
        if not (lo <= self.geometry.rotate_deg <= hi):
            problems.append(f"geometry.rotate_deg={self.geometry.rotate_deg} outside [{lo}, {hi}]")
        elif abs(self.geometry.rotate_deg) > WARN_ROTATE:
            self.warnings.append(
                f"rotate_deg={self.geometry.rotate_deg:.2f} is large; "
                "corners will be filled from the edge pixels")

        crop = self.geometry.crop
        if crop.w < MIN_CROP_FRACTION or crop.h < MIN_CROP_FRACTION:
            problems.append(
                f"crop too small ({crop.w:.4f}x{crop.h:.4f}); minimum is {MIN_CROP_FRACTION}")
        if not geometry.is_sane(crop, tol=1e-4):
            problems.append(f"crop {crop} escapes the frame {self.frame_w}x{self.frame_h}")

        if self.geometry.aspect:
            want = geometry.parse_aspect(self.geometry.aspect)
            got = crop.aspect(self.frame_w, self.frame_h)
            if want and abs(got - want) / want > 0.02:
                self.warnings.append(
                    f"crop aspect {got:.3f} does not match declared {self.geometry.aspect} "
                    f"({want:.3f}); renderer will use the declared aspect")
                self.geometry.crop = geometry.fit_aspect(
                    crop, self.frame_w, self.frame_h, want)

        for key, (lo, hi) in ADJUST_RANGES.items():
            value = getattr(self.adjust, key)
            if not (lo <= value <= hi):
                problems.append(f"adjust.{key}={value} outside [{lo}, {hi}]")

        if problems:
            raise RecipeError(problems)
        return self

    # -- derived -----------------------------------------------------------
    def crop_px(self, frame_w: Optional[int] = None,
                frame_h: Optional[int] = None) -> tuple[int, int, int, int]:
        return geometry.norm_to_px(self.geometry.crop,
                                   frame_w or self.frame_w,
                                   frame_h or self.frame_h)

    def crop_signature(self) -> tuple:
        """Cheap identity so a stale render can be detected."""
        c = self.geometry.crop
        return (round(self.geometry.rotate_deg, 3),
                round(c.x, 5), round(c.y, 5), round(c.w, 5), round(c.h, 5),
                round(self.adjust.exposure_ev, 3), round(self.adjust.contrast, 3),
                round(self.adjust.saturation, 3), round(self.adjust.sharpening, 3),
                round(self.adjust.vignette, 3))
