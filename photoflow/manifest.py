"""Shoot inventory: what is actually on the card, before anything expensive.

Everything is read from EXIF only - no pixels are decoded - so this is fast on
a 100+ RAW card and gives the culling stage the context it needs (bursts,
lens changes, ISO ramps as the light fell).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from . import develop, util
from .util import IMAGE_EXT

__all__ = ["inventory", "load", "group_bursts", "flatten", "raw_hint"]

_FIELDS = {
    "Exif.Image.Model": "camera",
    "Exif.Image.Make": "make",
    "Exif.Photo.LensModel": "lens",
    "Exif.Photo.LensSpecification": "lens_spec",
    "Exif.Photo.FNumber": "aperture",
    "Exif.Photo.ExposureTime": "shutter",
    "Exif.Photo.ISOSpeedRatings": "iso",
    "Exif.Photo.PhotographicSensitivity": "iso",
    "Exif.Photo.FocalLength": "focal",
    "Exif.Photo.FocalLengthIn35mmFilm": "focal_35mm",
    "Exif.Photo.ExposureBiasValue": "ev_bias",
    "Exif.Photo.Flash": "flash",
    "Exif.Image.Orientation": "orientation",
    "Exif.Photo.DateTimeOriginal": "captured",
    "Exif.Image.DateTime": "file_time",
    "Exif.Photo.BodySerialNumber": "body_serial",
    # Sony: the numeric lens id is often the only lens information present
    "Exif.Sony2.LensID": "sony_lens_id",
    "Exif.Sony1.LensID": "sony_lens_id",
    "Exif.Sony2.SonyRawFileType": "sony_raw_file_type",
    "Exif.Sony1.SonyRawFileType": "sony_raw_file_type",
}


# Rough anchor points for the heuristic below, in bytes per pixel.
# Sony 14-bit uncompressed sits near 2.3-2.6; lossless-compressed near 1.0-1.3;
# lossy-compressed near 0.9-1.0. The bands overlap between bodies and bit
# depths, so this is only ever reported as a hint - `exiftool -SonyRawFileType`
# gives the real answer for a specific file.
_BPP_UNCOMPRESSED = 2.0
_BPP_COMPRESSED = 1.15
_BPP_IMPLAUSIBLE = 0.35   # no RAW format stores 16 MPix in 0.1 bytes/px


def raw_hint(entry: dict[str, Any]) -> dict[str, Any]:
    """Facts about how the file stores its pixels, plus explicit caveats.

    Deliberately reports measurements (bytes per pixel) rather than asserting
    "lossy" / "lossless": the tag that would settle it is defined by ExifTool,
    which is not installed here.
    """
    hint: dict[str, Any] = {}
    w, h, size = entry.get("w") or 0, entry.get("h") or 0, entry.get("bytes") or 0
    if w and h and size:
        bpp = size / float(w * h)
        hint["bytes_per_pixel"] = round(bpp, 3)
        if bpp >= _BPP_UNCOMPRESSED:
            hint["storage"] = "uncompressed_likely"
        elif bpp <= _BPP_IMPLAUSIBLE:
            hint["storage"] = "implausibly_small"
        elif bpp <= _BPP_COMPRESSED:
            hint["storage"] = "compressed_likely"
        else:
            hint["storage"] = "ambiguous"
        hint["shadow_push_careful"] = bpp <= _BPP_COMPRESSED

    if entry.get("sony_raw_file_type"):
        hint["sony_raw_file_type"] = entry["sony_raw_file_type"]

    notes: list[str] = []
    make = str(entry.get("make") or "").lower()
    if "sony" in make:
        if not entry.get("lens"):
            notes.append("Sony body with no LensModel tag: lens corrections may "
                         "need the numeric LensID / manual selection in darktable")
        if entry.get("sony_raw_file_type") is None:
            notes.append("SonyRawFileType absent (install exiftool to read it)")
    if hint.get("storage") == "implausibly_small":
        notes.append("file is far too small for its pixel count: verify it is a "
                     "real camera original before trusting the render")
    elif hint.get("shadow_push_careful"):
        notes.append("compressed storage suspected: avoid big shadow lifts; "
                     "prefer exposure/crop over recovery")
    if notes:
        hint["notes"] = notes
    return hint


def _tags(path: Path) -> dict[str, str]:
    out = util.run(["exiv2", "-Pkt", str(path)], check=False)
    tags: dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^(\S+)\s{2,}(.*)$", line)
        if m:
            tags[m.group(1)] = m.group(2).strip()
    return tags


def _parse_time(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    return None


def inventory(folder: str | Path, *, recursive: bool = True) -> list[dict[str, Any]]:
    folder = Path(folder)
    pattern = "**/*" if recursive else "*"
    files = sorted(p for p in folder.glob(pattern)
                   if p.is_file() and p.suffix.lower() in IMAGE_EXT
                   and not p.name.startswith("."))

    entries: list[dict[str, Any]] = []
    for idx, path in enumerate(files):
        tags = _tags(path)
        try:
            w, h = develop.source_dims(path)
        except Exception:
            w = h = 0
        entry: dict[str, Any] = {
            "index": idx,
            "image": path.name,
            "path": str(path),
            "ext": path.suffix.lower().lstrip("."),
            "bytes": path.stat().st_size,
            "w": w, "h": h,
            "is_raw": path.suffix.lower() in util.RAW_EXT,
        }
        for key, name in _FIELDS.items():
            if key in tags:
                entry[name] = tags[key]
        entry["captured_dt"] = (_parse_time(entry.get("captured")) or
                                _parse_time(entry.get("file_time")))
        if entry["is_raw"]:
            entry["raw_hint"] = raw_hint(entry)
        entries.append(entry)

    entries.sort(key=lambda e: (e["captured_dt"] or datetime.max, e["image"]))
    for idx, entry in enumerate(entries):
        entry["index"] = idx
        ts = entry.pop("captured_dt", None)
        entry["captured_ts"] = ts.isoformat() if ts else None
    return entries


def _entry_time(entry: dict[str, Any]) -> Optional[datetime]:
    raw = entry.get("captured_ts")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return _parse_time(str(raw))


def group_bursts(entries: Iterable[dict[str, Any]], gap_seconds: float = 2.0) -> list[list[dict[str, Any]]]:
    """Bursts = frames from one shutter press, i.e. within `gap_seconds`.

    Frames without a timestamp become their own singleton group rather than
    silently joining the previous burst.
    """
    bursts: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for entry in entries:
        ts = _entry_time(entry)
        prev = _entry_time(current[-1]) if current else None
        if not current or ts is None or prev is None:
            if current:
                bursts.append(current)
            current = [entry]
        elif (ts - prev).total_seconds() <= gap_seconds:
            current.append(entry)
        else:
            bursts.append(current)
            current = [entry]
    if current:
        bursts.append(current)
    return bursts


def load(work_root: str | Path) -> list[dict[str, Any]]:
    return util.read_json(Path(work_root) / "manifest.json")


def flatten(entries: list[dict[str, Any]]) -> str:
    lines = []
    for e in entries:
        hint = e.get("raw_hint") or {}
        storage = hint.get("storage", "-")
        lines.append(
            f"{e['index']:03d} {e['image']:<26} {e.get('w', 0):>5}x{e.get('h', 0):<5} "
            f"{str(e.get('captured', '-')):<20} {str(e.get('camera', '-')):<20} "
            f"ISO {str(e.get('iso', '-')):<6} {str(e.get('shutter', '-')):<10} "
            f"f/{str(e.get('aperture', '-')):<5} {str(e.get('lens', '-')):<18} "
            f"{storage}")
    return "\n".join(lines)
