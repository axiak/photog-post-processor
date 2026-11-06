# Review protocol (detail)

Loaded on demand from `SKILL.md`. The code that enforces all of this is
`photoflow/review.py` (request building, parsing, validation) and
`photoflow/recipe.py` (range checks, aspect self-repair).

---

## Why proofs, not masters

Reviewing 100 frames at full size costs ~174k image tokens; the tiered review
costs ~72k (59% less) and every frame is still seen at least once. See
`python -m photoflow budget 100`.

The guarantee that makes this safe: a crop decided on an 800 px proof renders as
the identical framing on the 4024x6022 master. Coordinates are stored only as
fractions of the frame (`norm`), never as resolution-dependent pixels.

---

## Coordinate frames

```
source/master   4024x6022 for this camera (display orientation!)
proof           ~535x800, same aspect
norm            floats in [0,1] of the frame  <- what the recipe stores
```

`crop.space = "proof"` means pixels of the proof image you were shown, origin
top-left. The pipeline divides by the proof's own width/height, so it does not
need to know its size:

```
norm.x = proof.x / proof_width
```

Consequence: reading a rectangle off the image you actually looked at is always
correct, even if the proof is regenerated at a different size later.

---

## Full accepted item shape

```json
{
  "id": "DSC02327.ARW",          // required, verbatim from the request
  "verdict": "keep",             // required: keep|maybe|reject|duplicate
  "score": 78,                   // 0-100
  "confidence": 0.82,            // 0-1
  "issues": ["..."],             // free text, short
  "rationale": "one sentence",   // <= 400 chars
  "rotate_deg": -0.8,            // -10..10 accepted, keep within +/-3
  "crop": {"space": "proof", "x": 67, "y": 0, "w": 400, "h": 500},
  "aspect": "4:5",               // optional; presets in review.DELIVERABLES
  "adjust": {
    "exposure_ev": 0.35,         // -3..3, applied in linear light
    "contrast": 0.1,             // -1..1, sigmoidal S-curve
    "saturation": 0.05,          // -1..1
    "sharpening": 0.3,           // 0..2, applied after resize
    "vignette": 0.10             // 0..0.5 radial corner darkening; 0.10 subtle
  }
}
```

Anything not listed is rejected (unknown keys are a hard error, not a warning).
`white_balance`, `denoise`, `highlights`, `shadows`, `tone_curve`, `clarity`
are accepted **only** in a recipe's `develop` block and are refused by the local
renderer — they belong in darktable.

---

## Judgement order

1. Focus / motion blur (cross-check the `sharp` number; it is computed on a
   normalised 1024 px copy so it is comparable across the whole shoot)
2. Eyes open, expression, no blink, no half-turn
3. Blown highlights: dress, candles, window light, stage spots
4. Edge intrusions: heads, elbows, phones, exit signs
5. Horizon tilt / vertical geometry
6. Headroom, lead room, subject placement
7. Deliverable crop without amputating hands or veils

Negative evidence beats optimism: a blown highlight or a cut-off hand is
`reject`, not `maybe`.

`duplicate` is a real verdict and is skipped during render. Use it for the 5th
frame of a burst, not for a frame that is merely similar.

---

## Aspect handling

If you declare `"aspect": "4:5"` and draw a rectangle that is not 4:5, the recipe
**grows** the crop around the same centre to match and records a warning. It does
not fail the batch. Still, draw the right rectangle: the grown version may not be
what you intended at the edges.

Presets: `free, social_4x5, social_1x1, social_9x16, story_9x16, print_3x2,
album_7x5, wide_16x9`.

---

## Response hygiene

* JSON only. No prose, no ```` ```json ```` fence (fences are tolerated by the
  parser but should not be relied on).
* Every `id` must come from the request, verbatim.
* Do not silently drop frames from the batch; a missing frame simply gets no
  recipe and will not render, which is easy to miss.

---

## Self-check before ingesting

```
[ ] every id in the response appears in request_00N.json
[ ] no crop has x+w > 1 or y+h > 1 when expressed in norm
[ ] no crop smaller than 2% of the frame in either axis
[ ] exposure moves are within +/-0.7 EV when SHADOW_PUSH_CAREFUL is set
[ ] rotate_deg is a tilt correction, not a creative rotation
```
