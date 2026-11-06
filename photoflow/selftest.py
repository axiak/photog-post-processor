"""Self-tests. Run this before trusting any of the above.

    python -m photoflow.selftest          # geometry + protocol, no tools needed
    python -m photoflow.selftest --e2e    # also renders with ImageMagick

The tests that matter most are the resolution-equivalence ones: they are the
claim that "a crop approved on an 800 px proof is exactly the crop on the
45 MP master". If those fail, the whole cost strategy is unsound.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Callable

import numpy as np

from . import exifmeta, geometry, manifest, measure, recipe as recipe_mod, review, render, util
from . import develop
from .geometry import Rect
from .recipe import Adjust, Geometry, Recipe, RecipeError

PASS, FAIL = "PASS", "FAIL"


# --------------------------------------------------------------------------
# geometry
# --------------------------------------------------------------------------
def test_parse_aspect() -> None:
    assert geometry.parse_aspect("4:5") == 0.8
    assert geometry.parse_aspect("3x2") == 1.5
    assert geometry.parse_aspect(1.0) == 1.0
    assert geometry.parse_aspect(None) is None
    assert geometry.parse_aspect("free") is None
    try:
        geometry.parse_aspect("0:5")
    except ValueError:
        pass
    else:
        raise AssertionError("zero aspect ratio must raise")


def test_proof_dims() -> None:
    pw, ph, scale = geometry.proof_dims(6240, 4160, 800)
    assert (pw, ph) == (800, 533), (pw, ph)
    assert abs(scale.sx - 800 / 6240) < 1e-12
    assert abs(pw / ph - 6240 / 4160) < 0.01
    # portrait
    pw, ph, _ = geometry.proof_dims(4000, 6000, 800)
    assert (pw, ph) == (533, 800), (pw, ph)


def test_roundtrip_norm_px() -> None:
    rng = np.random.default_rng(7)
    for _ in range(200):
        w, h = int(rng.integers(100, 9000)), int(rng.integers(100, 9000))
        rect = Rect(*[float(v) for v in (rng.random() * 0.5, rng.random() * 0.5,
                                         rng.random() * 0.5 + 0.3,
                                         rng.random() * 0.5 + 0.3)])
        rect = geometry.clamp_into_frame(rect)
        px = geometry.norm_to_px(rect, w, h)
        back = geometry.px_to_norm(*px, w, h)
        tol = 1.5 / min(w, h)
        assert abs(back.x - rect.x) < tol, (back, rect, w, h)
        assert abs(back.w - rect.w) < tol, (back, rect, w, h)
        assert px[0] >= 0 and px[1] >= 0
        assert px[0] + px[2] <= w and px[1] + px[3] <= h


def test_proof_norm_equivalence() -> None:
    """A rect measured in proof pixels must equal the same fraction of the master."""
    rng = np.random.default_rng(11)
    for _ in range(200):
        fw, fh = int(rng.integers(1000, 9000)), int(rng.integers(1000, 9000))
        pw, ph, _ = geometry.proof_dims(fw, fh, int(rng.integers(320, 1024)))
        rect_px = Rect(*(float(v) for v in (
            rng.integers(0, pw // 2), rng.integers(0, ph // 2),
            max(2, rng.integers(pw // 8, pw // 2)),
            max(2, rng.integers(ph // 8, ph // 2)))))
        norm = geometry.proof_to_norm(rect_px, pw, ph)
        master_px = geometry.norm_to_px(norm, fw, fh)
        # the same framing, expressed on the master
        scale_x, scale_y = fw / pw, fh / ph
        expected = (rect_px.x * scale_x, rect_px.y * scale_y,
                    rect_px.w * scale_x, rect_px.h * scale_y)
        for got, want in zip(master_px, expected):
            assert abs(got - want) <= 2, (master_px, expected)


def test_fit_aspect() -> None:
    rect = Rect(0.2, 0.2, 0.3, 0.5)
    out = geometry.fit_aspect(rect, 6000, 4000, "4:5")
    assert abs(out.aspect(6000, 4000) - 0.8) < 0.01, out.aspect(6000, 4000)
    assert geometry.is_sane(out)
    # centre is preserved when there is room
    cx0 = rect.x + rect.w / 2
    cx1 = out.x + out.w / 2
    assert abs(cx0 - cx1) < 0.01
    # a rect that would escape is pulled back in
    out2 = geometry.fit_aspect(Rect(0.9, 0.9, 0.3, 0.3), 6000, 4000, "1:1")
    assert geometry.is_sane(out2), out2


def test_clamp() -> None:
    for rect in (Rect(-0.2, -0.1, 0.4, 0.4), Rect(0.9, 0.95, 0.5, 0.5),
                 Rect(0.0, 0.0, 2.0, 2.0)):
        out = geometry.clamp_into_frame(rect)
        assert geometry.is_sane(out), (rect, out)


def test_rect_rejects_garbage() -> None:
    for bad in (float("nan"), float("inf")):
        try:
            Rect(bad, 0, 1, 1)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Rect accepted {bad}")
    try:
        Rect(0, 0, 0, 1)
    except ValueError:
        pass
    else:
        raise AssertionError("Rect accepted zero width")


# --------------------------------------------------------------------------
# recipe validation
# --------------------------------------------------------------------------
def test_recipe_roundtrip() -> None:
    rec = Recipe.new("IMG_0001.CR2", 6240, 4160)
    rec.geometry = Geometry(rotate_deg=-1.3, crop=Rect(0.06, 0.0, 0.55, 0.92), aspect="4:5")
    rec.adjust = Adjust(exposure_ev=0.35, contrast=0.2, saturation=0.1,
                        sharpening=0.4, vignette=0.12)
    rec.validate()
    again = Recipe.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert again.crop_signature() == rec.crop_signature()


def test_recipe_rejects_out_of_range() -> None:
    bad = {
        "schema": "photoflow.recipe/1", "image": "x.CR2", "source": {"w": 6000, "h": 4000},
        "geometry": {"crop": {"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.5}},
        "adjust": {"exposure_ev": 40},
    }
    try:
        Recipe.from_dict(bad)
    except RecipeError as exc:
        assert "exposure_ev" in str(exc), exc
    else:
        raise AssertionError("exposure_ev=40 must be rejected")


def test_recipe_rejects_unknown_key() -> None:
    bad = {
        "schema": "photoflow.recipe/1", "image": "x.CR2", "source": {"w": 6000, "h": 4000},
        "geometry": {"crop": {"x": 0, "y": 0, "w": 0.5, "h": 0.5}},
        "adjust": {"exposure_ev": 0.1, "vibrance": 9},
    }
    try:
        Recipe.from_dict(bad)
    except RecipeError as exc:
        assert "vibrance" in str(exc), exc
    else:
        raise AssertionError("unknown adjust key must be rejected")


def test_recipe_rejects_sliver_crop() -> None:
    rec = Recipe.new("x.CR2", 6000, 4000)
    rec.geometry.crop = Rect(0.5, 0.5, 0.001, 0.5)
    try:
        rec.validate()
    except RecipeError as exc:
        assert "too small" in str(exc), exc
    else:
        raise AssertionError("sliver crop must be rejected")


def test_recipe_fixes_aspect_mismatch() -> None:
    rec = Recipe.new("x.CR2", 6000, 4000)
    rec.geometry = Geometry(crop=Rect(0.1, 0.1, 0.8, 0.8), aspect="4:5")
    rec.validate()
    assert rec.warnings, "aspect mismatch should warn"
    assert abs(rec.geometry.crop.aspect(6000, 4000) - 0.8) < 0.01


# --------------------------------------------------------------------------
# review protocol
# --------------------------------------------------------------------------
def test_parse_response_fenced() -> None:
    text = '```json\n{"items":[{"id":"a.jpg","verdict":"keep"}]}\n```'
    out = review.parse_response(text)
    assert out["items"][0]["id"] == "a.jpg"


def test_parse_response_rejects_bad_verdict() -> None:
    try:
        review.parse_response('{"items":[{"id":"a.jpg","verdict":"sure"}]}')
    except RecipeError as exc:
        assert "verdict" in str(exc)
    else:
        raise AssertionError("bad verdict must be rejected")


def test_parse_response_rejects_unknown_key() -> None:
    try:
        review.parse_response('{"items":[{"id":"a.jpg","verdict":"keep","vibe":"warm"}]}')
    except RecipeError as exc:
        assert "vibe" in str(exc)
    else:
        raise AssertionError("unknown item key must be rejected")


def test_parse_response_rejects_prose() -> None:
    try:
        review.parse_response("I think these are all lovely.")
    except RecipeError:
        pass
    else:
        raise AssertionError("prose must be rejected")


def test_to_recipe_proof_coordinates() -> None:
    entry = {
        "image": "a.jpg",
        "frame": {"w": 6000, "h": 4000},
        "proof_size": {"w": 800, "h": 533},
        "proof": "/tmp/a.jpg",
    }
    item = {"id": "a.jpg", "verdict": "keep",
            "crop": {"space": "proof", "x": 80, "y": 53, "w": 400, "h": 266},
            "adjust": {"exposure_ev": 0.2}}
    rec = review.to_recipe(entry, item)
    assert abs(rec.geometry.crop.x - 0.1) < 0.005, rec.geometry.crop
    assert abs(rec.geometry.crop.w - 0.5) < 0.005, rec.geometry.crop
    assert rec.adjust.exposure_ev == 0.2
    left, top, w, h = rec.crop_px()
    assert abs(w - 3000) <= 8 and abs(h - 2000) <= 8, (left, top, w, h)


def test_token_estimates() -> None:
    tok = review.estimate_image_tokens(800, 800)
    assert 700 < tok["patches_28"] < 900, tok
    plan = review.budget_plan(100)
    assert plan["tier0_sheets"] == 4
    assert plan["total_tokens"] < plan["naive_all_800px_tokens"]
    assert 0 < plan["saving_vs_naive"] < 1


# --------------------------------------------------------------------------
# darktable / raw handling (regression guards for bugs found on a real NEF)
# --------------------------------------------------------------------------
def test_darktable_argv_order() -> None:
    """CLI options must precede --core; core options must follow it."""
    argv = develop.darktable_argv("in.ARW", "out.jpg", xmp="in.xmp",
                                  configdir="/tmp/d", long_edge=800)
    core_at = argv.index("--core")
    for opt in ("--width", "--height", "--apply-custom-presets"):
        assert argv.index(opt) < core_at, f"{opt} is a CLI option, must precede --core"
    for opt in ("--configdir", "--conf"):
        assert argv.index(opt) > core_at, f"{opt} is a core option, must follow --core"

    src_at = argv.index("in.ARW")
    assert argv.index("in.xmp") == src_at + 1, "sidecar must be the 2nd positional"
    assert argv[src_at + 2] == "out.jpg"
    assert argv[argv.index("--width") + 1] == "800"


def test_darktable_argv_no_size() -> None:
    argv = develop.darktable_argv("in.ARW", "out.jpg", configdir="/tmp/d")
    assert "--width" not in argv and "--height" not in argv


def test_raw_hint() -> None:
    sony = {"w": 6000, "h": 4000, "is_raw": True,
            "make": "SONY", "camera": "ILCE-7M3"}
    big = manifest.raw_hint(dict(sony, bytes=62_000_000))       # ~2.58 B/px
    assert big["storage"] == "uncompressed_likely", big
    assert big["shadow_push_careful"] is False

    small = manifest.raw_hint(dict(sony, bytes=23_000_000))     # ~0.96 B/px
    assert small["storage"] == "compressed_likely", small
    assert small["shadow_push_careful"] is True
    assert any("shadow lifts" in n for n in small["notes"])
    assert any("LensModel" in n for n in small["notes"]), small["notes"]

    mid = manifest.raw_hint(dict(sony, bytes=38_000_000))       # ~1.58 B/px
    assert mid["storage"] == "ambiguous"

    # a file far too small for its pixel count must be called out, not
    # silently described as "compressed"
    tiny = manifest.raw_hint(dict(sony, bytes=1_300_000))
    assert tiny["storage"] == "implausibly_small", tiny
    assert any("too small" in n for n in tiny["notes"])

    tagged = manifest.raw_hint(dict(sony, bytes=23_000_000, sony_raw_file_type="4"))
    assert tagged["sony_raw_file_type"] == "4"


def test_raw_note_line() -> None:
    line = review._raw_note_line("a.ARW", {
        "bytes_per_pixel": 0.96, "storage": "compressed_likely",
        "shadow_push_careful": True})
    assert "SHADOW_PUSH_CAREFUL" in line
    assert "0.96" in line
    assert "no raw metadata" in review._raw_note_line("b.ARW", {})


# --------------------------------------------------------------------------
# darktable styles (encoding reverse-engineered from real .dtstyle files)
# --------------------------------------------------------------------------
def _dtstyle_xml(name: str, params_hex: str, params_gz: bytes | None = None) -> str:
    import base64
    import zlib

    gz = ""
    if params_gz is not None:
        encoded = base64.b64encode(zlib.compress(params_gz)).decode()
        gz = ("<plugin><num>2</num><module>6</module><operation>filmicrgb</operation>"
              f"<op_params>gz00{encoded}</op_params>"
              "<enabled>1</enabled><blendop_params></blendop_params>"
              "<blendop_version>13</blendop_version><multi_priority>0</multi_priority>"
              "<multi_name></multi_name>"
              "<multi_name_hand_edited>0</multi_name_hand_edited></plugin>")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<darktable_style version="1.0"><info>'
            f'<name>{name}</name><description>test look</description></info><style>'
            '<plugin><num>1</num><module>6</module><operation>exposure</operation>'
            f'<op_params>{params_hex}</op_params><enabled>1</enabled>'
            '<blendop_params></blendop_params><blendop_version>13</blendop_version>'
            '<multi_priority>0</multi_priority><multi_name></multi_name>'
            '<multi_name_hand_edited>0</multi_name_hand_edited></plugin>'
            f'{gz}</style></darktable_style>')


def test_decode_param() -> None:
    import base64
    import zlib

    from photoflow import styles as styles_mod

    # short params are plain hex
    assert styles_mod.decode_param("010000000000003f") == bytes.fromhex("010000000000003f")
    assert styles_mod.decode_param("") == b""
    # long params: "gz" + 2 unknown hex chars + base64(zlib(bytes))
    raw = bytes(range(132))
    encoded = "gz03" + base64.b64encode(zlib.compress(raw)).decode()
    assert styles_mod.decode_param(encoded) == raw
    try:
        styles_mod.decode_param("garbage-not-hex-zz")
    except styles_mod.StyleError:
        pass
    else:
        raise AssertionError("bad hex must raise StyleError")


def test_parse_dtstyle(tmp: Path) -> None:
    from photoflow import styles as styles_mod

    p = tmp / "look.dtstyle"
    p.write_text(_dtstyle_xml("my look", "010000000000003f", bytes(range(64))))
    parsed = styles_mod.parse_dtstyle(p)
    assert parsed["name"] == "my look"
    assert parsed["description"] == "test look"
    assert len(parsed["plugins"]) == 2
    ops = [x["operation"] for x in parsed["plugins"]]
    assert "exposure" in ops and "filmicrgb" in ops
    filmic = [x for x in parsed["plugins"] if x["operation"] == "filmicrgb"][0]
    assert filmic["op_params"] == bytes(range(64)), len(filmic["op_params"])


def test_import_and_find_style(tmp: Path) -> None:
    """Import into a real sqlite db and resolve names, without darktable."""
    import sqlite3

    from photoflow import styles as styles_mod

    cfg = tmp / "cfg"
    cfg.mkdir()
    con = sqlite3.connect(cfg / "data.db")
    con.execute("create table styles(id INTEGER PRIMARY KEY, name VARCHAR, "
                "description VARCHAR, iop_list VARCHAR)")
    con.execute("create table style_items(styleid INTEGER, num INTEGER, module INTEGER, "
                "operation VARCHAR(256), op_params BLOB, enabled INTEGER, "
                "blendop_params BLOB, blendop_version INTEGER, multi_priority INTEGER, "
                "multi_name VARCHAR(256), multi_name_hand_edited INTEGER)")
    con.execute("insert into styles(name, description, iop_list) values "
                "('_l10n_darktable|_l10n_examples|_l10n_colors|_l10n_faded','','')")
    con.commit()
    con.close()

    # suffix resolution, like darktable's localized names
    assert styles_mod.find_style(cfg, "faded").endswith("faded")

    p = tmp / "look.dtstyle"
    p.write_text(_dtstyle_xml("wedding 2026", "010000000000003f", bytes(range(64))))
    assert styles_mod.import_dtstyle(cfg, p) == "wedding 2026"
    assert styles_mod.find_style(cfg, "wedding 2026") == "wedding 2026"

    listed = {s["name"]: s["modules"] for s in styles_mod.list_styles(cfg)}
    assert listed["wedding 2026"] == 2, listed

    # re-import is idempotent (replaces, does not duplicate)
    styles_mod.import_dtstyle(cfg, p)
    assert len(styles_mod.list_styles(cfg, "wedding")) == 1

    try:
        styles_mod.find_style(cfg, "no-such-style")
    except styles_mod.StyleError:
        pass
    else:
        raise AssertionError("unknown style must raise")


def test_style_argv_placement() -> None:
    """--style is a CLI option: it must precede --core."""
    argv = develop.darktable_argv("in.ARW", "out.jpg", configdir="/tmp/d",
                                  style="wedding 2026")
    assert argv.index("--style") < argv.index("--core")
    assert argv[argv.index("--style") + 1] == "wedding 2026"
    assert "--style" not in develop.darktable_argv("i.ARW", "o.jpg", configdir="/tmp/d")


def test_apply_presets_must_be_on() -> None:
    """--apply-custom-presets 0 skips data.db creation, which kills styles.

    Verified against darktable 5.6.2: a fresh configdir exported with
    --apply-custom-presets 0 contains only darktablerc, no data.db at all - so
    no styles exist and --style cannot be resolved. The default must stay on.
    """
    argv = develop.darktable_argv("in.ARW", "out.jpg", configdir="/tmp/d")
    flag = argv[argv.index("--apply-custom-presets") + 1]
    assert flag == "1", f"default must be 1, got {flag}"
    # and it is a CLI option, so it belongs before --core
    assert argv.index("--apply-custom-presets") < argv.index("--core")


def test_style_name_resolution() -> None:
    from photoflow import styles as styles_mod

    names = ["_l10n_darktable|_l10n_examples|_l10n_colors|_l10n_sepia",
             "_l10n_darktable|_l10n_examples|_l10n_colors|_l10n_faded",
             "wedding 2026", "pf_test_faded"]
    # leaf names resolve even though the db name is localized
    assert styles_mod._resolve_name(names, "sepia").endswith("_l10n_sepia")
    assert styles_mod._resolve_name(names, "faded").endswith("_l10n_faded")
    assert styles_mod._resolve_name(names, "wedding 2026") == "wedding 2026"
    # a user style must not shadow the built-in through suffix matching
    assert styles_mod._resolve_name(names, "pf_test_faded") == "pf_test_faded"
    try:
        styles_mod._resolve_name(names, "nope")
    except styles_mod.StyleError:
        pass
    else:
        raise AssertionError("unknown style must raise")


def test_orientation_swaps_dims() -> None:
    """A real A7 II ARW stores 6000x4000 with Orientation=6.

    darktable exports it as 4024x6022, so the recorded frame must be portrait
    or every aspect calculation downstream is wrong.
    """
    upright = {"Exif.Image.Orientation": "top, left"}
    assert develop.orientation_code(upright) == 1
    assert develop.display_dims(upright, 6000, 4000) == (6000, 4000)

    rotated = {"Exif.Image.Orientation": "right, top"}          # 6
    assert develop.orientation_code(rotated) == 6
    assert develop.display_dims(rotated, 6000, 4000) == (4000, 6000)

    rotated_ccw = {"Exif.Image.Orientation": "left, top"}       # 5, also swaps
    assert develop.display_dims(rotated_ccw, 6000, 4000) == (4000, 6000)

    flipped = {"Exif.Image.Orientation": "top, right"}           # 2, mirror only
    assert develop.display_dims(flipped, 6000, 4000) == (6000, 4000)

    numeric = {"Exif.Image.Orientation": "6"}                    # some builds
    assert develop.display_dims(numeric, 6000, 4000) == (4000, 6000)

    assert develop.orientation_code({}) == 1


def test_exif_dms_roundtrip() -> None:
    """decimal degrees <-> DMS must be lossless to well under a metre."""
    for value in (41.933201, -72.6271707, 0.0, 179.9999999, -0.5):
        (d, _), (m, _), (sn, sd) = exifmeta.decimal_to_dms(value)
        back = d + m / 60 + (sn / sd) / 3600
        assert abs(back - abs(value)) < 1e-9, (value, back)


# --------------------------------------------------------------------------
# end-to-end (needs ImageMagick)
# --------------------------------------------------------------------------
def _make_master(path: Path, w: int = 1600, h: int = 1067) -> Path:
    util.require("magick")
    util.run([
        "magick", "-size", f"{w}x{h}", "gradient:#2b3f8f-#f0c9a0",
        "-fill", "#d8d0c5", "-draw", f"rectangle {int(w*0.15)},{int(h*0.45)} {int(w*0.42)},{int(h*0.9)}",
        "-fill", "#3a2a20", "-draw", f"circle {int(w*0.62)},{int(h*0.4)} {int(w*0.70)},{int(h*0.4)}",
        "-attenuate", "0.35", "+noise", "Gaussian",
        "-quality", "95", str(path),
    ])
    return path


def test_render_equivalence(tmp: Path) -> None:
    """Same recipe at proof size and master size must frame identically."""
    master = _make_master(tmp / "master.jpg")
    rec = Recipe.new("master.jpg", 1600, 1067)
    rec.geometry = Geometry(rotate_deg=0.8, crop=Rect(0.10, 0.10, 0.50, 0.60))
    rec.adjust = Adjust(exposure_ev=0.30, contrast=0.20, saturation=0.10, sharpening=0.30)
    rec.validate()

    full = render.render(master, rec, tmp / "full.jpg")
    small = render.render(master, rec, tmp / "small.jpg", long_edge=320)

    fw, fh = render.image_size(full)
    sw, sh = render.image_size(small)
    assert abs(fw - round(0.50 * 1600)) <= 1, (fw,)
    assert abs(fh - round(0.60 * 1067)) <= 1, (fh,)
    assert abs(sw - 320) <= 1 and abs(sh - round(320 * fh / fw)) <= 2, (sw, sh)

    # content must agree once normalised to the same size
    a = measure._load_rgb01(full, 320)
    b = measure._load_rgb01(small, 320)
    diff = float(np.abs(a - b).mean())
    assert diff < 0.06, f"proof/master mismatch {diff:.4f}"


def test_render_sharpening_ignores_noise(tmp: Path) -> None:
    """Sharpening must not amplify grain below the threshold.

    A flat grey field carrying low-amplitude Gaussian noise stands in for an
    ISO-1250 shadow.  With threshold 0 the unsharp mask roughly doubles the
    high-pass energy; with the threshold it must stay close to the source.
    """
    from PIL import Image, ImageFilter

    util.require("magick")
    src = tmp / "noisy.jpg"
    util.run(["magick", "-size", "600x400", "xc:#808080",
              "-attenuate", "0.1", "+noise", "Gaussian",
              "-quality", "95", str(src)])
    rec = Recipe.new("noisy.jpg", 600, 400)
    rec.adjust = Adjust(sharpening=1.0)
    rec.validate()
    out = render.render(src, rec, tmp / "noisy_sharp.jpg", quality=95)

    def highpass(path: Path) -> float:
        with Image.open(path) as im:
            grey = im.convert("L")
            a = np.asarray(grey, dtype=np.float64) / 255.0
            b = np.asarray(grey.filter(ImageFilter.GaussianBlur(1.2)),
                           dtype=np.float64) / 255.0
        return float((a - b).std())

    before, after = highpass(src), highpass(out)
    assert after < before * 1.25, f"sharpening amplified noise {before:.5f} -> {after:.5f}"


def test_exif_stamp(tmp: Path) -> None:
    """Capture metadata must survive onto the render, geometry must be ours."""
    util.require("magick")
    util.require("exiv2")
    src = tmp / "exif_src.jpg"
    util.run(["magick", "-size", "320x240", "xc:#777777", "-quality", "95", str(src)])
    util.run(["exiv2",
              "-M", "set Exif.Image.Make Ascii TESTCAM",
              "-M", "set Exif.Image.Model Ascii TESTBODY",
              "-M", "set Exif.Image.Orientation Short 6",
              "-M", "set Exif.Photo.DateTimeOriginal Ascii 2024:05:06 07:08:09",
              "-M", "set Exif.Photo.ISOSpeedRatings Short 800",
              str(src)])

    dst = tmp / "exif_dst.jpg"
    util.run(["magick", "-size", "120x90", "xc:#999999", "-quality", "95", str(dst)])
    width, height = render.image_size(dst)
    exifmeta.stamp(dst, src)

    tags = exifmeta.verify(dst)
    assert tags["Exif.Photo.DateTimeOriginal"] == "2024:05:06 07:08:09", tags
    assert tags["Exif.Image.Make"] == "TESTCAM", tags
    assert tags.get("Exif.Photo.ISOSpeedRatings") == "800", tags
    assert tags["Exif.Image.Orientation"] == "top, left", tags          # forced to 1
    assert tags["Exif.Photo.PixelXDimension"] == str(width), tags
    assert tags["Exif.Photo.PixelYDimension"] == str(height), tags

    # offset pointers from the source must never be copied
    listing = util.run(["exiv2", "-pa", str(dst)], check=False)
    for bad in ("JPEGInterchangeFormat", "SubIFDs", "InteroperabilityTag"):
        assert bad not in listing, f"stale pointer tag copied: {bad}"

    # GPS is additive: setting it must not disturb the tags just copied
    exifmeta.set_gps(dst, 41.933201, -72.6271707)
    tags = exifmeta.verify(dst)
    assert tags["Exif.GPSInfo.GPSLatitude"].startswith("41 deg 55' 59"), tags
    assert tags["Exif.GPSInfo.GPSLongitude"].startswith("72 deg 37' 37"), tags
    assert tags["Exif.Photo.DateTimeOriginal"] == "2024:05:06 07:08:09", tags
    assert tags["Exif.Image.Make"] == "TESTCAM", tags


def test_render_vignette(tmp: Path) -> None:
    """A vignette must keep the centre and darken the corners, without a hard edge."""
    util.require("magick")
    src = tmp / "flat.jpg"
    util.run(["magick", "-size", "600x400", "xc:#808080", "-quality", "95", str(src)])
    rec = Recipe.new("flat.jpg", 600, 400)
    rec.adjust = Adjust(vignette=0.20)
    rec.validate()

    out = render.render(src, rec, tmp / "flat_vignette.jpg")
    a = measure._load_rgb01(out, 400)
    h, w = a.shape[:2]
    centre = a[h // 2 - 20:h // 2 + 20, w // 2 - 20:w // 2 + 20].mean()
    corners = np.concatenate([
        a[:40, :40].ravel(), a[:40, -40:].ravel(),
        a[-40:, :40].ravel(), a[-40:, -40:].ravel(),
    ]).mean()
    assert 0.45 < centre < 0.65, centre
    assert corners < centre * 0.92, (centre, corners)


def test_proof_approves_master(tmp: Path) -> None:
    """A crop read off the proof lands on the master within a couple of pixels."""
    master = _make_master(tmp / "master2.jpg")
    rec0 = Recipe.new("master2.jpg", 1600, 1067)
    proof = render.render(master, rec0, tmp / "proof.jpg", long_edge=400)
    pw, ph = render.image_size(proof)

    entry = {"image": "master2.jpg", "frame": {"w": 1600, "h": 1067},
             "proof_size": {"w": pw, "h": ph}, "proof": str(proof)}
    item = {"id": "master2.jpg", "verdict": "keep",
            "crop": {"space": "proof", "x": 40, "y": 32, "w": 200, "h": 160}}
    rec = review.to_recipe(entry, item)
    left, top, w, h = rec.crop_px()
    # proof is 400 px on a 1600 px master: a factor of exactly 4
    assert abs(w - 800) <= 3, w
    assert abs(h - 640) <= 3, h
    assert abs(left - 160) <= 3, left
    assert abs(top - 128) <= 3, top


def test_unsupported_develop_op(tmp: Path) -> None:
    master = _make_master(tmp / "m3.jpg")
    rec = Recipe.new("m3.jpg", 1600, 1067)
    rec.develop = {"denoise": 0.4}
    try:
        render.render(master, rec, tmp / "x.jpg", dry_run=True)
    except render.UnsupportedOp as exc:
        assert "denoise" in str(exc)
    else:
        raise AssertionError("denoise must be refused by the local renderer")


def test_measure_sanity(tmp: Path) -> None:
    master = _make_master(tmp / "m4.jpg")
    m = measure.measure(master)
    assert 0 < m["mean_luma"] < 1
    assert m["percentiles"]["p1"] <= m["percentiles"]["p50"] <= m["percentiles"]["p99"]
    assert m["sharpness"] > 0
    assert isinstance(m["flags"], list)
    assert -1.5 <= m["suggest_exposure_ev"] <= 1.5
    assert measure.metrics_summary(m)


def test_parallel_slot_exclusive() -> None:
    """Every concurrent call gets a slot no other live call holds.

    Regression: pinning a darktable configdir to `index % jobs` let two frames
    share a configdir, so darktable aborted with a database-lock error. The
    bug was that item->slot must be stable for the *call*, not for the item's
    position in the list, and threads finish out of order.
    """
    import threading
    import time

    live: dict[int, int] = {}
    lock = threading.Lock()
    overlaps: list[tuple[int, int]] = []
    seen: list[int] = []

    def work(item: int, slot: int) -> int:
        with lock:
            if slot in live:
                overlaps.append((slot, item))
            live[slot] = live.get(slot, 0) + 1
            seen.append(slot)
        # deliberately unbalanced durations so threads finish out of order
        time.sleep(0.004 * ((item % 3) + 1))
        with lock:
            live[slot] -= 1
            if live[slot] == 0:
                del live[slot]
        return item * 10

    jobs = 4
    out = util.parallel_map_slot(work, list(range(24)), jobs)
    assert out == [i * 10 for i in range(24)], out      # order preserved
    assert not overlaps, f"two calls shared a slot: {overlaps}"
    assert set(seen) == set(range(jobs)), f"slots used: {sorted(set(seen))}"
    assert util.parallel_map(lambda x: x + 1, [1, 2, 3], 1) == [2, 3, 4]
    assert util.parallel_map_slot(lambda x, s: (x, s), [7], 4) == [(7, 0)]


def test_run_decodes_non_utf8() -> None:
    """A tool that emits non-UTF-8 bytes must not crash the pipeline.

    Regression: darktable 5.6 prints non-UTF-8 bytes in its module-order warning,
    and a strict decode raised UnicodeDecodeError instead of returning output.
    """
    import sys as _sys
    out = util.run([_sys.executable, "-c",
                    "import sys; sys.stdout.buffer.write(b'ok \\xcd done')"])
    assert "ok" in out and "done" in out, repr(out)


# --------------------------------------------------------------------------
# harness
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    e2e = "--e2e" in argv

    tests: list[tuple[str, Callable[[], None]]] = [
        ("geometry.parse_aspect", test_parse_aspect),
        ("geometry.proof_dims", test_proof_dims),
        ("geometry.roundtrip_norm_px", test_roundtrip_norm_px),
        ("geometry.proof_norm_equivalence", test_proof_norm_equivalence),
        ("geometry.fit_aspect", test_fit_aspect),
        ("geometry.clamp", test_clamp),
        ("geometry.rejects_garbage", test_rect_rejects_garbage),
        ("recipe.roundtrip", test_recipe_roundtrip),
        ("recipe.rejects_out_of_range", test_recipe_rejects_out_of_range),
        ("recipe.rejects_unknown_key", test_recipe_rejects_unknown_key),
        ("recipe.rejects_sliver_crop", test_recipe_rejects_sliver_crop),
        ("recipe.fixes_aspect_mismatch", test_recipe_fixes_aspect_mismatch),
        ("review.parse_fenced", test_parse_response_fenced),
        ("review.rejects_bad_verdict", test_parse_response_rejects_bad_verdict),
        ("review.rejects_unknown_key", test_parse_response_rejects_unknown_key),
        ("review.rejects_prose", test_parse_response_rejects_prose),
        ("review.proof_coordinates", test_to_recipe_proof_coordinates),
        ("review.token_estimates", test_token_estimates),
        ("develop.darktable_argv_order", test_darktable_argv_order),
        ("develop.darktable_argv_no_size", test_darktable_argv_no_size),
        ("exif.dms_roundtrip", test_exif_dms_roundtrip),
        ("manifest.raw_hint", test_raw_hint),
        ("review.raw_note_line", test_raw_note_line),
        ("develop.orientation_swaps_dims", test_orientation_swaps_dims),
        ("develop.style_argv_placement", test_style_argv_placement),
        ("develop.apply_presets_must_be_on", test_apply_presets_must_be_on),
        ("styles.decode_param", test_decode_param),
        ("styles.name_resolution", test_style_name_resolution),
        ("util.parallel_slot_exclusive", test_parallel_slot_exclusive),
        ("util.run_decodes_non_utf8", test_run_decodes_non_utf8),
    ]

    failures = 0

    def run(name: str, fn) -> None:
        nonlocal failures
        try:
            fn()
            print(f"{PASS}  {name}")
        except Exception as exc:  # noqa: BLE001 - harness
            failures += 1
            print(f"{FAIL}  {name}: {type(exc).__name__}: {exc}")
            if "-v" in argv:
                traceback.print_exc()

    for name, fn in tests:
        run(name, fn)

    tmp = Path(tempfile.mkdtemp(prefix="pf_selftest_"))
    try:
        # these need a scratch dir but no external tools
        for name, fn in [
            ("styles.parse_dtstyle", lambda: test_parse_dtstyle(tmp)),
            ("styles.import_and_find_style", lambda: test_import_and_find_style(tmp)),
        ]:
            run(name, fn)

        if e2e:
            for name, fn in [
                ("render.equivalence", lambda: test_render_equivalence(tmp)),
                ("render.proof_approves_master", lambda: test_proof_approves_master(tmp)),
                ("render.refuses_develop_ops", lambda: test_unsupported_develop_op(tmp)),
                ("render.vignette", lambda: test_render_vignette(tmp)),
                ("render.sharpen_ignores_noise", lambda: test_render_sharpening_ignores_noise(tmp)),
                ("exif.stamp", lambda: test_exif_stamp(tmp)),
                ("measure.sanity", lambda: test_measure_sanity(tmp)),
            ]:
                run(name, fn)
        else:
            print("(skipped 7 ImageMagick end-to-end tests; pass --e2e to run them)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    total = len(tests) + 2 + (7 if e2e else 0)
    print(f"\n{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
