# photoflow

An agent-operable photo pipeline: **RAW → select → crop/adjust**, where the
reviewing model only ever looks at small proofs.

Nothing here touches your originals. Everything is written into a workspace
directory and every creative decision is a JSON file you can read, diff, and
re-run.

```
python -m photoflow <command> ...
python -m photoflow.selftest --e2e      # 39 tests, 7 of them real renders
```

---

## 1. The cost strategy (why proofs are small)

Reviewing 100 frames at full size is the expensive mistake. The pipeline is
tiered so each photo is seen at the cheapest resolution that can answer the
question being asked:

| tier | artefact | size | who looks at it | when |
|---|---|---|---|---|
| 0 | contact sheet | 25 tiles × ~360 px | model | triage: which frames are worth a closer look |
| 1 | proof | ~800 px long edge | model | per-frame verdict, crop, straighten, exposure |
| 2 | master | full res | nobody | only after the proof is approved |

Measured with `python -m photoflow budget 100`:

```
tier0_sheets          4        ~28k tokens
tier1_proofs         25        ~44k tokens
total                        ~72k tokens
naive: 100 × 800 px         ~174k tokens
saving                       59%
```

**The claim that makes this sound:** a crop decided on an 800 px proof is
*the same crop* on the 4024x6022 master, to the pixel. That is not an
approximation — see [the invariant](#the-invariant) and the tests that enforce
it (`geometry.proof_norm_equivalence`, `render.proof_approves_master`).
Measured on a real ARW: a 4:5 crop read off an 800 px proof rendered as
`320x400` proof and `3009x3764` master, mean pixel difference `0.0084`.

---

## 2. Pipeline

```bash
# 0. what's on the card (EXIF only, no pixels decoded)
python -m photoflow inventory --photos /path/to/shoot --work work --print --bursts

# 1. develop full-res masters ONCE (darktable); the renderer crops these,
#    never the RAW (see "Masters" below)
python -m photoflow masters --work work --jobs 4 --format jpg

# 2. tier-1 proofs + exact scale record   -> work/proofs/, work/proofs/index.json
python -m photoflow proofs --work work --from-masters --long-edge 800

# 3. numbers to travel with the images    -> work/metrics/metrics.json
python -m photoflow measure --work work --print

# 4. tier-0 contact sheets                -> work/sheets/
python -m photoflow sheets --work work --per-sheet 25 --tile 360 --cols 6

# 5. review bundles for the model         -> work/review/request_00N.{json,prompt.md}
python -m photoflow review --work work --per-batch 10 --deliverable social_4x5

# 6. fold the model's answers into recipes -> work/recipes/<id>.json
python -m photoflow review --work work --ingest work/review/response_001.json

# 7. render proofs, then masters          -> work/out/proof/, work/out/full/
python -m photoflow render --work work --edge 400
python -m photoflow render --work work --full

# 8. stamp capture date/camera/lens onto the delivered JPEGs
#    (add --gps "LAT,LON" to set the location; it is remembered in settings.json)
python -m photoflow exif --work work --verify
```

`render` strips metadata so a render is a pure function of the recipe; `exif`
puts back the value tags an album needs (`DateTimeOriginal`, make/model, lens,
exposure, ISO, focal length, GPS) and overrides orientation and pixel
dimensions to the rendered file. Offset pointers and maker notes are never
copied.

`proofs --mode embedded` uses the camera's own JPEG preview instead of running
darktable: much faster, ungraded, fine for culling. It **refuses** to build a
proof from a too-small preview and falls back to darktable instead — a real NEF
on this machine exposes only a 160x112 EXIF thumbnail, and upscaling that to
800 px would hand the model a blurry fake.

### Masters (why render never touches the RAW)

Two reasons, both learned the hard way:

1. ImageMagick cannot read every RAW. This build's libraw/vips/IM all reject a
   real NEF that darktable's rawspeed reads fine.
2. Cropping the RAW directly would bypass darktable, so the master would not
   have the same look as the proof the model approved.

So darktable develops `work/masters/<id>.jpg` (8-bit, for review) or `.tif`
(16-bit, for final delivery) once, and every render crops that. Verified on a
real NEF: a 4:5 crop off a 800 px proof produced `320x400` (ar 0.8000) as a
proof and `2614x3267` (ar 0.8001) as a master — an 8.17x scale, same framing.

---

## 3. The review loop

The model is asked for **JSON only**, against a fixed schema, with either
fractions of the frame (`"space":"norm"`) or pixels of the proof it was shown
(`"space":"proof"`). Anything else is rejected — never interpreted.

```json
{"items": [
  {"id": "IMG_501.CR2", "verdict": "keep", "score": 72, "confidence": 0.8,
   "crop": {"space": "proof", "x": 80, "y": 0, "w": 440, "h": 533},
   "aspect": "4:5",
   "rotate_deg": 0.0,
   "adjust": {"exposure_ev": 0.2, "contrast": 0.12,
              "saturation": 0.06, "sharpening": 0.35},
   "issues": ["highlight on dress near window"],
   "rationale": "tightened onto the couple"}
]}
```

Three things keep this from going wrong unattended:

1. **Validation before execution.** `photoflow.recipe` rejects out-of-range
   values (`exposure_ev: 40`), unknown keys, sliver crops, and crops that
   escape the frame. Problems are reported all at once, not one per round trip.
2. **Aspect self-repair.** If the model declares `"aspect": "4:5"` but draws a
   0.826 rect, the recipe grows it to 0.800 around the same centre and records
   a warning instead of failing.
3. **Numbers next to pixels.** Every proof travels with
   `p1 p50 p99 clip+ clip- sharp exp_sugg flags` — a line of text that costs
   ~30 tokens and makes the verdict far better than the image alone.

### Closing the loop properly

Every adjustment the renderer implements is measurable, so the agent can
verify its own work instead of guessing:

```
render proof  ->  measure  ->  compare to target  ->  adjust recipe  ->  re-render
```

The target is a number, not a feeling: `p50 → 0.18 linear`, `clip+ < 1%`,
`p99 < 0.98`. `measure.suggest_exposure_ev` is the initial guess.

---

## 4. The invariant

Three coordinate frames, and exactly one rule:

```
source  full-resolution pixels
proof   the downscaled copy the model sees
norm    floats in [0,1] of the source frame    <-- the ONLY thing that travels
```

A recipe never contains a pixel count that depends on resolution. So:

* proof and master are rendered by the same code path from the same recipe;
* a stale 800 px proof can never silently disagree with the master;
* the model can be swapped, the proof size changed, or the master re-exported
  at a different resolution without invalidating a single decision.

Proof scaling keeps `sx` and `sy` separate because a proof is rounded to whole
pixels; they differ by less than one pixel and are never forced equal.

**Frames are recorded in display orientation, never stored orientation.** Real
ARW files are stored rotated (6000x4000 with `Orientation=6`) and darktable
exports them the right way up (4024x6022). Recording the EXIF dimensions made
`sx` and `sy` disagree by 2x and silently corrupted every aspect calculation.
So `display_dims()` swaps the axes for orientation 5-8, and if EXIF and the
rendered artefact still disagree, **the artefact wins** — the pixels are the
truth, the tag is a claim.

---

## 5. What runs where

| stage | tool | why |
|---|---|---|
| demosaic, lens/CA correction, white balance, base tone | `darktable-cli` | the RAW science; deterministic per camera |
| full-res master | `darktable-cli` | the only thing that reads every RAW; also what keeps proof and master identical in look |
| proof + master rendering | ImageMagick | predictable 16-bit handling, inspectable argv |
| fast thumbnails | libvips | much faster than magick for previews |
| EXIF inventory, embedded previews | exiv2 | reads metadata without decoding the RAW |
| selection metrics | numpy + Pillow | measurable, no model needed |
| verdict / crop / look | the reviewing model | judgement, cheaply |

Rotation, crop, resize, exposure, contrast, saturation, vignette and sharpening
are applied locally. Sharpening is an unsharp mask with a 5% threshold so it
boosts edges, not sensor grain. White balance, denoise, tone curves and highlight
recovery are
**refused by the local renderer** (`render.UnsupportedOp`) rather than faked —
they belong in darktable, and silently approximating them would break the
"recipes are reproducible" guarantee.

---

## 6. Sony (ARW) — measured on real files

The wedding shoot is on this same body: **Sony ILCE-7M2 (A7 II)**, shot
outdoors (so expect better ISO than the indoor ISO 12800 files below).

Verified against 2,009 real `.ARW` files in `~/Documents/My Pictures`, shot on
an **ILCE-7M2 (A7 II)**, 6000x4000, ISO 12800, FE 50mm F1.8.

| what | measured |
|---|---|
| stored size | 6000x4000, `Orientation = right, top` (6) |
| darktable export | **4024x6022** (rotated, tag normalised to 1) |
| file size | 24.8 MB → **1.035 bytes/px** → `compressed_likely` |
| previews | 160x120 thumb **and 1616x1080 JPEG, with no orientation tag** |
| lens | `Sony2.LensSpec = FE 50mm F1.8`, `Sony2.LensID = Manual lens` |
| `SonyRawFileType` | **absent** on this body |
| libraw (`dcraw_emu`, `magick`, `vips`) | reads these ARWs fine |

What that means in practice:

* **The A7 II's compressed RAW is lossy 11-bit** (24.8 MB / 24 MP), so
  `SHADOW_PUSH_CAREFUL` fires correctly and the review prompt caps
  `|exposure_ev| <= 0.7` and prefers crop/contrast over shadow recovery. An
  uncompressed 14-bit frame from the same body lands near 1.9 bytes/px and gets
  `ambiguous` instead.
* **Embedded-preview mode works and is ~12x faster**: 0.76 s for 9 frames vs
  9.3 s to develop them (0.08 s vs 1.03 s per frame). Good for culling. Its
  recorded frame can differ from the master by <1% (4000x6000 vs 4024x6022),
  which does not move a crop, but re-run the review-quality tier before final
  delivery so the recipes are derived from the graded images.
* **`--style` import is not implemented** (the user's look is not applied yet).
  darktable rejects a `.dtstyle` path; the style must be imported into the
  isolated configdir's database, then passed as `--style NAME` — a **CLI**
  option, so it belongs before `--core`. Options: one-off GUI import into
  `dtcfg/wNN`, or writing the `styles` blob with sqlite3. Until then every proof
  is darktable's default rendering.
* **The preview is landscape while the frame is portrait**, and its orientation
  tag is stripped, so `match_display_orientation` rotates it using the parent
  RAW's tag. Without that, every crop would land in a sideways frame.
* `SonyRawFileType` is not written by this body, so the bytes/px hint cannot be
  upgraded to a fact here; `pacman -S perl-image-exiftool` remains the cleanest
  per-file confirmation.
* Lens *specification* survives even when `LensModel`/`LensID` are unusable, so
  the inventory shows `FE 50mm F1.8` rather than a blank.

A real NEF on the same machine was rejected by libraw, `magick` and `vips` while
darktable read it fine — so "libraw reads ARW" is a fact about this shoot, not a
guarantee. The masters stage exists for that reason, and because cropping the
RAW would bypass darktable's look entirely.

---

## 7. Styles — putting *your* look on every frame

A style is not a LUT or a master file. It is a saved **module stack**: which
processing modules are active, their parameter structs, their blend settings and
their order. `darktable-cli --style NAME` does **not** accept a path — it looks
the name up in the `styles` table of the configdir's `data.db`.

So a custom style has to be inside that database. `photoflow.style` does that,
and three things had to be discovered the hard way:

### 7.1 `--apply-custom-presets 0` silently disables styles entirely

The biggest one. With that flag darktable never initialises `data.db` at all — a
fresh configdir ends up containing only `darktablerc`. The pipeline had been
running with **no database**, so `--style` could never have resolved anything.
Verified:

```
--width 100                        -> data.db created
--width 100 --apply-custom-presets 0 -> no data.db
```

The flag now defaults to **on**; concurrency is handled by per-worker configdirs
instead, which is what the flag was really for.

### 7.2 darktable does not overwrite, it renames

Given an existing output path, `darktable-cli` writes `name-1.jpg` and exits 0.
The old code checked "did the destination appear?" and happily accepted the
**stale** file, so a re-render could be a silent no-op. Targets are now deleted
before the run, and a master records the style that produced it
(`masters/<id>.jpg.meta.json`) so a style switch invalidates cached masters.

### 7.3 db names are localized

`examples_colors_faded` fails with "cannot find the style". The real name is
`_l10n_darktable|_l10n_examples|_l10n_colors|_l10n_faded`. Resolution order is
exact → **leaf** (`_l10n_` stripped from the last `|` component) → case-insensitive
→ substring, and ambiguity is an error rather than a guess.

### 7.4 The param encoding

| where | encoding |
|---|---|
| `.dtstyle`, short params | plain lowercase hex |
| `.dtstyle`, long params | `"gz"` + 2 unknown hex chars + base64(**zlib**) |
| `data.db` | raw uncompressed bytes (BLOB) |

Confirmed byte-for-byte: dropping 4 chars of `gz03eJyb...` and inflating
reproduced the 132-byte blob exactly. `import` decodes and inserts; the 2 hex
chars are ignored (their meaning is not the compressed or uncompressed length).

### 7.5 Workflow

```bash
# once per workspace
python -m photoflow styles --work work --init                    # create the db
python -m photoflow styles --work work --list --match examples    # what's available

# a style you made in darktable's GUI (darkroom > styles panel > create)
python -m photoflow styles --work work --from-library --match "Wedding 2026"

# or from an exported .dtstyle file
python -m photoflow styles --work work --import ~/looks/wedding.dtstyle --rename wedding

# built-in looks need no import at all
python -m photoflow styles --work work --set faded
python -m photoflow styles --work work --clear        # back to darktable defaults
```

Selecting a style writes `work/settings.json`, so `masters`/`proofs`/`render`
all follow it without further flags. Changing it drops the worker configdirs and
invalidates cached masters automatically.

### 7.7 Camera-JPEG matching (this shoot)

`darktable-curve-tool` fits a basecurve from RAW+JPEG pairs. On the A7 II shoot
the first fit (dcraw/PPM space) was close but the darktable pipeline saw a
slightly different input scale, so the final curve was fitted in darktable space
by matching tone quantiles. The style is `pf-cam6w-slight`: basecurve + a small
`colorequal` warm shift, with filmic/sigmoid/exposure/denoise disabled.

Measured on 8 frames, it tracks the camera within ~0.002 warmth and ~0.003
saturation, and clips less than the camera on every bright frame. The 25 recipes
are neutral in colour (`contrast=saturation=0`, `sharpening=0.45`); seven carry a
+0.10 to +0.30 EV high-key lift measured against the Mark Davis reference set,
three are trimmed below 0 (DSC02655, DSC02660, DSC02675) to hold the camera JPEG's
highlight level, and six portraits also have a deliberately subtle `vignette=0.10`. State, fit data
and the full method are in `work/camfit/README.md`.

### 7.6 Verified

Round-trip on a real ARW, comparing rendered masters:

```
imported .dtstyle vs darktable's built-in copy   mean|diff| = 0.000000   (identical)
from-library import vs direct --style            mean|diff| = 0.000000
styled vs unstyled                               mean|diff| = 0.090403
style switch: sepia mean=0.3838 -> faded mean=0.5023
```

### 7.7 What is *not* done

No style has been created for this shoot yet — the user's library currently
contains **0 styles**. The 510 shipped `darktable_<Camera>` styles are
JPEG-matching basecurves, and the 24 `examples_*` are demo looks; neither is a
wedding grade.

---

## 8. Verified environment notes

Checked on this machine (Arch, darktable 5.6.2, ImageMagick 7.1.2-33 Q16-HDRI,
libvips 8.18, exiv2 0.28.9, exiftool *not* installed). Every item below was hit
while building this, not copied from documentation:

* Your global `~/.config/darktable/data.db` is **out of date**, so
  `darktable-cli` aborts (`database ... is out-of-date. aborting`). Every call
  here uses an isolated `--configdir`, which also makes renders reproducible.
* **`darktable-cli` has a two-region grammar:**
  `darktable-cli [CLI OPTIONS + POSITIONALS] --core [CORE OPTIONS]`.
  `--width`, `--height`, `--apply-custom-presets` are CLI options and must come
  *before* `--core`; `--configdir` and `--conf` are core options and must come
  *after*. A misplaced `--width` makes darktable print its usage and exit 1 with
  no error message. `develop.darktable_argv` is pure and unit-tested for this.
* Two concurrent runs sharing a configdir die with `database is locked` —
  so `--jobs N` allocates one configdir per worker.
* `--style NAME` requires the style **imported into the database**; passing a
  `.dtstyle` path is rejected.
* 8-bit vs 16-bit TIFF export is `--conf plugins/imageio/format/tiff/bpp=16`
  (verified: produced 16-bit sRGB TIFF).
* Warm 800 px export is ~1 s/frame (3 real NEFs through 3 workers: 2.9 s total,
  including process start-up); a full 4894x3267 develop is ~2.7 s.
* This build's **libraw cannot open every RAW**: a real NEF that darktable reads
  fine is rejected by `dcraw_emu`, `magick`, *and* `vips` ("Unsupported file
  format or not RAW file"), while real A7 II ARWs read fine. Hence the masters
  stage.
* **Orientation is a trap.** A real ARW stores 6000x4000 with Orientation=6 and
  darktable exports 4024x6022. Recording the EXIF dimensions as the frame made
  `sx`/`sy` disagree by 2x and would corrupt every aspect calculation. Frames
  are now recorded in *display* orientation (`display_dims`), and the rendered
  artefact is the final tiebreaker when EXIF disagrees.
* **The embedded JPEG preview of that ARW has no orientation tag** even though
  the parent RAW does, so extracted previews must be rotated using the parent's
  tag.
* `exiv2 -pp` may list only a 160x112 EXIF thumbnail even when a full JPEG
  preview is expected — never trust it to be large (the NEF case), and never
  upscale it into a proof.
* `dcraw_emu -v FILE` **writes** `FILE.ppm` next to the input. Never run it on a
  shoot directory; it polluted a photo folder during testing.
* ImageMagick's `-level` with inverted or negative bounds does **not** mean
  "reduce contrast" (it produced a wrong result in testing); contrast uses
  `-sigmoidal-contrast`, which is monotonic in both directions and cannot clip.
* Exposure must be applied in linear light (`-colorspace RGB -evaluate multiply`
  → `-colorspace sRGB`); doing it on gamma-encoded pixels is visibly wrong.

---

## 9. Deliberately not built yet

* **No shoot-specific style exists yet.** The machinery is verified
  (section 7.6) but the user's library has 0 styles, so every export currently
  uses darktable's defaults.
* **No full shoot has been run.** 9 real ARWs went through end-to-end in
  development, but not 100, and not with the aspect you'll actually deliver.
* **Face-aware crops.** `opencv-python-headless` (Haar or YuNet) would let the
  renderer place crops on eyes instead of nudging rectangles.
* **Burst de-duplication** is timestamp grouping only; perceptual hashing
  (dHash + sharpest-of-group) is the missing second half.
* **Semantic tagging** (find "first dance") would need CLIP + onnxruntime.
* **`--parallel` render.** Rendering is serial; it is fast enough for 100 frames
  at proof size, but a full-res 100-frame delivery pass will want a pool.

---

## 10. Layout

```
photoflow/
  geometry.py   resolution-independent crop/scale math + the invariant
  recipe.py     the JSON contract, validation, aspect self-repair
  measure.py    sharpness / clipping / percentiles / exposure suggestion
  render.py     recipe -> ImageMagick argv (printable, testable)
  review.py     request bundles, strict response parsing, token budget
  previews.py   proof generation and scale records
  develop.py    darktable-cli grammar, masters, embedded previews, rasters
  sheets.py     labelled contact sheets + cell maps
  manifest.py   EXIF inventory, burst grouping, raw-storage hints
  exifmeta.py   capture metadata (date/camera/lens/GPS) onto rendered JPEGs
  cli.py        the commands in section 2
  selftest.py   39 tests, incl. proof-vs-master equivalence

work/
  manifest.json  masters/  proofs/  sheets/  metrics/  recipes/  review/
  out/proof/  out/full/  dtcfg/w00..wNN
```
