"""The review protocol: what the model is asked, and what is accepted back.

Two hard rules make this safe to run unattended:

1. The model answers in a strict JSON schema. Anything else is rejected, never
   "interpreted".
2. Coordinates are either fractions of the frame (``norm``) or pixels **of the
   proof it was shown** (``proof``). Both are converted to norm here, so a crop
   read off an 800 px proof lands exactly on the master.

Every accepted answer is clamped and range-checked by :mod:`photoflow.recipe`
before it can reach the renderer.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Optional

from . import geometry, measure, recipe as recipe_mod, util
from .recipe import Adjust, Geometry, Recipe, RecipeError

__all__ = [
    "REVIEW_SCHEMA",
    "VERDICTS",
    "DELIVERABLES",
    "build_request",
    "render_prompt",
    "parse_response",
    "to_recipe",
    "estimate_image_tokens",
    "estimate_batch",
    "budget_plan",
]

VERDICTS = ("keep", "maybe", "reject", "duplicate")

# Deliverable aspect presets, so "make it Instagram" is not ambiguous.
DELIVERABLES: dict[str, Optional[str]] = {
    "free": None,
    "social_4x5": "4:5",
    "social_1x1": "1:1",
    "social_9x16": "9:16",
    "story_9x16": "9:16",
    "print_3x2": "3:2",
    "album_7x5": "7:5",
    "wide_16x9": "16:9",
}

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["items"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "verdict"],
                "additionalProperties": False,
                "properties": {
                    "id": {"type": "string", "description": "image name, verbatim"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                    "score": {"type": "number", "minimum": 0, "maximum": 100},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string", "maxLength": 400},
                    "rotate_deg": {"type": "number", "minimum": -10, "maximum": 10},
                    "crop": {
                        "type": "object",
                        "required": ["x", "y", "w", "h"],
                        "additionalProperties": False,
                        "properties": {
                            "space": {"type": "string", "enum": ["norm", "proof"]},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "w": {"type": "number"},
                            "h": {"type": "number"},
                        },
                    },
                    "aspect": {"type": "string"},
                    "adjust": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "exposure_ev": {"type": "number"},
                            "contrast": {"type": "number"},
                            "saturation": {"type": "number"},
                            "sharpening": {"type": "number"},
                            "vignette": {"type": "number", "minimum": 0, "maximum": 0.5},
                        },
                    },
                },
            },
        }
    },
}

PROMPT_TEMPLATE = """\
You are reviewing wedding photographs. You see small proofs, never masters.
Your answer drives an automated renderer, so it is executed exactly as written.

HARD RULES
- Reply with JSON only: one object, no prose, no markdown fence.
- Use the `id` values verbatim. Do not invent ids or reorder-based references.
- Coordinates: fractions of the frame (0..1, `"space":"norm"`) or pixels of
  the proof you were shown (`"space":"proof"`, origin top-left). Never mix.
  Never emit coordinates outside the frame.
- `rotate_deg` is clockwise-positive and tiny: correct horizon tilt only.
  Above 3 degrees, say so in `issues` instead of guessing.
- Only propose a crop you can actually see. If the framing is already correct,
  omit `crop` entirely.
- Negative evidence beats optimism: a blown highlight or a cut-off hand is a
  `reject`, not a `maybe`.

DELIVERABLE: {deliverable} (aspect {aspect})
TARGET ASPECT PRESETS: {presets}

WHAT TO CHECK, IN THIS ORDER
1. Focus and motion blur (sharpness number is in the measurements)
2. Eyes open, expressions, no blink, no halfway-turn faces
3. Blown highlights: white dress, candles, window light, stage spots
4. Intrusions at the frame edge: heads, elbows, phones, exit signs
5. Horizon tilt and vertical geometry
6. Headroom and lead room; subject placement
7. The crop the deliverable needs, without amputating hands or veils

MEASUREMENTS (per image, computed at 1024 px - your eyes are still the judge)
{measurements}

RAW STORAGE FACTS (measured, not inferred - respect them)
{raw_notes}

IMAGES
{images}

SCHEMA
{schema}
"""


# --------------------------------------------------------------------------
# cost model
# --------------------------------------------------------------------------
def estimate_image_tokens(w: int, h: int) -> dict[str, int]:
    """Rough token cost of one image, by two common accounting schemes.

    Model-agnostic on purpose: patch based (28x28 tiles) and area based
    (~750 px per token up to 1.15 MP). Both are approximations - treat them as
    an order-of-magnitude budget, not a quote.
    """
    patch = math.ceil(w / 28) * math.ceil(h / 28)
    area = math.ceil(min(w * h, 1_150_000) / 750)
    return {"patches_28": patch, "area_750": area}


def estimate_batch(n_images: int, w: int = 800, h: int = 800,
                   prompt_tokens: int = 700) -> dict[str, int]:
    tok = estimate_image_tokens(w, h)
    return {
        "images": n_images,
        "per_image_patches": tok["patches_28"],
        "per_image_area": tok["area_750"],
        "total_patches": n_images * tok["patches_28"] + n_images * prompt_tokens,
        "total_area": n_images * tok["area_750"] + n_images * prompt_tokens,
    }


def budget_plan(n_photos: int, *, per_sheet: int = 25,
                shortlist: int = 25, proof_edge: int = 800,
                sheet_edge: int = 2200) -> dict[str, Any]:
    """Cheapest tiered review that still shows every photo at least once."""
    sheets = math.ceil(n_photos / per_sheet)
    sheet_tok = estimate_image_tokens(sheet_edge, sheet_edge)["patches_28"]
    proof_tok = estimate_image_tokens(proof_edge, proof_edge)["patches_28"]
    tier0 = sheets * (sheet_tok + 800)
    tier1 = shortlist * (proof_tok + 900)
    return {
        "photos": n_photos,
        "tier0_sheets": sheets,
        "tier0_tokens": tier0,
        "tier1_proofs": shortlist,
        "tier1_tokens": tier1,
        "total_tokens": tier0 + tier1,
        "naive_all_800px_tokens": n_photos * (proof_tok + 900),
        "saving_vs_naive": round(1 - (tier0 + tier1) / max(1, n_photos * (proof_tok + 900)), 3),
    }


# --------------------------------------------------------------------------
# request construction
# --------------------------------------------------------------------------
def _raw_note_line(name: str, hint: dict[str, Any]) -> str:
    if not hint:
        return f"- {name}: (no raw metadata)"
    bits = []
    if "bytes_per_pixel" in hint:
        bits.append(f"bytes/px={hint['bytes_per_pixel']}")
    if hint.get("sony_raw_file_type"):
        bits.append(f"SonyRawFileType={hint['sony_raw_file_type']}")
    if hint.get("storage"):
        bits.append(f"storage={hint['storage']}")
    if hint.get("shadow_push_careful"):
        bits.append("SHADOW_PUSH_CAREFUL (cap|exposure_ev|<=0.7, "
                    "prefer crop/contrast over recovery)")
    for note in hint.get("notes") or []:
        bits.append(f"note: {note}")
    return f"- {name}: " + "; ".join(bits)


def build_request(entries: list[dict[str, Any]], *,
                  metrics: Optional[dict[str, dict]] = None,
                  raw_info: Optional[dict[str, dict]] = None,
                  deliverable: str = "social_4x5",
                  instructions: Optional[str] = None) -> dict[str, Any]:
    """Bundle everything the reviewer needs; one JSON file per batch."""
    if deliverable not in DELIVERABLES:
        raise ValueError(f"unknown deliverable {deliverable!r}; "
                         f"choose from {sorted(DELIVERABLES)}")

    images = []
    meas_lines = []
    raw_lines = []
    for entry in entries:
        proof = entry["proof"]
        m = (metrics or {}).get(entry["image"]) or measure.measure(proof)
        images.append({
            "id": entry["image"],
            "path": str(proof),
            "proof_w": entry["proof_size"]["w"],
            "proof_h": entry["proof_size"]["h"],
            "frame_w": entry["frame"]["w"],
            "frame_h": entry["frame"]["h"],
        })
        meas_lines.append(f"- {entry['image']}: {measure.metrics_summary(m)}")
        raw_lines.append(_raw_note_line(entry["image"],
                                        (raw_info or {}).get(entry["image"], {})))

    measurements = "\n".join(meas_lines)
    raw_notes = "\n".join(raw_lines)
    return {
        "deliverable": deliverable,
        "aspect": DELIVERABLES[deliverable],
        "instructions": instructions,
        "images": images,
        "measurements": measurements,
        "raw_notes": raw_notes,
        "prompt": render_prompt(images=images, measurements=measurements,
                                raw_notes=raw_notes, deliverable=deliverable,
                                instructions=instructions),
        "schema": REVIEW_SCHEMA,
        "token_estimate": estimate_batch(len(images)),
    }


def render_prompt(*, images: list[dict[str, Any]], measurements: str,
                  raw_notes: str = "",
                  deliverable: str = "social_4x5",
                  instructions: Optional[str] = None) -> str:
    presets = ", ".join(f"{k}={v or 'free'}" for k, v in DELIVERABLES.items())
    lines = []
    for im in images:
        lines.append(f"- id={im['id']}  proof={im['proof_w']}x{im['proof_h']}  "
                     f"master={im['frame_w']}x{im['frame_h']}  path={im['path']}")
    text = PROMPT_TEMPLATE.format(
        deliverable=deliverable,
        aspect=DELIVERABLES[deliverable],
        presets=presets,
        measurements=measurements,
        raw_notes=raw_notes or "- (none)",
        images="\n".join(lines),
        schema=json.dumps(REVIEW_SCHEMA, indent=1),
    )
    if instructions:
        text += f"\nADDITIONAL INSTRUCTIONS FROM THE PHOTOGRAPHER\n{instructions}\n"
    return text


# --------------------------------------------------------------------------
# response ingestion
# --------------------------------------------------------------------------
def _strip_fence(text: str) -> str:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    if fence:
        return fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start:end + 1]
    return text


def parse_response(text: str) -> dict[str, Any]:
    """Parse + shape-check. Raises RecipeError with every problem at once."""
    raw = _strip_fence(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RecipeError([f"response is not valid JSON: {exc}"]) from exc

    problems: list[str] = []
    if not isinstance(data, dict) or "items" not in data:
        raise RecipeError(["response must be an object with an 'items' array"])
    if not isinstance(data["items"], list) or not data["items"]:
        raise RecipeError(["'items' must be a non-empty array"])

    allowed = set(REVIEW_SCHEMA["properties"]["items"]["items"]["properties"])
    cleaned: list[dict[str, Any]] = []
    for i, item in enumerate(data["items"]):
        if not isinstance(item, dict):
            problems.append(f"items[{i}] is not an object")
            continue
        extra = set(item) - allowed
        if extra:
            problems.append(f"items[{i}] has unknown key(s): {', '.join(sorted(extra))}")
            continue
        if "id" not in item:
            problems.append(f"items[{i}] missing id")
            continue
        if item.get("verdict") not in VERDICTS:
            problems.append(f"items[{i}] verdict must be one of {VERDICTS}, "
                            f"got {item.get('verdict')!r}")
            continue
        cleaned.append(item)

    if problems:
        raise RecipeError(problems)
    return {"items": cleaned}


def _crop_to_norm(crop: dict[str, Any], proof_w: int, proof_h: int) -> geometry.Rect:
    space = crop.get("space", "norm")
    rect = geometry.Rect(float(crop["x"]), float(crop["y"]),
                         float(crop["w"]), float(crop["h"]))
    if space == "proof":
        return geometry.proof_to_norm(rect, proof_w, proof_h)
    if space != "norm":
        raise RecipeError([f"crop.space must be 'norm' or 'proof', got {space!r}"])
    return rect


def to_recipe(entry: dict[str, Any], item: dict[str, Any],
              base: Optional[Recipe] = None) -> Recipe:
    """Fold one validated review item into a renderable recipe."""
    if item.get("verdict") == "reject":
        verdict_note = "rejected; do not render"
    else:
        verdict_note = ""

    rec = base or Recipe.new(entry["image"], entry["frame"]["w"], entry["frame"]["h"])

    if "rotate_deg" in item:
        rec.geometry.rotate_deg = float(item["rotate_deg"])

    pw = entry["proof_size"]["w"]
    ph = entry["proof_size"]["h"]
    if "crop" in item:
        rect = _crop_to_norm(item["crop"], pw, ph)
        rec.geometry.crop = geometry.clamp_into_frame(rect)
    if item.get("aspect"):
        rec.geometry.aspect = str(item["aspect"])

    adj = item.get("adjust") or {}
    if adj:
        rec.adjust = Adjust(
            exposure_ev=float(adj.get("exposure_ev", rec.adjust.exposure_ev)),
            contrast=float(adj.get("contrast", rec.adjust.contrast)),
            saturation=float(adj.get("saturation", rec.adjust.saturation)),
            sharpening=float(adj.get("sharpening", rec.adjust.sharpening)),
            vignette=float(adj.get("vignette", rec.adjust.vignette)),
        )

    rec.review = {
        "verdict": item.get("verdict"),
        "score": item.get("score"),
        "confidence": item.get("confidence"),
        "issues": list(item.get("issues") or []),
        "rationale": item.get("rationale"),
        "note": verdict_note,
    }
    rec.validate()
    return rec
