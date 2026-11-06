# AGENTS.md — photoflow

An agent-operable photo pipeline: **RAW → select → crop/adjust**, where the
reviewing model only ever sees small proofs. Python package `photoflow/`, CLI is
`python3 -m photoflow <command>`.

**Before running anything, read
[`.pi/skills/wedding-photo-pipeline/SKILL.md`](.pi/skills/wedding-photo-pipeline/SKILL.md).**
It is the operational runbook (shoot profile, stage sequence, validated failure
modes). This file is the short version and the non-negotiables.

## The 60-second version

```bash
python3 -m photoflow.selftest --e2e            # expect 39/39
./.pi/skills/wedding-photo-pipeline/scripts/prepare.sh "<shoot-dir>" work
#   -> inventory, masters, 800px proofs, metrics, contact sheets, review bundles
# then: review the proofs yourself, write strict JSON, and
python3 -m photoflow review --work work --ingest <response>.json
python3 -m photoflow render --work work --edge 400     # cheap check
python3 -m photoflow render --work work --full         # delivery
```

## Non-negotiables

1. **Never write into the shoot directory.** Everything goes in the `work/`
   workspace. (The one way this went wrong: `dcraw_emu -v FILE` writes
   `FILE.ppm` next to the input — never run it on a shoot.)
2. **Never send the model a full-resolution image** when a ~800 px proof
   carries the same decision. That is the entire cost model.
3. **Never hand-write darktable XMP `op_params`** — they are gzip/base64 binary
   structs. Decisions live in `work/recipes/*.json`, which is inspectable,
   diffable and re-runnable.
4. **Never upscale a thumbnail into a proof.** If the embedded preview is too
   small the code refuses and develops the RAW instead; do not "help".
5. **Render crops the developed master, never the RAW.** ImageMagick cannot read
   every RAW, and cropping a RAW bypasses darktable's look.

## The invariant that everything rests on

A recipe stores geometry only as `norm` fractions of the frame — never pixels.
So a 4:5 crop read off an 800 px proof renders as the identical framing on the
4024×6022 master (measured on a real ARW: proof `320×400`, master `3009×3764`,
mean pixel difference `0.0084`). If you change geometry code, keep
`python3 -m photoflow.selftest --e2e` green — those tests exist to protect this.

Frames are recorded in **display** orientation. Real ARW files are stored
rotated (`6000×4000` @ Orientation 6) and export as `4024×6022`; if EXIF and the
rendered artefact disagree, **the artefact wins**.

## Shoot profile (confirmed)

Sony **ILCE-7M2 (A7 II)** for the wedding, same body as the 2023 samples.
Outdoors, so better ISO than the indoor ISO 12800 tests. Review masters are
**JPEG** (`--format jpg`, ~16 MB/frame); use `--format tif` only for the final
delivery pass. `.ARW` at ~1.035 bytes/px means lossy 11-bit compressed, so
`SHADOW_PUSH_CAREFUL` in the review prompt is real: cap `|exposure_ev| <= 0.7`
and prefer crop/contrast over shadow recovery.

## Where things are

| path | what |
|---|---|
| `README.md` | design rationale, cost model, verified environment quirks |
| `.pi/skills/wedding-photo-pipeline/SKILL.md` | the runbook |
| `.../references/review-protocol.md` | exact JSON contract for reviews |
| `photoflow/geometry.py` | resolution-independent crop math (the invariant) |
| `photoflow/recipe.py` | the JSON contract + validation + aspect self-repair |
| `photoflow/render.py` | recipe → ImageMagick argv |
| `photoflow/review.py` | request bundles, strict parsing, token budget |
| `photoflow/develop.py` | darktable-cli grammar, masters, orientation |
| `photoflow/exifmeta.py` | copies capture date/camera/lens/GPS onto renders |
| `photoflow/selftest.py` | 39 tests incl. proof-vs-master equivalence |

## Known gaps — do not claim these work

* **Shoot-specific style now exists for this shoot.** `pf-cam6w-slight` in
  `work/dtcfg/template/data.db` is fitted to this shoot's own camera JPEGs; the
  104 masters and 25 deliveries use it. The user's own darktable library still
  has 0 styles, so a new shoot starts from defaults. Method and state are in
  `work/camfit/README.md`; the machinery (`photoflow styles --init /
  --from-library / --import / --set`) is verified. See README section 7.
* Never set `--apply-custom-presets 0`: it stops darktable creating `data.db`,
  which silently disables all styles.
* No face-aware crops; no perceptual burst de-duplication; `render` is serial.
* Denoise/white-balance are refused by the local renderer (`UnsupportedOp`) on
  purpose — they belong in darktable. Do not approximate them silently.
* The camera-match style stores `bilat` **disabled**, so the ISO-1250 masters
  carry real sensor grain (most visible at 1:1 in shadows). Output sharpening
  uses a 5% threshold (`render.SHARPEN_THRESHOLD`) so it sharpens edges instead
  of doubling that grain. Truly cleaning the grain means enabling denoise in the
  style and re-developing the masters from RAW — not adding a local filter.
