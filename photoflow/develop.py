"""RAW -> viewable image: darktable export, embedded previews, plain rasters.

Verified behaviour on this machine (darktable 5.6.2, exiv2 0.28.9):

* ``darktable-cli IN OUT.jpg --core --configdir DIR``           works headless
* ``darktable-cli IN.XMP_DIR_IF_NEEDED``: sidecar goes as 2nd positional arg
* ``--style NAME`` needs the style *imported into the db*; a .dtstyle path
  is rejected, so styles must be baked into a configdir ahead of time
* two concurrent runs sharing one configdir abort with
  "database is locked" -> use one configdir per worker
* output must be a file path (``--out-ext`` / output dir patterns also work)
* an out-of-date global db makes darktable-cli abort, which is the other
  reason everything here uses an isolated --configdir
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import util
from .util import RAW_EXT

__all__ = [
    "source_dims",
    "list_previews",
    "has_embedded_preview",
    "extract_embedded_preview",
    "darktable_export",
    "darktable_argv",
    "make_master",
    "display_dims",
    "orientation_code",
    "rotation_for_orientation",
    "match_display_orientation",
    "image_size",
    "decode_raster",
    "to_viewable",
    "worker_configdir",
]


# --------------------------------------------------------------------------
# dimensions / metadata
# --------------------------------------------------------------------------
_EXIV_KEYS = (
    "Exif.Photo.PixelXDimension",
    "Exif.Photo.PixelYDimension",
    "Exif.Image.ImageWidth",
    "Exif.Image.ImageLength",
)

# exiv2 prints orientation interpreted, not numerically. 5-8 mean the display
# orientation is the stored one rotated by 90 or 270 degrees.
_ORIENTATION_TO_INT = {
    "top, left": 1, "top, right": 2, "bottom, right": 3, "bottom, left": 4,
    "left, top": 5, "right, top": 6, "right, bottom": 7, "left, bottom": 8,
}
_ORIENTATION_SWAPS_AXES = {5, 6, 7, 8}


def orientation_code(tags: dict[str, str]) -> int:
    raw = tags.get("Exif.Image.Orientation", "").strip().lower()
    if raw in _ORIENTATION_TO_INT:
        return _ORIENTATION_TO_INT[raw]
    m = re.match(r"^(\d+)", raw)
    return int(m.group(1)) if m else 1


def display_dims(tags: dict[str, str], w: int, h: int) -> tuple[int, int]:
    """Swap w/h when the file is stored rotated.

    A real A7 II ARW on disk reports 6000x4000 with Orientation=6, and darktable
    exports 4024x6022. Recording 6000x4000 as the frame would make every aspect
    calculation wrong, so the display orientation is what gets recorded.
    """
    return (h, w) if orientation_code(tags) in _ORIENTATION_SWAPS_AXES else (w, h)


def _exiv_tags(path: Path) -> dict[str, str]:
    out = util.run(["exiv2", "-Pkt", str(path)], check=False)
    tags: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s{2,}(.*)$", line)
        if m:
            tags[m.group(1)] = m.group(2).strip()
    return tags


def source_dims(path: str | Path) -> tuple[int, int]:
    """Full-resolution dimensions, resolved without a full RAW decode."""
    p = Path(path)

    if p.suffix.lower() in RAW_EXT:
        tags = _exiv_tags(p)
        try:
            return display_dims(tags,
                                int(float(tags["Exif.Photo.PixelXDimension"])),
                                int(float(tags["Exif.Photo.PixelYDimension"])))
        except (KeyError, ValueError):
            pass
        try:
            return display_dims(tags,
                                int(float(tags["Exif.Image.ImageWidth"])),
                                int(float(tags["Exif.Image.ImageLength"])))
        except (KeyError, ValueError):
            pass
        # last resort failed: this build's libraw cannot open every file that
        # darktable's rawspeed can (confirmed on a real NEF), so never rely on
        # dcraw_emu as the primary path.
        raise ValueError(
            f"could not determine dimensions of {p}: no PixelXDimension / "
            f"ImageWidth in EXIF and libraw refused the file")

    from PIL import Image, ImageOps
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        return im.width, im.height


@dataclass(frozen=True)
class Preview:
    index: int
    width: int
    height: int

    @property
    def long_edge(self) -> int:
        return max(self.width, self.height)


def list_previews(path: str | Path) -> list[Preview]:
    """The JPEG previews exiv2 can see in the file, as Preview records."""
    listing = util.run(["exiv2", "-pp", str(path)], check=False)
    return [Preview(int(i), int(w), int(h))
            for i, w, h in re.findall(r"Preview\s+(\d+):.*?(\d+)\s*x\s*(\d+)", listing)]


def has_embedded_preview(path: str | Path, min_long_edge: int = 1) -> bool:
    return any(p.long_edge >= min_long_edge for p in list_previews(path))


def extract_embedded_preview(path: str | Path, dest: str | Path,
                             min_long_edge: int = 0) -> Optional[Path]:
    """Pull the camera's own JPEG preview out of the RAW.

    Returns None when the file has no preview big enough to be useful. That is
    a deliberate refusal: a real NEF on this machine exposes only a 160x112
    EXIF thumbnail, and upscaling that to an 800 px "proof" would hand the
    reviewing model a blurry fake and quietly ruin every downstream decision.

    exiv2 always writes next to the source file, so the RAW is symlinked into a
    temp dir first - the shoot directory is never touched.
    """
    p = Path(path)
    previews = list_previews(p)
    if not previews:
        return None

    usable = [p_ for p_ in previews if p_.long_edge >= min_long_edge]
    if min_long_edge > 0 and not usable:
        return None
    best = max(usable or previews, key=lambda p_: p_.width * p_.height)

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pf_preview_") as tmp:
        link = Path(tmp) / p.name
        os.symlink(p.resolve(), link)
        util.run(["exiv2", f"-ep{best.index}", str(link)], check=False)
        produced = sorted(Path(tmp).glob(f"{p.name}-preview{best.index}.*"))
        if not produced:
            produced = sorted(Path(tmp).glob("*preview*"))
        if not produced:
            return None
        shutil.move(str(produced[0]), dest)
    return dest


# --------------------------------------------------------------------------
# darktable
# --------------------------------------------------------------------------
def worker_configdir(work_root: str | Path, worker: int = 0,
                     template: Optional[str | Path] = None) -> Path:
    """One isolated darktable configdir per worker (avoids db lock contention).

    If ``template`` exists and the worker dir has no database yet, the template
    is copied in. That is how an imported style reaches every worker: the
    template is the single source of truth for config, and copying keeps all
    workers byte-identical.
    """
    d = Path(work_root) / "dtcfg" / f"w{worker:02d}"
    if template is not None and not (d / "data.db").exists():
        tpl = Path(template)
        if (tpl / "data.db").exists():
            if d.exists():
                shutil.rmtree(d)
            shutil.copytree(tpl, d)
            return d
    d.mkdir(parents=True, exist_ok=True)
    return d


def darktable_argv(src: str | Path, dest: str | Path, *,
                   xmp: Optional[str | Path] = None,
                   configdir: str | Path,
                   long_edge: Optional[int] = None,
                   quality: int = 95,
                   apply_presets: bool = True,
                   sixteen_bit: bool = False,
                   style: Optional[str] = None) -> list[str]:
    """Build the darktable-cli argv. Pure, so it is testable.

    darktable-cli's grammar is two regions:

        darktable-cli [CLI OPTIONS and POSITIONALS] --core [CORE OPTIONS]

    CLI options (--width, --height, --apply-custom-presets, --style, --out-ext)
    are only accepted *before* --core; --configdir and --conf are core options
    and only accepted *after* it. Getting this wrong makes darktable print its
    usage and exit 1 with no error message. Verified on a real NEF:

        --width 800 IN OUT --core --configdir D   -> 799x534   ok
        IN OUT --core --configdir D --width 800   -> usage     (cli opt too late)
        --core --configdir D --width 800 IN OUT   -> usage     (cli opt too late)
    """
    argv: list[str] = ["darktable-cli"]

    # --- CLI region -------------------------------------------------------
    if long_edge:
        argv += ["--width", str(long_edge), "--height", str(long_edge)]
    if style:
        # --style takes a *name from the configdir database*, never a path
        argv += ["--style", style]
    # --apply-custom-presets 0 makes darktable skip initialising data.db
    # entirely (verified: a fresh configdir ends up with only darktablerc, no
    # database). With no database there are no styles and --style silently
    # cannot work, so this must stay on. Concurrency is handled by giving each
    # worker its own configdir instead.
    argv += ["--apply-custom-presets", "1" if apply_presets else "0"]

    argv.append(str(src))
    if xmp:
        argv.append(str(xmp))          # sidecar goes 2nd, before the output
    argv.append(str(dest))

    # --- core region ------------------------------------------------------
    argv += [
        "--core",
        "--configdir", str(configdir),
        "--conf", f"plugins/imageio/format/jpeg/quality={int(quality)}",
    ]
    if sixteen_bit and Path(dest).suffix.lower() in (".tif", ".tiff"):
        # verified: produces 16-bit sRGB TIFF
        argv += ["--conf", "plugins/imageio/format/tiff/bpp=16"]
    return argv


def make_master(src: str | Path, dest: str | Path, *,
                configdir: Optional[str | Path] = None,
                work_root: Optional[str | Path] = None,
                worker: int = 0,
                quality: int = 95,
                sixteen_bit: Optional[bool] = None,
                style: Optional[str] = None) -> Path:
    """Develop a full-resolution master that the local renderer can read.

    Why this exists: ImageMagick cannot read every RAW (confirmed on a real NEF,
    which this build's libraw refuses), and cropping a RAW directly would
    bypass darktable and give the master a different look from the proof.
    Everything downstream crops *this* file, so proof and master always agree.

    .tif -> 16-bit (use for final delivery), .jpg -> 8-bit (fine for review).
    """
    util.require("darktable-cli")
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # darktable-cli does NOT overwrite: given an existing output it writes
    # "name-1.jpg" instead and still exits 0. That silently leaves a stale
    # master in place while the pipeline believes it re-rendered, so the target
    # is removed first and only the requested path can satisfy the check below.
    dest.unlink(missing_ok=True)

    if src.suffix.lower() not in RAW_EXT:
        return decode_raster(src, dest, None)

    if configdir is None:
        if work_root is None:
            raise ValueError("make_master needs configdir= or work_root=")
        configdir = worker_configdir(work_root, worker,
                                     template=Path(work_root) / "dtcfg" / "template")
    if sixteen_bit is None:
        sixteen_bit = dest.suffix.lower() in (".tif", ".tiff")

    argv = darktable_argv(src, dest, configdir=configdir, quality=quality,
                          sixteen_bit=sixteen_bit, style=style)
    util.run(argv, timeout=900)
    if not dest.exists() or dest.stat().st_size == 0:
        raise util.ToolError(argv, 1, f"darktable produced no master at {dest}")

    # record what produced this master so a style switch cannot silently reuse it
    if work_root is not None:
        util.write_json(
            Path(work_root) / "masters" / f"{dest.stem}.{dest.suffix.lstrip('.')}.meta.json",
            {"source": str(src), "style": style, "sixteen_bit": bool(sixteen_bit),
             "bytes": dest.stat().st_size})
    return dest


def darktable_export(src: str | Path, dest: str | Path, *,
                     xmp: Optional[str | Path] = None,
                     configdir: Optional[str | Path] = None,
                     work_root: Optional[str | Path] = None,
                     worker: int = 0,
                     long_edge: Optional[int] = None,
                     quality: int = 95,
                     apply_presets: bool = True,
                     style: Optional[str] = None) -> Path:
    """Develop one file through darktable-cli.

    ``long_edge`` caps the export size; that is what keeps the reviewing proofs
    cheap (a 800 px proof instead of a 45 MP master).
    """
    util.require("darktable-cli")
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if configdir is None:
        if work_root is None:
            raise ValueError("darktable_export needs configdir= or work_root=")
        configdir = worker_configdir(work_root, worker)

    dest.unlink(missing_ok=True)   # darktable would rename instead of overwrite
    argv = darktable_argv(src, dest, xmp=xmp, configdir=configdir,
                          long_edge=long_edge, quality=quality,
                          apply_presets=apply_presets, style=style)

    util.run(argv, timeout=600)
    if not dest.exists() or dest.stat().st_size == 0:
        raise util.ToolError(argv, 1, f"darktable produced no output at {dest}")
    return dest


def decode_raster(src: str | Path, dest: str | Path, long_edge: Optional[int] = None,
                  *, rotate: Optional[float] = None, quality: int = 95) -> Path:
    """Copy/resize an already-viewable image (jpeg, tif, png, ...).

    Orientation is applied with ImageMagick's -auto-orient, which is a no-op on
    files whose tag is already 1 (e.g. darktable's exports, where the pixels are
    rotated and the tag normalised). Pass ``rotate`` to force a rotation.
    """
    src, dest = Path(src), Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if src.resolve() == dest.resolve() and long_edge is not None:
        # in-place resize: go through a temp file, never truncate the source
        tmp = dest.with_name(f".{dest.stem}.pf_tmp{dest.suffix}")
        try:
            decode_raster(src, tmp, long_edge, rotate=rotate, quality=quality)
            shutil.move(str(tmp), dest)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return dest

    if long_edge is None and rotate is None and src.suffix.lower() == dest.suffix.lower():
        shutil.copyfile(src, dest)
        return dest

    if shutil.which("magick"):
        argv = ["magick", str(src)]
        if rotate is not None:
            argv += ["-rotate", f"{rotate:g}"]
        else:
            argv += ["-auto-orient"]
        if long_edge:
            argv += ["-thumbnail", f"{long_edge}x{long_edge}>"]
        argv += ["-strip", "-quality", str(int(quality)), str(dest)]
        util.run(argv, timeout=300)
        return dest

    from PIL import Image, ImageOps
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        if rotate is not None:
            im = im.rotate(-rotate, expand=True)
        if long_edge:
            im.thumbnail((long_edge, long_edge), Image.LANCZOS)
        im.save(dest, quality=quality)
    return dest


def rotation_for_orientation(code: int) -> float:
    """Clockwise degrees needed to display a file stored with `code`."""
    return {5: 90.0, 6: 90.0, 7: 270.0, 8: 270.0}.get(int(code), 0.0)


def match_display_orientation(preview: str | Path, source: str | Path) -> bool:
    """Rotate a preview so its shape agrees with the source's display shape.

    The JPEG preview inside a real A7 II ARW is stored landscape (1616x1080)
    with *no* orientation tag - the rotation lives only on the parent RAW. So a
    preview extracted from a portrait frame stays landscape unless it is fixed
    here, which would hand the reviewer a sideways image and put every crop
    coordinate in the wrong frame.
    """
    preview = Path(preview)
    prev_w, prev_h = image_size(preview)

    # the parent's EXIF knows the truth; without it, fall back to matching the
    # shape of the declared display size
    code = orientation_code(_exiv_tags(Path(source))) if Path(source).suffix.lower() in RAW_EXT else 1
    deg = rotation_for_orientation(code)
    if deg == 0.0:
        disp_w, disp_h = source_dims(source)
        if (prev_w > prev_h) == (disp_w > disp_h):
            return False
        deg = 90.0

    if int(deg) % 360 == 0:
        return False

    tmp = preview.with_name(f".{preview.stem}.rot{preview.suffix}")
    try:
        decode_raster(preview, tmp, None, rotate=deg, quality=95)
        shutil.move(str(tmp), preview)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return True


def image_size(path: str | Path) -> tuple[int, int]:
    from PIL import Image, ImageOps

    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        return im.width, im.height


def to_viewable(src: str | Path, dest: str | Path, *,
                long_edge: Optional[int] = None,
                prefer_embedded: bool = False,
                configdir: Optional[str | Path] = None,
                work_root: Optional[str | Path] = None,
                worker: int = 0,
                quality: int = 95,
                style: Optional[str] = None) -> Path:
    """Uniform entry point: any supported file -> a viewable JPEG/TIFF."""
    src, dest = Path(src), Path(dest)
    if src.suffix.lower() not in RAW_EXT:
        return decode_raster(src, dest, long_edge)

    if prefer_embedded:
        got = extract_embedded_preview(src, dest, min_long_edge=long_edge or 0)
        if got:
            match_display_orientation(got, src)
            if long_edge and max(image_size(got)) > long_edge:
                return decode_raster(got, got, long_edge)
            return got
        # no usable preview -> develop it; never substitute a thumbnail

    return darktable_export(src, dest, configdir=configdir, work_root=work_root,
                            worker=worker, long_edge=long_edge, quality=quality,
                            style=style)
