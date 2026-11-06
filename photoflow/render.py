"""Execute a recipe at any resolution.

This is the agent's actuator. It is deliberately boring and fully inspectable:
one ImageMagick argv per output, printable with ``describe_command`` before
anything runs.

Op order is fixed and meaningful:

    rotate -> crop -> resize -> exposure -> contrast -> saturation -> vignette -> sharpen

Rotate comes before crop because a straightened horizon must be straightened
before you can decide where the frame edges are. Vignette uses the final output
dimensions, so proof and master get the same radial falloff. Sharpen comes last
because sharpening a downscaled proof is the point - proof and master get the
same treatment at their own native size.

Every adjustment here is *measurable*, so the loop can be closed:
render -> photoflow.measure -> compare -> re-render. See README "the loop".
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from . import geometry, util
from .recipe import DEVELOP_KEYS, Recipe

__all__ = ["render", "render_chain", "image_size", "describe_command", "UnsupportedOp"]

CONTRAST_GAIN_RANGE = (0.15, 12.0)

# Output sharpening must sharpen edges, not sensor grain.  ImageMagick skips
# local differences below this fraction of the quantum range (0.05 ~ 13/255).
# Measured on real ISO-1250 masters: without it the unsharp mask roughly doubles
# the high-frequency noise in flat shadow areas, which is what reads as "grainy".
SHARPEN_THRESHOLD = 0.05


class UnsupportedOp(RuntimeError):
    """A develop-stage op was asked of the local renderer."""


def image_size(path: str | Path) -> tuple[int, int]:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.width, im.height


def _contrast_gain(contrast: float) -> float:
    """-1..1 -> sigmoidal gain centred on mid-grey.

    Sigmoidal (S-curve) rather than a linear level stretch: it cannot clip, and
    it falls off at both ends the way a tone curve should. Verified in this
    build: gain < 1 flattens, gain > 1 steepens, both monotonic.
    """
    lo, hi = CONTRAST_GAIN_RANGE
    gain = 3.0 ** (2.0 * contrast)
    return min(max(gain, lo), hi)


def render_chain(src: str | Path, recipe: Recipe, out: str | Path, *,
                 long_edge: Optional[int] = None,
                 quality: int = 95,
                 strip: bool = True,
                 ignore_develop: bool = False) -> list[str]:
    """Build the ImageMagick argv without running it."""
    src, out = Path(src), Path(out)

    requested = sorted(k for k, v in recipe.develop.items() if v not in (None, {}, []))
    if requested and not ignore_develop:
        raise UnsupportedOp(
            "recipe asks for develop-stage op(s) the local renderer does not "
            f"implement: {', '.join(requested)}. Bake these into the darktable "
            "XMP/configdir (see README) or re-run with ignore_develop=True.\n"
            f"Supported locally: exposure_ev, contrast, saturation, sharpening, vignette."
        )

    frame_w, frame_h = image_size(src)
    argv: list[str] = ["magick", str(src)]

    # 1. straighten in place; edge pixels fill the corners (no canvas growth
    #    means the model's crop coordinates stay valid in the frame it saw)
    if abs(recipe.geometry.rotate_deg) > 1e-4:
        argv += ["-virtual-pixel", "edge",
                 "-distort", "SRT", f"{recipe.geometry.rotate_deg:.4f}"]

    # 2. crop, in norm space -> pixels of *this* source, so the same recipe
    #    works on the master and on a small proof
    left, top, width, height = recipe.crop_px(frame_w, frame_h)
    is_full = (left, top, width, height) == (0, 0, frame_w, frame_h)
    if not is_full:
        argv += ["-crop", f"{width}x{height}+{left}+{top}", "+repage"]

    # 3. size cap (this is what makes proofs cheap)
    out_w, out_h = width, height
    if long_edge and max(width, height) > long_edge:
        argv += ["-resize", f"{long_edge}x{long_edge}>"]
        scale = long_edge / max(width, height)
        out_w = max(1, round(width * scale))
        out_h = max(1, round(height * scale))

    a = recipe.adjust

    # 4. exposure in linear light, not on gamma-encoded values
    if abs(a.exposure_ev) > 1e-3:
        k = 2.0 ** a.exposure_ev
        argv += ["-colorspace", "RGB", "-evaluate", "multiply", f"{k:.8f}",
                 "-colorspace", "sRGB"]

    # 5. contrast
    if abs(a.contrast) > 1e-3:
        argv += ["-sigmoidal-contrast", f"{_contrast_gain(a.contrast):.4f}x50%"]

    # 6. saturation
    if abs(a.saturation) > 1e-3:
        pct = min(max((1.0 + a.saturation) * 100.0, 0.0), 300.0)
        argv += ["-modulate", f"100,{pct:.2f},100"]

    # 7. radial vignette: multiply by a white-to-grey gradient.  The gradient's
    #    centre stays pure white; its corners are (1 - strength).  Building it
    #    at the final output size keeps the proof and master geometrically identical.
    if a.vignette > 1e-4:
        strength = min(max(a.vignette, 0.0), 0.5)
        argv += ["(", "-size", f"{out_w}x{out_h}", "radial-gradient:white-black",
                 "-evaluate", "multiply", f"{strength:.4f}",
                 "-evaluate", "add", f"{(1.0 - strength) * 100.0:.4f}%",
                 ")", "-compose", "multiply", "-composite"]

    # 8. output sharpening (after resize, by design).  The threshold keeps the
    #    mask off low-contrast noise; see SHARPEN_THRESHOLD.
    if a.sharpening > 1e-3:
        argv += ["-unsharp", f"0x0.9+{a.sharpening:.3f}+{SHARPEN_THRESHOLD}"]

    if strip:
        argv += ["-strip"]
    argv += ["-depth", "8", "-quality", str(int(quality))]
    argv.append(str(out))
    return argv


def describe_command(argv: Sequence[str]) -> str:
    return " \\\n  ".join(" ".join(argv))


def render(src: str | Path, recipe: Recipe, out: str | Path, *,
           long_edge: Optional[int] = None,
           quality: int = 95,
           strip: bool = True,
           ignore_develop: bool = False,
           dry_run: bool = False) -> Path:
    """Render one recipe. Returns the output path."""
    out = Path(out)
    argv = render_chain(src, recipe, out, long_edge=long_edge,
                        quality=quality, strip=strip, ignore_develop=ignore_develop)
    if dry_run:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    util.run(argv, timeout=900)
    if not out.exists() or out.stat().st_size == 0:
        raise util.ToolError(argv, 1, f"magick produced no output at {out}")
    return out


def check_supported(recipe: Recipe) -> list[str]:
    """Non-fatal list of ops that will be silently ignored locally."""
    return sorted(k for k in recipe.develop if k in DEVELOP_KEYS)
