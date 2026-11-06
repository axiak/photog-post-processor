---
name: wedding-photo-pipeline
description: Runs the photoflow RAW-to-delivery pipeline for a Sony A7 II shoot - inventory, darktable masters, cheap 800px proofs, agent review of those proofs with strict JSON crops/adjustments, and resolution-independent rendering. Use when selecting and post-processing wedding/event photos from .ARW files in this repository.
compatibility: Needs darktable-cli 5.x, ImageMagick 7, exiv2, libvips, Python 3 with numpy + Pillow on PATH.
metadata:
  project: photoflow
  camera: Sony ILCE-7M2 (A7 II)
---

# Wedding photo pipeline

Operational runbook. The *design* reasoning lives in `README.md`; this file is
the sequence to actually execute, plus the failure modes that cost real debugging
time on this exact machine.

**Never modify the shoot directory.** Every stage writes into a workspace
directory. The only exception is `exiv2`/`darktable`, which are read-only here.

---

## Shoot profile (confirmed with the user)

| fact | value | consequence |
|---|---|---|
| body | Sony ILCE-7M2 (A7 II), same body for the wedding | 24 MP, stored `6000x4000` @ Orientation 6, exports `4024x6022` |
| RAW format | `.ARW`, 24.8 MB/frame ≈ 1.035 bytes/px | lossy 11-bit compressed → `SHADOW_PUSH_CAREFUL` will fire |
| lighting | **outdoors**, better ISO than the 2023 indoor samples | denoise is not the critical path; expect lower ISO |
| review masters | **JPEG** (`--format jpg`), not 16-bit TIFF | ~16 MB/frame → ~1.6 GB per 100; use `tif` only for final delivery |
| deliverable aspect | ask the user; default `social_4x5` | `review --deliverable` |

---

## 0. Preflight (30 seconds, do not skip)

```bash
python -m photoflow.selftest --e2e     # expect: 35/35 passed
which darktable-cli magick exiv2 vips
```

Then prove one real frame end-to-end before touching 100:

```bash
mkdir -p /tmp/pf_probe && cp "<shoot>/SOMETHING.ARW" /tmp/pf_probe/
python -m photoflow inventory --photos /tmp/pf_probe --work /tmp/pf_probe_work
python -m photoflow masters  --work /tmp/pf_probe_work --jobs 1
python -m photoflow proofs   --work /tmp/pf_probe_work --from-masters
```

Check `proofs/index.json`: `frame` must be **portrait** (`4024x6022`) for a
portrait shot, `proof_size` ~`535x800`, and `sx ≈ sy`. If `frame` is landscape
while `proof_size` is portrait, stop — that is the orientation bug returning.

Do **not** run `dcraw_emu` anywhere near the shoot: `dcraw_emu -v FILE` writes
`FILE.ppm` next to the input.

---

## 1. Full run (the happy path)

`scripts/prepare.sh` does steps 1-5 and prints the review bundle paths:

```bash
./.pi/skills/wedding-photo-pipeline/scripts/prepare.sh "<shoot-dir>" work          # review tier
./.pi/skills/wedding-photo-pipeline/scripts/prepare.sh "<shoot-dir>" work --fast    # culling tier
```

By hand:

```bash
python -m photoflow inventory --photos "<shoot>" --work work --print --bursts
python -m photoflow masters  --work work --jobs 4 --format jpg
python -m photoflow proofs   --work work --from-masters --long-edge 800
python -m photoflow measure  --work work --print
python -m photoflow sheets   --work work --per-sheet 25 --tile 360 --cols 6
python -m photoflow review   --work work --per-batch 10 --deliverable social_4x5
```

Expected timings on this machine (A7 II): masters ≈ 1.0 s/frame at 4 jobs,
proofs-from-masters ≈ 0.1 s/frame, `--mode embedded` ≈ 0.08 s/frame.

`--fast` uses `--mode embedded` (the camera's own 1616x1080 JPEG). It is
ungraded but 12x faster and fine for triage. It must produce portrait proofs
for portrait frames — if it does not, stop.

---

## 2. Review the proofs (this is the agent's own job)

Read the contact sheets first (`work/sheets/sheet_*.jpg`), then the individual
800 px proofs. **Never ask for a full-resolution render to "check" something** —
that defeats the cost model and the master is not more informative at 800 px.

Each proof arrives with a measurement line:

```
p1=0.046 p50=0.529 p99=0.619 clip+=0.0% clip-=0.0% sharp=15.1 exp_sugg=-0.43EV flags=-
```

and a raw-storage line:

```
bytes/px=1.034; storage=compressed_likely; SHADOW_PUSH_CAREFUL (cap|exposure_ev|<=0.7 ...)
```

Obey `SHADOW_PUSH_CAREFUL`: this camera's RAW is lossy 11-bit, so big shadow
lifts reveal compression artefacts. Prefer crop and contrast over recovery.

Answer with **JSON only**, matching `work/review/request_00N.json`'s `schema`:

```json
{"items": [
  {"id": "DSC02327.ARW", "verdict": "keep", "score": 78, "confidence": 0.82,
   "crop": {"space": "proof", "x": 67, "y": 0, "w": 400, "h": 500},
   "aspect": "4:5", "rotate_deg": -0.8,
   "adjust": {"exposure_ev": 0.35, "contrast": 0.1, "saturation": 0.05, "sharpening": 0.3,
              "vignette": 0.10},
   "issues": ["ISO 12800 noise in shadows"], "rationale": "tightened to the subject"}
]}
```

Rules that the validator enforces (rejections are all-or-nothing per response):

* `verdict` ∈ `keep | maybe | reject | duplicate`
* `crop.space` is `"proof"` (pixels of the proof you were shown) or `"norm"`
  (0-1 fractions). Never mix within one item.
* ranges: `exposure_ev` ±3, `contrast` ±1, `saturation` ±1, `sharpening` 0-2,
  `vignette` 0-0.5 (use ~0.10; 0.25 is already a visible edge burn),
  `rotate_deg` ±45 (keep within ±3 for real work)
* no unknown keys anywhere
* omit `crop` when the framing is already right

Write the response to `work/review/response_00N.json`, then:

```bash
python -m photoflow review --work work --ingest work/review/response_00N.json
```

Any warning it prints about aspect is informational: the recipe grows the crop
to the declared aspect around the same centre.

Full contract and edge cases: [references/review-protocol.md](references/review-protocol.md).

---

## 3. Render and verify

```bash
python -m photoflow render --work work --edge 400          # cheap proofs
python -m photoflow render --work work --full              # masters
```

`render` develops any missing master automatically, then crops **the master,
never the RAW**. `reject` and `duplicate` recipes are skipped
(`--keep-duplicates` to override). For final delivery, regenerate masters at
16-bit first:

```bash
python -m photoflow masters --work work --format tif --force --jobs 4
```

Acceptance checks before declaring done:

```bash
python -m photoflow exif --work work --verify    # date/camera/lens on the JPEGs
# add --gps "LAT,LON" to stamp a location; render --full re-applies it automatically
```

```bash
python - <<'EOF'
import sys, numpy as np; sys.path.insert(0,'.')
from photoflow import measure, render
p = measure._load_rgb01("work/out/proof/DSC02327.jpg", 400)
f = measure._load_rgb01("work/out/full/DSC02327.jpg", 400)
print("proof vs master mean |diff|:", round(float(np.abs(p-f).mean()), 4))   # want < 0.02
print(render.image_size("work/out/full/DSC02327.jpg"))
EOF
```

Every full render must have the requested aspect (±2%).

**Caveat: the proof-vs-master check only means "geometry is equivalent" when
`sharpening` is 0.** Sharpening is `-unsharp 0x0.9+amount+0.05` (5% threshold)
applied *after* resize, so it is not scale-invariant: the proof is sharpened at
400 px, the master at 4024 px, and the comparison then reports the sharpening
difference rather than the crop.
Measured here with `sharpening: 0.45`: 0.0239 (looks like a broken invariant);
with sharpening forced to 0 on the same recipes: 0.0085-0.0110 over four crops.
Re-run with `recipe.adjust.sharpening = 0.0` before concluding geometry broke.

Measure highlight integrity on the *rendered* output
(`measure` over `work/out/full/*.jpg`), not only on `work/proofs/`. Recipe
exposure compounds with the style: 8 of 25 frames here clipped 2-10% until their
`exposure_ev` was pulled down, and the proof-only metrics never revealed it.

---

## 4. Failure modes (all hit on this machine, all diagnosed)

| symptom | cause | fix |
|---|---|---|
| `darktable-cli` prints usage, exit 1, no message | a CLI option after `--core` (`--width`, `--height`, `--apply-custom-presets`) | CLI opts + positionals first, then `--core` + `--configdir`/`--conf` |
| `database is locked` | two runs sharing a configdir | one `dtcfg/wNN` per worker (already automatic) |
| `database ... is out-of-date. aborting` | global `~/.config/darktable/data.db` is old | always pass an isolated `--configdir` (already automatic) |
| `magick: Unsupported file format or not RAW file` | ImageMagick/libraw cannot read that RAW | render from `work/masters/`, never from the RAW |
| frames landscape but proofs portrait (or vice versa) | EXIF orientation ignored | `develop.display_dims()`; the rendered artefact wins over EXIF |
| proof is sideways, crop lands in the wrong place | embedded preview carries no orientation tag | `develop.match_display_orientation()` rotates from the parent RAW |
| proof is blurry/upscaled | source only had a 160x112 EXIF thumbnail | `extract_embedded_preview` refuses and falls back to darktable |
| `TypeError: datetime is not JSON serializable` | would mean inventory regressed | `manifest.inventory` stores ISO strings |
| `--style NAME` errors "cannot find the style" | name is localized, or the db was never created | use the leaf name; ensure `--apply-custom-presets 1` (now default) |
| style has no effect on a re-render | darktable wrote `name-1.jpg` instead of overwriting | targets are unlinked first; check for stray `*_01.jpg` |
| style switch does not change proofs | masters are cached | handled by `.meta.json`; run `masters --force` if a stale one predates it |

---

## 5. Known gaps (do not pretend otherwise)

1. **Style import is implemented**, not a gap: `styles --init / --from-library /
   --import / --set` writes into `work/dtcfg/template/data.db` and worker
   configdirs copy it. The caveat is only that `--style` takes a name from that
   database, never a `.dtstyle` path.
2. **No face-aware crops.** The model places rectangles by eye on an 800 px
   proof. `opencv-python-headless` (YuNet) would make crops eye-accurate.
3. **Burst de-dup is timestamp-only** (2 s gap). Perceptual hashing plus
   sharpest-of-group is not implemented.
4. **`render` is serial.** Fine at proof size; a 100-frame 16-bit delivery pass
   will want a pool.
5. **Denoise lives only in darktable.** The local renderer raises
   `UnsupportedOp` rather than faking it. For an outdoors shoot this is a
   non-issue; for the old ISO 12800 indoor set it would matter.
6. **A shoot-specific style exists for this shoot:** `pf-cam6w-slight`, fitted
   from its own camera JPEGs (see 5b). The user's own darktable library still has
   0 styles, so a new shoot starts from defaults — do not claim a look was
   applied there.

---

## 5b. Styles (the user's look)

A style is a saved module stack (which modules, their params, blend, order), not
a LUT. `--style` takes a **name from the configdir database**, never a path.

```bash
python -m photoflow styles --work work --init                     # once: create db
python -m photoflow styles --work work --list --match examples     # what exists
# the user's own look, straight out of their darktable library:
python -m photoflow styles --work work --from-library --match "Wedding 2026"
# or from an exported file:
python -m photoflow styles --work work --import ~/looks/wedding.dtstyle --rename wedding
# a shipped demo look (no import needed):
python -m photoflow styles --work work --set faded
python -m photoflow styles --work work --clear
```

The choice is stored in `work/settings.json`; `masters`, `proofs` and `render`
follow it with no extra flags. Changing it invalidates worker configdirs and
cached masters automatically (each master records its style in a `.meta.json`).

Three traps, all verified:

* **`--apply-custom-presets 0` means no database at all.** darktable then never
  creates `data.db`, so no styles exist and `--style` cannot resolve. The
  pipeline defaults this flag to on. Never "optimise" it back to 0.
* **darktable renames instead of overwriting.** An existing output becomes
  `name-1.jpg` and exit code stays 0, so a stale master can masquerade as a
  fresh render. Targets are unlinked first; keep it that way.
* **db style names are localized**: `_l10n_darktable|_l10n_examples|_l10n_colors|_l10n_faded`.
  Use the leaf name (`faded`); resolution is exact → leaf → case-insensitive →
  substring, and ambiguity raises rather than guesses.

To create the look in the first place: darktable → darkroom → right panel
`styles` → *create*, with one image edited the way the wedding should look. It
lands in `~/.config/darktable/data.db`, which `--from-library` reads read-only.

### Camera-JPEG matching (used on this shoot)

When the brief is "match the camera", do not start from filmic. darktable ships
`darktable-curve-tool` (`/usr/lib/darktable/tools/`) which fits a basecurve from
RAW+JPEG pairs. The working recipe for this A7 II shoot:

1. Copy RAW+JPEG pairs into the workspace (`dcraw_emu -6 -W -g 1 1 -w` writes
   its PPM next to the input — never run it on the shoot).
2. Accumulate 15-25 diverse pairs with `darktable-curve-tool -s state.dat raw.ppm
   jpeg.ppm`, then fit with `-z -n 16`. Build the 520-byte op_version-6 blob, not
   the tool's stale 504-byte op_version-2 output.
3. Refit **in darktable space**: render with an identity basecurve, pair against
   the camera JPEG, match tone quantiles (per-pixel pairing flattens contrast
   because the export is 6022 px vs the JPEG's 6000 px).
4. Add only `basecurve` + a gentle `colorequal`; leave filmic/sigmoid/exposure/
   denoise disabled. Warmth comes from orange/yellow/green hue shifts in
   `colorequal`, not from a global white-balance lift.

`pf-cam6w-slight` is the result; the state file, fit data and method notes are in
`work/camfit/`. Recipes were neutralised (`exposure_ev=contrast=saturation=0`,
`sharpening=0.45`) for the camera match. It clips where the camera clips — compare
against the camera JPEG's own clip+ before calling that a defect.

## 6. Hard rules

* Do not edit anything in the shoot directory.
* Do not hand-write darktable XMP `op_params` — they are gzip/base64 binary
  structs. Decisions belong in `work/recipes/*.json`.
* Do not send the model a full-resolution image when a proof will do.
* Do not upscale a thumbnail into a proof.
* Report measured numbers, not impressions. If a stage fails, read the failure
  table above before changing code.
