"""Copy the capture metadata from the original RAW onto the rendered JPEG.

The renderer strips metadata on purpose (``-strip``) so a render is a pure
function of the recipe.  That is right for proofs, but an uploaded photograph
without ``DateTimeOriginal`` sorts by upload date and shows no camera/lens in
any album.  This module stamps a curated subset back on:

* capture date/time (and timezone offsets when the camera wrote them)
* camera make/model, lens, exposure, ISO, focal length, flash, white balance
* GPS position if the original has one

Only *value* tags are copied.  Structural tags are deliberately excluded:
offset pointers such as ``SubIFDs``, ``JPEGInterchangeFormat`` and
``InteroperabilityTag`` reference the source file and would be corrupt in the
destination; ``Undefined`` blobs (maker notes, ExifVersion, thumbnails) are
skipped for the same reason.  Orientation and pixel dimensions are always
overridden because the render is cropped/rotated and is not the source size.

Reading uses ``exiv2 -pa`` (key/type/value) plus ``exiv2 -Pkv`` (raw values):
the interpreted values that ``-pa`` prints ("F3.5", "1/8000 s") are not valid
input for ``-M set``; the raw form ("35/10", "1/8000") is.
"""

from __future__ import annotations

import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Iterable, Optional

from . import util

__all__ = ["ORIGIN_TAGS", "read_source_tags", "stamp", "verify", "stamp_paths",
           "decimal_to_dms", "set_gps", "get_gps", "set_gps_setting"]

# Value tags worth carrying.  Anything not listed here is left out on purpose.
ORIGIN_TAGS: frozenset[str] = frozenset({
    # camera / file
    "Exif.Image.Make", "Exif.Image.Model", "Exif.Image.Software",
    "Exif.Image.DateTime", "Exif.Image.Artist", "Exif.Image.Copyright",
    "Exif.Image.ImageDescription",
    # capture
    "Exif.Photo.ExposureTime", "Exif.Photo.FNumber", "Exif.Photo.ExposureProgram",
    "Exif.Photo.ISOSpeedRatings", "Exif.Photo.SensitivityType",
    "Exif.Photo.RecommendedExposureIndex",
    "Exif.Photo.DateTimeOriginal", "Exif.Photo.DateTimeDigitized",
    "Exif.Photo.OffsetTime", "Exif.Photo.OffsetTimeOriginal",
    "Exif.Photo.OffsetTimeDigitized",
    "Exif.Photo.ShutterSpeedValue", "Exif.Photo.ApertureValue",
    "Exif.Photo.BrightnessValue", "Exif.Photo.ExposureBiasValue",
    "Exif.Photo.MaxApertureValue", "Exif.Photo.SubjectDistance",
    "Exif.Photo.MeteringMode", "Exif.Photo.LightSource", "Exif.Photo.Flash",
    "Exif.Photo.FocalLength", "Exif.Photo.SubjectArea",
    "Exif.Photo.CustomRendered", "Exif.Photo.ExposureMode",
    "Exif.Photo.WhiteBalance", "Exif.Photo.DigitalZoomRatio",
    "Exif.Photo.FocalLengthIn35mmFilm", "Exif.Photo.SceneCaptureType",
    "Exif.Photo.GainControl", "Exif.Photo.Contrast",
    "Exif.Photo.Saturation", "Exif.Photo.Sharpness",
    "Exif.Photo.SubjectDistanceRange", "Exif.Photo.ColorSpace",
    # lens
    "Exif.Photo.LensSpecification", "Exif.Photo.LensMake",
    "Exif.Photo.LensModel", "Exif.Photo.LensSerialNumber",
    "Exif.Photo.BodySerialNumber",
    # location
    "Exif.GPSInfo.GPSLatitudeRef", "Exif.GPSInfo.GPSLatitude",
    "Exif.GPSInfo.GPSLongitudeRef", "Exif.GPSInfo.GPSLongitude",
    "Exif.GPSInfo.GPSAltitudeRef", "Exif.GPSInfo.GPSAltitude",
    "Exif.GPSInfo.GPSTimeStamp", "Exif.GPSInfo.GPSDateStamp",
    "Exif.GPSInfo.GPSImgDirectionRef", "Exif.GPSInfo.GPSImgDirection",
})

_ROW = re.compile(r"^(\S+)\s+(\S+)\s+(\d+)\s*(.*)$")
_PAIR = re.compile(r"^(\S+)\s+(.*)$")


def _tag_types(src: Path) -> dict[str, str]:
    out = util.run(["exiv2", "-pa", str(src)], check=False)
    types: dict[str, str] = {}
    for line in out.splitlines():
        m = _ROW.match(line)
        if m:
            types[m.group(1)] = m.group(2)
    return types


def _raw_values(src: Path) -> dict[str, str]:
    out = util.run(["exiv2", "-Pkv", str(src)], check=False)
    values: dict[str, str] = {}
    for line in out.splitlines():
        m = _PAIR.match(line)
        if m:
            values[m.group(1)] = m.group(2).strip()
    return values


def read_source_tags(src: str | Path) -> dict[str, tuple[str, str]]:
    """Return ``{key: (exiv2 type, raw value)}`` for the tags we carry."""
    src = Path(src)
    types = _tag_types(src)
    values = _raw_values(src)
    tags: dict[str, tuple[str, str]] = {}
    for key, typ in types.items():
        if key not in ORIGIN_TAGS:
            continue
        if typ in ("Undefined", "Comment"):
            continue
        value = values.get(key, "")
        if not value:
            continue
        tags[key] = (typ, value)
    return tags


def _commands(src: Path, width: int, height: int) -> list[str]:
    tags = read_source_tags(src)
    args: list[str] = []
    for key in sorted(tags):
        typ, value = tags[key]
        args += ["-M", f"set {key} {typ} {value}"]
    # geometry always describes the *rendered* file, not the original
    args += [
        "-M", "set Exif.Image.Orientation Short 1",
        "-M", f"set Exif.Image.ImageWidth Long {width}",
        "-M", f"set Exif.Image.ImageLength Long {height}",
        "-M", f"set Exif.Photo.PixelXDimension Long {width}",
        "-M", f"set Exif.Photo.PixelYDimension Long {height}",
    ]
    return args


def stamp(dst: str | Path, src: str | Path, *, width: Optional[int] = None,
          height: Optional[int] = None, dry_run: bool = False) -> list[str]:
    """Copy capture metadata from ``src`` (RAW or JPEG) onto the JPEG ``dst``."""
    from . import render  # local import: avoid a cycle at module import time

    dst, src = Path(dst), Path(src)
    if width is None or height is None:
        width, height = render.image_size(dst)
    args = _commands(src, width, height)
    if not dry_run:
        util.run(["exiv2", *args, str(dst)])
    return args


def decimal_to_dms(value: float) -> list[tuple[int, int]]:
    """decimal degrees -> [(deg,1), (min,1), (sec_num,sec_den)] as exact-ish fractions."""
    total = round(abs(value) * 3600.0, 6)
    deg = int(total // 3600)
    total -= deg * 3600
    minute = int(total // 60)
    sec = total - minute * 60
    if sec >= 60:
        sec -= 60.0
        minute += 1
    if minute >= 60:
        minute -= 60
        deg += 1
    frac = Fraction(sec).limit_denominator(1_000_000)
    return [(deg, 1), (minute, 1), (frac.numerator, frac.denominator)]


def _rationals(dms: list[tuple[int, int]]) -> str:
    return " ".join(f"{n}/{d}" for n, d in dms)


def set_gps(path: str | Path, latitude: float, longitude: float, *,
            dry_run: bool = False) -> list[str]:
    """Write GPS lat/lon tags without touching any other metadata."""
    args = [
        "-M", "set Exif.GPSInfo.GPSVersionID Byte 2 3 0 0",
        "-M", f"set Exif.GPSInfo.GPSLatitudeRef Ascii {'N' if latitude >= 0 else 'S'}",
        "-M", f"set Exif.GPSInfo.GPSLatitude Rational {_rationals(decimal_to_dms(latitude))}",
        "-M", f"set Exif.GPSInfo.GPSLongitudeRef Ascii {'E' if longitude >= 0 else 'W'}",
        "-M", f"set Exif.GPSInfo.GPSLongitude Rational {_rationals(decimal_to_dms(longitude))}",
    ]
    if not dry_run:
        util.run(["exiv2", *args, str(path)])
    return args


def _settings_path(work_root: str | Path) -> Path:
    return Path(work_root) / "settings.json"


def get_gps(work_root: str | Path) -> Optional[tuple[float, float]]:
    p = _settings_path(work_root)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8")).get("gps")
        if not raw:
            return None
        lat, lon = (float(x) for x in str(raw).split(","))
        return lat, lon
    except (json.JSONDecodeError, ValueError):
        return None


def set_gps_setting(work_root: str | Path, latitude: float, longitude: float) -> None:
    p = _settings_path(work_root)
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    def fmt(v: float) -> str:
        return f"{v:.7f}".rstrip("0").rstrip(".")
    data["gps"] = f"{fmt(latitude)},{fmt(longitude)}"
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def verify(path: str | Path) -> dict[str, str]:
    """Read back the tags that matter, as interpreted by exiv2."""
    out = util.run(["exiv2", "-pa", str(path)], check=False)
    wanted = ("Exif.Photo.DateTimeOriginal", "Exif.Photo.PixelXDimension",
              "Exif.Photo.PixelYDimension", "Exif.Image.Orientation",
              "Exif.Image.Make", "Exif.Image.Model", "Exif.Photo.LensModel",
              "Exif.Photo.ISOSpeedRatings",
              "Exif.GPSInfo.GPSLatitude", "Exif.GPSInfo.GPSLongitude")
    found: dict[str, str] = {}
    for line in out.splitlines():
        m = _ROW.match(line)
        if m and m.group(1) in wanted:
            found[m.group(1)] = m.group(4).strip()
    return found


def stamp_paths(paths: Iterable[Path], sources: dict[str, Path], *,
                dry_run: bool = False) -> tuple[int, int]:
    """Stamp every ``*.jpg`` whose stem has a source. Returns (done, skipped)."""
    done = skipped = 0
    for path in paths:
        src = sources.get(path.stem)
        if src is None or not src.exists():
            skipped += 1
            continue
        stamp(path, src, dry_run=dry_run)
        done += 1
    return done, skipped
