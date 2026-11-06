"""Command line entry point: python -m photoflow <command> ...

    inventory  read EXIF for the whole shoot            (no pixels decoded)
    proofs     tier-1 small proofs + exact scale record
    measure    numeric metrics per proof
    sheets     tier-0 labelled contact sheets
    review     build request bundles / ingest model answers
    render     execute recipes at proof or master size
    exif       stamp capture date/camera/lens/GPS onto rendered JPEGs
    budget     token cost of a tiered review for N photos
    selftest   run the test suite

Every command is idempotent and writes only inside the workspace.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from . import (develop, exifmeta, manifest, measure, previews, render, review, sheets,
               styles, util)
from .paths import Workdir
from .recipe import Recipe
from .util import eprint


# --------------------------------------------------------------------------
def cmd_inventory(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    entries = manifest.inventory(args.photos, recursive=not args.no_recursive)
    util.write_json(wd.manifest, entries)
    if args.print:
        print(manifest.flatten(entries))
    raw = sum(1 for e in entries if e["is_raw"])
    print(f"{len(entries)} files ({raw} raw) -> {wd.manifest}", file=sys.stderr)
    if args.bursts:
        groups = manifest.group_bursts(entries, args.burst_gap)
        print(f"{len(groups)} burst group(s)", file=sys.stderr)
        util.write_json(wd.root / "bursts.json",
                        [[e["image"] for e in g] for g in groups])
    return 0


def _resolve_style(wd: Workdir, args: argparse.Namespace) -> Optional[str]:
    """--no-style > --style > work/settings.json, resolved to an exact db name."""
    if getattr(args, "no_style", False):
        return None
    wanted = getattr(args, "style", None) or styles.get_style(wd.root)
    if not wanted:
        return None
    tpl = wd.root / "dtcfg" / "template"
    try:
        return styles.find_style(tpl, wanted)
    except styles.StyleError as exc:
        eprint(f"warn: {exc}; exporting without a style")
        return None


def _reset_workers(wd: Workdir) -> int:
    """Drop worker configdirs so the next run re-copies the template.

    Worker dirs only copy from `dtcfg/template` when they have no database, so
    a style imported afterwards would never reach them - and darktable would
    then fail with "cannot find the style". They are pure caches, so dropping
    them is safe and is the cheapest correct invalidation.
    """
    removed = 0
    for d in (wd.root / "dtcfg").glob("w*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    return removed


def cmd_styles(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    tpl = wd.root / "dtcfg" / "template"

    if args.init or args.import_file:
        entries = manifest.load(wd.root) if wd.manifest.exists() else []
        probe = args.probe or (entries[0]["path"] if entries else None)
        if probe is None:
            eprint("need --probe IMAGE (or run `inventory` first) to initialise the db")
            return 2
        styles.init_template(wd.root, probe)
        print(f"template configdir ready: {tpl}", file=sys.stderr)

    if args.from_library:
        if not (tpl / "data.db").exists():
            eprint("run `photoflow styles --init` first")
            return 2
        name = styles.import_from_library(tpl, args.from_library, args.match or "",
                                          name=args.rename)
        styles.set_style(wd.root, name)
        _reset_workers(wd)
        print(f"imported style {name!r} from {args.from_library} and selected it",
              file=sys.stderr)
        return 0

    if args.import_file:
        name = styles.import_dtstyle(tpl, args.import_file, name=args.rename)
        styles.set_style(wd.root, name)
        _reset_workers(wd)
        print(f"imported style {name!r} and set it for this workspace", file=sys.stderr)
        return 0

    if args.set:
        name = styles.find_style(tpl, args.set)
        styles.set_style(wd.root, name)
        _reset_workers(wd)
        print(f"style set: {name}", file=sys.stderr)
        return 0

    if args.clear:
        styles.set_style(wd.root, None)
        _reset_workers(wd)
        print("style cleared (darktable defaults)", file=sys.stderr)
        return 0

    if args.list or True:
        if not (tpl / "data.db").exists():
            eprint("no template database yet; run `photoflow styles --init`")
            return 2
        current = styles.get_style(wd.root)
        for s in styles.list_styles(tpl, args.match):
            mark = "*" if s["name"] == current else " "
            print(f"{mark} {s['modules']:>2} modules  {s['name']}")
        return 0
    return 0


def cmd_masters(args: argparse.Namespace) -> int:
    """Develop full-resolution masters that the renderer crops from."""
    wd = Workdir.create(args.work)
    entries = manifest.load(wd.root)
    if args.limit:
        entries = entries[: args.limit]
    slots = max(1, min(args.jobs, len(entries) or 1))
    style = _resolve_style(wd, args)
    if style:
        print(f"style: {style}", file=sys.stderr)

    def one(item: tuple[int, dict], worker: int) -> str:
        idx, entry = item
        dest = wd.master_path(entry["image"], args.format)
        stale = dest.exists() and wd.find_master(entry["image"], style=style) is None
        if dest.exists() and not args.force and not stale:
            return str(dest)
        if stale:
            print(f"re-developing {entry['image']} (style changed)", file=sys.stderr)
        return str(develop.make_master(
            entry["path"], dest,
            work_root=wd.root, worker=worker,
            quality=args.quality,
            style=style,
            sixteen_bit=(args.format in ("tif", "tiff"))))

    made = util.parallel_map_slot(one, list(enumerate(entries)), slots)
    print(f"{len(made)} masters ({args.format}) -> {wd.masters_dir}", file=sys.stderr)
    return 0


def cmd_proofs(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    entries = manifest.load(wd.root)
    if args.limit:
        entries = entries[: args.limit]
    results = previews.make_proofs(
        [e["path"] for e in entries], wd.root,
        long_edge=args.long_edge, mode=args.mode, jobs=args.jobs,
        from_masters=args.from_masters,
        style=_resolve_style(wd, args))
    sizes = {r["proof_size"]["w"] for r in results}
    print(f"{len(results)} proofs at ~{args.long_edge}px ({args.mode}) -> "
          f"{wd.proof_index}", file=sys.stderr)
    print(f"proof widths: {sorted(sizes)}", file=sys.stderr)
    return 0


def cmd_measure(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    index = util.read_json(wd.proof_index)
    out: dict[str, Any] = {}

    def one(entry: dict) -> tuple[str, dict]:
        return entry["image"], measure.measure(entry["proof"])

    for name, m in util.parallel_map(one, index, args.jobs):
        out[name] = m
        if args.print:
            print(f"{name:<28} {measure.metrics_summary(m)}")
    util.write_json(wd.root / "metrics" / "metrics.json", out)
    print(f"{len(out)} measured -> {wd.root / 'metrics' / 'metrics.json'}", file=sys.stderr)
    return 0


def cmd_sheets(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    index = util.read_json(wd.proof_index)
    if args.limit:
        index = index[: args.limit]
    built = sheets.plan_sheets(index, wd.root / "sheets", per_sheet=args.per_sheet,
                               tile=args.tile, cols=args.cols, pointsize=args.pointsize)
    util.write_json(wd.root / "sheets" / "index.json", built)
    for s in built:
        print(f"{s['sheet']}  {s['sheet_size']['w']}x{s['sheet_size']['h']}  "
              f"{len(s['cells'])} cells", file=sys.stderr)
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    if args.ingest:
        return _ingest_review(wd, Path(args.ingest), args)

    index = util.read_json(wd.proof_index)
    metrics = {}
    mpath = wd.root / "metrics" / "metrics.json"
    if mpath.exists():
        metrics = util.read_json(mpath)
    raw_info: dict[str, dict] = {}
    if wd.manifest.exists():
        raw_info = {e["image"]: e.get("raw_hint") or {}
                    for e in manifest.load(wd.root)}
    if args.only:
        keep = set(args.only)
        index = [e for e in index if e["image"] in keep or Path(e["image"]).stem in keep]
    if args.limit:
        index = index[: args.limit]

    per = max(1, args.per_batch)
    made = []
    for page, start in enumerate(range(0, len(index), per), start=1):
        chunk = index[start:start + per]
        bundle = review.build_request(chunk, metrics=metrics, raw_info=raw_info,
                                      deliverable=args.deliverable,
                                      instructions=args.instructions)
        path = wd.root / "review" / f"request_{page:03d}.json"
        util.write_json(path, bundle)
        prompt_path = wd.root / "review" / f"request_{page:03d}.prompt.md"
        prompt_path.write_text(bundle["prompt"], encoding="utf-8")
        made.append((path, prompt_path, len(chunk)))

    for path, prompt_path, n in made:
        print(f"{path}  ({n} images)  prompt: {prompt_path}", file=sys.stderr)
    tok = review.estimate_batch(len(index))
    print(f"estimated review cost: ~{tok['total_patches']} tokens "
          f"(patch scheme) for {len(index)} proofs", file=sys.stderr)
    return 0


def _ingest_review(wd: Workdir, path: Path, args: argparse.Namespace) -> int:
    text = path.read_text(encoding="utf-8")
    payload = review.parse_response(text)
    index = {e["image"]: e for e in util.read_json(wd.proof_index)}

    written = 0
    for item in payload["items"]:
        entry = index.get(item["id"])
        if entry is None:
            eprint(f"skip unknown id {item['id']!r}")
            continue
        base = None
        if wd.has_recipe(item["id"]) and not args.fresh:
            base = wd.recipe(item["id"])
        rec = review.to_recipe(entry, item, base=base)
        rec.dump(wd.recipe_path(item["id"]))
        written += 1
        if rec.warnings:
            for w in rec.warnings:
                eprint(f"warn {item['id']}: {w}")
    print(f"{written} recipe(s) written -> {wd.root / 'recipes'}", file=sys.stderr)
    return 0


SKIP_VERDICTS = ("reject", "duplicate")


def cmd_render(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    index = {e["image"]: e for e in util.read_json(wd.proof_index)}
    recipes = sorted((wd.root / "recipes").glob("*.json"))
    if not recipes:
        eprint("no recipes yet - run `review --ingest` first")
        return 2

    long_edge = None if args.full else args.edge
    target_dir = wd.root / "out" / ("full" if args.full else "proof")
    done = skipped = failed = 0

    for path in recipes:
        rec = Recipe.load(path)
        if args.only and rec.image not in args.only and Path(rec.image).stem not in args.only:
            continue
        if rec.review.get("verdict") in SKIP_VERDICTS and not (
                args.keep_duplicates and rec.review.get("verdict") == "duplicate"):
            skipped += 1
            continue
        entry = index.get(rec.image)
        if entry is None:
            eprint(f"warn: {rec.image} not in proof index, skipping")
            skipped += 1
            continue

        # crop the *developed* master, never the RAW: ImageMagick cannot read
        # every RAW and cropping the RAW would bypass darktable's look
        style = _resolve_style(wd, args)
        master = wd.find_master(rec.image, style=style)
        if master is None:
            try:
                master = develop.make_master(
                    entry["source"], wd.master_path(rec.image, args.master_format),
                    work_root=wd.root, style=style,
                    sixteen_bit=(args.master_format != "jpg"))
            except (util.ToolError, ValueError) as exc:
                failed += 1
                eprint(f"error {rec.image}: master failed: {exc}")
                continue

        out = target_dir / f"{Path(rec.image).stem}.jpg"
        try:
            render.render(master, rec, out, long_edge=long_edge,
                          quality=args.quality, dry_run=args.dry_run,
                          ignore_develop=args.ignore_develop)
            done += 1
            if args.verbose:
                print(f"{out}  {render.image_size(out)}")
        except render.UnsupportedOp as exc:
            failed += 1
            eprint(f"error {rec.image}: {exc}")
        except util.ToolError as exc:
            failed += 1
            eprint(f"error {rec.image}: {exc}")
        else:
            # full-size deliverables carry the capture metadata; proofs do not
            if args.full and not args.dry_run:
                try:
                    exifmeta.stamp(out, entry["source"])
                    gps = exifmeta.get_gps(wd.root)
                    if gps:
                        exifmeta.set_gps(out, *gps)
                except util.ToolError as exc:
                    eprint(f"warn {rec.image}: exif stamp failed: {exc}")
        if rec.warnings:
            for w in rec.warnings:
                eprint(f"warn {rec.image}: {w}")

    print(f"rendered {done}, skipped {skipped}, failed {failed} -> {target_dir}",
          file=sys.stderr)
    return 1 if failed else 0


def cmd_exif(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    entries = manifest.load(wd.root)
    if not entries:
        eprint("no manifest - run `inventory` first")
        return 2

    sources: dict[str, Path] = {}
    for entry in entries:
        src = Path(entry["path"])
        if not src.is_absolute():
            for base in (Path.cwd(), wd.root.parent, wd.root):
                if (base / src).exists():
                    src = base / src
                    break
        sources[Path(entry["image"]).stem] = src

    dirs = args.dirs if args.dirs is not None else [
        "out/full", "style_grid/gallery", "style_grid/final"]
    paths: list[Path] = []
    for d in dirs:
        base = Path(d) if Path(d).is_absolute() else wd.root / d
        paths += sorted(base.glob("*.jpg"))
    if args.only:
        keep = {Path(str(n)).stem for n in args.only}
        paths = [p for p in paths if p.stem in keep]
    if not paths:
        eprint("no rendered JPEGs found in the requested directories")
        return 2

    done, skipped = exifmeta.stamp_paths(paths, sources, dry_run=args.dry_run)

    gps = None
    if args.gps:
        try:
            lat, lon = (float(x) for x in args.gps.split(","))
        except ValueError:
            eprint("error: --gps must be LAT,LON (decimal degrees)")
            return 2
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            eprint("error: --gps out of range")
            return 2
        if not args.dry_run:
            exifmeta.set_gps_setting(wd.root, lat, lon)
        gps = (lat, lon)
    else:
        gps = exifmeta.get_gps(wd.root)

    if gps:
        for path in paths:
            exifmeta.set_gps(path, *gps, dry_run=args.dry_run)
        print(f"gps {gps[0]:.7f},{gps[1]:.7f} -> {len(paths)} file(s)")

    if args.verify:
        for path in paths:
            tags = exifmeta.verify(path)
            print(f"{path.name}: " + ", ".join(f"{k.split('.')[-1]}={v}" for k, v in tags.items()))
    print(f"stamped {done}, skipped {skipped} ({'dry run' if args.dry_run else 'written'})")
    return 0 if done else 1


def cmd_budget(args: argparse.Namespace) -> int:
    n = args.photos
    if Path(str(n)).is_dir():
        n = len(manifest.inventory(str(n), recursive=True))
    plan = review.budget_plan(int(n), per_sheet=args.per_sheet,
                              shortlist=args.shortlist, proof_edge=args.edge)
    print(json.dumps(plan, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    wd = Workdir.create(args.work)
    print(wd.describe())
    if wd.manifest.exists():
        print(f"  photos  : {len(manifest.load(wd.root))}")
    if wd.proof_index.exists():
        print(f"  proofs  : {len(util.read_json(wd.proof_index))} indexed")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from . import selftest
    argv = ["--e2e"] if args.e2e else []
    if args.verbose:
        argv.append("-v")
    return selftest.main(argv)


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="photoflow", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn, help_: str, *, photos: bool = False) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--work", default="work", help="workspace directory")
        if photos:
            sp.add_argument("--photos", required=True, help="shoot directory")
        sp.set_defaults(func=fn)
        return sp

    sp = add("inventory", cmd_inventory, "read EXIF for the shoot", photos=True)
    sp.add_argument("--no-recursive", action="store_true")
    sp.add_argument("--print", action="store_true")
    sp.add_argument("--bursts", action="store_true")
    sp.add_argument("--burst-gap", type=float, default=2.0)

    sp = add("styles", cmd_styles, "list / import / select darktable styles")
    sp.add_argument("--init", action="store_true", help="create the template configdir")
    sp.add_argument("--import", dest="import_file", default=None,
                    help="path to a .dtstyle to import (and select)")
    sp.add_argument("--rename", default=None, help="store the imported style under this name")
    sp.add_argument("--from-library", nargs="?", const=str(Path.home() / ".config" / "darktable" / "data.db"),
                    default=None, metavar="DATA_DB",
                    help="import a style from a real darktable library "
                         "(default: ~/.config/darktable/data.db) - use --match to name it")
    sp.add_argument("--set", default=None, help="select a style by name or suffix")
    sp.add_argument("--clear", action="store_true", help="stop applying a style")
    sp.add_argument("--list", action="store_true", help="list available styles")
    sp.add_argument("--match", default=None, help="filter the listing")
    sp.add_argument("--probe", default=None, help="image used to initialise the db")

    sp = add("masters", cmd_masters, "develop full-res masters for rendering")
    sp.add_argument("--format", default="jpg", choices=("jpg", "tif"),
                    help="jpg = 8-bit review masters, tif = 16-bit final delivery")
    sp.add_argument("--quality", type=int, default=95)
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--limit", type=int)
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--style", default=None, help="darktable style name or suffix")
    sp.add_argument("--no-style", action="store_true")

    sp = add("proofs", cmd_proofs, "build small proofs")
    sp.add_argument("--long-edge", type=int, default=previews.DEFAULT_PROOF_EDGE)
    sp.add_argument("--mode", choices=("develop", "embedded"), default="develop")
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--limit", type=int)
    sp.add_argument("--from-masters", action="store_true",
                    help="develop masters once, then proof from them")
    sp.add_argument("--style", default=None, help="darktable style name or suffix")
    sp.add_argument("--no-style", action="store_true")

    sp = add("measure", cmd_measure, "numeric metrics per proof")
    sp.add_argument("--jobs", type=int, default=4)
    sp.add_argument("--print", action="store_true")

    sp = add("sheets", cmd_sheets, "labelled contact sheets")
    sp.add_argument("--per-sheet", type=int, default=25)
    sp.add_argument("--tile", type=int, default=360)
    sp.add_argument("--cols", type=int, default=6)
    sp.add_argument("--pointsize", type=int, default=20)
    sp.add_argument("--limit", type=int)

    sp = add("review", cmd_review, "build request bundles / ingest answers")
    sp.add_argument("--deliverable", default="social_4x5", choices=sorted(review.DELIVERABLES))
    sp.add_argument("--per-batch", type=int, default=10)
    sp.add_argument("--instructions", default=None)
    sp.add_argument("--limit", type=int)
    sp.add_argument("--only", nargs="*", default=None)
    sp.add_argument("--ingest", default=None, help="path to a model response file")
    sp.add_argument("--fresh", action="store_true", help="ignore existing recipes")

    sp = add("render", cmd_render, "execute recipes")
    sp.add_argument("--edge", type=int, default=800, help="proof-size render")
    sp.add_argument("--full", action="store_true", help="render at master resolution")
    sp.add_argument("--quality", type=int, default=95)
    sp.add_argument("--only", nargs="*", default=None)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--ignore-develop", action="store_true")
    sp.add_argument("--master-format", default="jpg", choices=("jpg", "tif"),
                    help="format to develop on demand when no master exists")
    sp.add_argument("--keep-duplicates", action="store_true",
                    help="render frames the reviewer marked 'duplicate' too")
    sp.add_argument("--style", default=None, help="darktable style for on-demand masters")
    sp.add_argument("--no-style", action="store_true")
    sp.add_argument("--verbose", action="store_true")

    sp = add("exif", cmd_exif, "stamp capture EXIF onto rendered JPEGs")
    sp.add_argument("--dirs", nargs="*", default=None,
                    help="directories to stamp (default: out/full, style_grid/gallery, style_grid/final)")
    sp.add_argument("--only", nargs="*", default=None, help="frame stems to stamp")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verify", action="store_true", help="print the copied tags")
    sp.add_argument("--gps", default=None, metavar="LAT,LON",
                    help="set GPS position on every file and remember it in work/settings.json")

    sp = add("budget", cmd_budget, "estimate review token cost")
    sp.add_argument("photos", help="count, or a directory to count")
    sp.add_argument("--per-sheet", type=int, default=25)
    sp.add_argument("--shortlist", type=int, default=25)
    sp.add_argument("--edge", type=int, default=800)

    add("status", cmd_status, "workspace summary")

    sp = add("selftest", cmd_selftest, "run the test suite")
    sp.add_argument("--e2e", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except util.ToolError as exc:
        eprint(f"tool error: {exc}")
        return 3
    except (ValueError, KeyError) as exc:
        eprint(f"error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
