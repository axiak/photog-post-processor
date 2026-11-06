"""darktable styles: getting a saved look onto every export.

What a style actually is
------------------------
A style is a saved *module stack*: which processing modules are active, their
parameter structs, their blend/mask settings, and their order. It is not a
master-file or a LUT. A `.dtstyle` file is that stack as XML: one `<plugin>` per
module, with `op_params` as an encoded string and `blendop_params` likewise.

Why this needed real investigation
----------------------------------
`darktable-cli --style NAME` does *not* accept a path. It looks the name up in
the `styles` table of the configdir's `data.db`, which means a custom style has
to be inside that database.

Measured on this machine (darktable 5.6.2):

* A fresh configdir already contains **534 styles**: 510 camera-matching
  basecurve styles (`darktable_<Camera>`) and 24 example looks
  (`examples_colors_*`). So `--style` works out of the box, but only for names
  in that table.
* db names are the *localized* strings, e.g.
  `_l10n_darktable|_l10n_examples|_l10n_colors|_l10n_faded`.
  `examples_colors_faded` fails with "cannot find the style".
* `styles(id, name, description, iop_list)` plus
  `style_items(styleid, num, module, operation, op_params BLOB,
  enabled, blendop_params BLOB, blendop_version, multi_priority, multi_name,
  multi_name_hand_edited)` — a 1:1 map of the `<plugin>` list.
* Param encoding in `.dtstyle`:
  - short params: plain lowercase **hex**
  - long params: `"gz"` + 2 hex chars + base64(**zlib**-compressed bytes)
  - the database stores the **raw uncompressed bytes** as a BLOB
  Verified byte-for-byte: dropping 4 chars of `gz03eJyb...` and inflating
  reproduced the 132-byte blob exactly.
  The meaning of the 2 hex chars is unknown (it is not the compressed or
  uncompressed length); it is ignored on import, which this module only does.

Import is therefore mechanical: decode the params, insert one `styles` row and
N `style_items` rows. :func:`import_dtstyle` does exactly that, and the
round-trip is covered by a test that renders a style imported from its own
`.dtstyle` file and compares pixels against the built-in original.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import sqlite3
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Optional

from . import util

__all__ = [
    "StyleError",
    "decode_param",
    "parse_dtstyle",
    "init_template",
    "import_dtstyle",
    "import_from_library",
    "list_styles",
    "find_style",
    "get_style",
    "set_style",
    "settings_path",
]

GZ_PREFIX = "gz"
TEMPLATE_DIR = "dtcfg/template"
PROBE_SIZE = 16          # px; enough to make darktable create its database


class StyleError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# param codec
# --------------------------------------------------------------------------
def decode_param(text: str) -> bytes:
    """`.dtstyle` param string -> raw bytes (the form stored in the db)."""
    text = (text or "").strip()
    if not text:
        return b""
    if text.startswith(GZ_PREFIX):
        payload = text[len(GZ_PREFIX) + 2:]      # skip "gz" + 2 unknown hex chars
        try:
            return zlib.decompress(base64.b64decode(payload))
        except Exception as exc:  # noqa: BLE001 - report the module, not a traceback
            raise StyleError(f"cannot decode gz param ({len(text)} chars): {exc}") from exc
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise StyleError(f"cannot decode hex param: {exc}") from exc


# --------------------------------------------------------------------------
# .dtstyle parsing
# --------------------------------------------------------------------------
def _text(node, tag: str, default: str = "") -> str:
    child = node.find(tag)
    return (child.text or default) if child is not None else default


def parse_dtstyle(path: str | Path) -> dict:
    """Parse a .dtstyle into {name, description, plugins:[...]}."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        root = ET.fromstring(raw)
        info = root.find("info")
        name = _text(info, "name") if info is not None else ""
        description = _text(info, "description") if info is not None else ""
        plugins = []
        for p in root.iter("plugin"):
            plugins.append({
                "num": int(_text(p, "num", "0") or 0),
                "module": int(_text(p, "module", "0") or 0),
                "operation": _text(p, "operation"),
                "op_params": decode_param(_text(p, "op_params")),
                "enabled": int(_text(p, "enabled", "1") or 1),
                "blendop_params": decode_param(_text(p, "blendop_params")),
                "blendop_version": int(_text(p, "blendop_version", "0") or 0),
                "multi_priority": int(_text(p, "multi_priority", "0") or 0),
                "multi_name": _text(p, "multi_name"),
                "multi_name_hand_edited": int(_text(p, "multi_name_hand_edited", "0") or 0),
            })
    except ET.ParseError:
        # some hand-edited styles are not strict XML; fall back to regex
        name = (re.search(r"<name>(.*?)</name>", raw, re.S) or [None, ""])[1]
        description = (re.search(r"<description>(.*?)</description>", raw, re.S) or [None, ""])[1]
        plugins = []
        for block in re.findall(r"<plugin>(.*?)</plugin>", raw, re.S):
            f = dict(re.findall(r"<(\w+)>(.*?)</\1>", block, re.S))
            plugins.append({
                "num": int(f.get("num", 0) or 0),
                "module": int(f.get("module", 0) or 0),
                "operation": f.get("operation", ""),
                "op_params": decode_param(f.get("op_params", "")),
                "enabled": int(f.get("enabled", 1) or 1),
                "blendop_params": decode_param(f.get("blendop_params", "")),
                "blendop_version": int(f.get("blendop_version", 0) or 0),
                "multi_priority": int(f.get("multi_priority", 0) or 0),
                "multi_name": f.get("multi_name", ""),
                "multi_name_hand_edited": int(f.get("multi_name_hand_edited", 0) or 0),
            })

    if not plugins:
        raise StyleError(f"{path.name} contains no <plugin> modules")
    if not name:
        name = path.stem
    return {"name": name.strip(), "description": description.strip(), "plugins": plugins}


# --------------------------------------------------------------------------
# database
# --------------------------------------------------------------------------
def _db(configdir: str | Path) -> Path:
    return Path(configdir) / "data.db"


def init_template(work_root: str | Path, probe_image: str | Path,
                  worker: int = 0) -> Path:
    """Create `work/dtcfg/template` with a fully initialised data.db.

    darktable only writes its database when it actually processes an image, so
    this does a 16 px export once. Everything else (all worker configdirs) is
    then copied from this template, which also guarantees every worker shares
    exactly the same style set.
    """
    from . import develop

    tpl = Path(work_root) / TEMPLATE_DIR
    if _db(tpl).exists():
        return tpl
    tmp = tpl.with_name("template.tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "probe.jpg"
    develop.darktable_export(probe_image, out, configdir=tmp,
                             long_edge=PROBE_SIZE, quality=50)
    out.unlink(missing_ok=True)
    if not _db(tmp).exists():
        raise StyleError("darktable did not create a database; cannot import styles")
    if tpl.exists():
        shutil.rmtree(tpl)
    tmp.rename(tpl)
    return tpl


def list_styles(configdir: str | Path, match: Optional[str] = None) -> list[dict]:
    db = _db(configdir)
    if not db.exists():
        raise StyleError(f"no database at {db}; run `photoflow styles --init` first")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        sql = ("select s.name, s.description, count(i.num) "
               "from styles s left join style_items i on i.styleid = s.id ")
        args: tuple = ()
        if match:
            sql += "where s.name like ? "
            args = (f"%{match}%",)
        sql += "group by s.id order by s.name"
        return [{"name": n, "description": d, "modules": c}
                for n, d, c in con.execute(sql, args)]
    finally:
        con.close()


def _style_leaf(name: str) -> str:
    """The human-facing part of a db style name.

    darktable stores localized names as `_l10n_<context>|_l10n_<name>`, e.g.
    `_l10n_darktable|_l10n_examples|_l10n_colors|_l10n_sepia`. The leaf has its
    `_l10n_` marker stripped so `sepia` resolves to it.
    """
    leaf = name.split("|")[-1]
    return leaf[len("_l10n_"):] if leaf.startswith("_l10n_") else leaf


def _resolve_name(names: list[str], wanted: str) -> str:
    """Pure name resolver (exact > leaf > case-insensitive > substring)."""
    names = list(names)
    if wanted in names:
        return wanted
    lowered = wanted.lower()
    by_leaf = [n for n in names if _style_leaf(n).lower() == lowered]
    by_exact = [n for n in names if n.lower() == lowered]
    by_sub = [n for n in names if lowered in n.lower()]
    for label, hits in (("leaf name", by_leaf), ("case-insensitive", by_exact),
                        ("substring", by_sub)):
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise StyleError(f"ambiguous style name {wanted!r} ({label}); candidates: "
                             + ", ".join(sorted(hits)[:8]))
    raise StyleError(f"no style matching {wanted!r} in the database")


def find_style(configdir: str | Path, wanted: str) -> str:
    """Resolve a friendly name to the exact db name for this configdir."""
    return _resolve_name([s["name"] for s in list_styles(configdir)], wanted)


STYLE_ITEM_FIELDS = (
    "num", "module", "operation", "op_params", "enabled", "blendop_params",
    "blendop_version", "multi_priority", "multi_name", "multi_name_hand_edited",
)


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f"pragma table_info({table})")]


def _read_style_rows(con: sqlite3.Connection, style_id: int) -> list[dict]:
    """Read one style's modules, tolerating older schemas with fewer columns."""
    have = [c for c in STYLE_ITEM_FIELDS if c in _columns(con, "style_items")]
    sql = f"select {','.join(have)} from style_items where styleid = ? order by num"
    return [dict(zip(have, row)) for row in con.execute(sql, (style_id,))]


def _insert_style(con: sqlite3.Connection, name: str, description: str,
                  rows: list[dict]) -> None:
    """Insert/replace a style. Only columns present in this db are written."""
    have = [c for c in STYLE_ITEM_FIELDS if c in _columns(con, "style_items")]
    con.execute("delete from style_items where styleid in "
                "(select id from styles where name = ?)", (name,))
    con.execute("delete from styles where name = ?", (name,))
    cur = con.execute(
        "insert into styles(name, description, iop_list) values (?, ?, '')",
        (name, description or ""))
    style_id = cur.lastrowid
    placeholders = ",".join("?" for _ in have)
    sql = (f"insert into style_items(styleid, {','.join(have)}) "
           f"values (?, {placeholders})")
    for row in rows:
        values = []
        for col in have:
            value = row.get(col)
            if isinstance(value, bytes):
                value = sqlite3.Binary(value)
            values.append(0 if value is None else value)
        con.execute(sql, [style_id, *values])


def import_dtstyle(configdir: str | Path, dtstyle: str | Path,
                   name: Optional[str] = None) -> str:
    """Insert a .dtstyle into a configdir's styles tables. Returns the db name."""
    db = _db(configdir)
    if not db.exists():
        raise StyleError(f"no database at {db}; run `photoflow styles --init` first")
    parsed = parse_dtstyle(dtstyle)
    style_name = name or parsed["name"]

    con = sqlite3.connect(str(db))
    try:
        _insert_style(con, style_name, parsed["description"], parsed["plugins"])
        con.commit()
    except sqlite3.DatabaseError as exc:
        con.rollback()
        raise StyleError(f"cannot write style into {db}: {exc}") from exc
    finally:
        con.close()
    return style_name


def import_from_library(configdir: str | Path, library_db: str | Path,
                        wanted: str, name: Optional[str] = None) -> str:
    """Copy a style straight out of a real darktable library into the workspace.

    This is the shortcut that avoids the export-a-.dtstyle dance: whatever the
    user created in darktable's GUI already lives in `~/.config/darktable/data.db`.
    That file is opened read-only, and only columns that the (possibly older)
    library schema actually has are carried across.
    """
    library_db = Path(library_db).expanduser()
    if not library_db.exists():
        raise StyleError(f"no darktable library at {library_db}")
    target = _db(configdir)
    if not target.exists():
        raise StyleError(f"no database at {target}; run `photoflow styles --init` first")

    lib = sqlite3.connect(f"file:{library_db}?mode=ro", uri=True)
    try:
        names = [n for (n,) in lib.execute("select name from styles")]
        if not names:
            raise StyleError(
                f"{library_db} contains no styles - create one in darktable first "
                "(darkroom > styles panel > create)")
        source = _resolve_name(names, wanted)
        row = lib.execute("select id, description from styles where name = ?",
                          (source,)).fetchone()
        rows = _read_style_rows(lib, row[0])
        description = row[1] or ""
    finally:
        lib.close()

    style_name = name or source
    con = sqlite3.connect(str(target))
    try:
        _insert_style(con, style_name, description, rows)
        con.commit()
    except sqlite3.DatabaseError as exc:
        con.rollback()
        raise StyleError(f"cannot write style into {target}: {exc}") from exc
    finally:
        con.close()
    return style_name


# --------------------------------------------------------------------------
# workspace setting
# --------------------------------------------------------------------------
def settings_path(work_root: str | Path) -> Path:
    return Path(work_root) / "settings.json"


def get_style(work_root: str | Path) -> Optional[str]:
    p = settings_path(work_root)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("style") or None
    except json.JSONDecodeError:
        return None


def set_style(work_root: str | Path, name: Optional[str]) -> None:
    p = settings_path(work_root)
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    if name:
        data["style"] = name
    else:
        data.pop("style", None)
    util.write_json(p, data)
