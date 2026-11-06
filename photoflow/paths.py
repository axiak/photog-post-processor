"""Workspace layout. One directory per shoot, everything reproducible inside it.

    <root>/
      manifest.json          shoot inventory (EXIF only)
      proofs/                tier-1 proofs (~800 px) + index.json
      sheets/                tier-0 contact sheets + cell maps
      metrics/               measurements per proof
      recipes/               the source of truth, one json per photo
      review/                request bundles + raw model responses
      out/proof/             renders of recipes, proof size
      out/full/              renders of recipes, master size
      dtcfg/w00..wNN         one darktable configdir per worker
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import json

__all__ = ["Workdir"]


@dataclass(frozen=True)
class Workdir:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser())

    def __truediv__(self, other: str) -> Path:
        return self.root / other

    @classmethod
    def create(cls, root: str | Path) -> "Workdir":
        wd = cls(Path(root))
        for sub in ("proofs", "sheets", "metrics", "recipes", "review",
                    "out/proof", "out/full", "dtcfg", "logs"):
            (wd.root / sub).mkdir(parents=True, exist_ok=True)
        return wd

    @property
    def masters_dir(self) -> Path:
        """darktable-developed full-resolution files the renderer crops from."""
        d = self.root / "masters"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def master_path(self, image: str, fmt: str = "jpg") -> Path:
        return self.masters_dir / f"{Path(image).stem}.{fmt}"

    def master_meta_path(self, image: str, fmt: str = "jpg") -> Path:
        return self.masters_dir / f"{Path(image).stem}.{fmt}.meta.json"

    def find_master(self, image: str, style: Optional[str] = None) -> Path | None:
        """A usable master for `image`, or None if it must be (re)developed.

        A cached master developed with a different style is *not* usable: the
        whole point of a style is that it changes the develop result, and
        silently reusing the old file makes a style switch look like a no-op.
        """
        for fmt in ("tif", "tiff", "jpg"):
            p = self.master_path(image, fmt)
            if not p.exists():
                continue
            meta_path = self.master_meta_path(image, fmt)
            if not meta_path.exists():
                return None if style is not None else p
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return None
            if (meta.get("style") or None) == (style or None):
                return p
            return None
        return None

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def proof_index(self) -> Path:
        return self.root / "proofs" / "index.json"

    def recipe_path(self, image: str) -> Path:
        return self.root / "recipes" / f"{Path(image).stem}.json"

    def recipe(self, image: str):
        from .recipe import Recipe
        return Recipe.load(self.recipe_path(image))

    def has_recipe(self, image: str) -> bool:
        return self.recipe_path(image).exists()

    def describe(self) -> str:
        return (f"workspace: {self.root}\n"
                f"  recipes : {len(list((self.root / 'recipes').glob('*.json')))}\n"
                f"  proofs  : {len(list((self.root / 'proofs').glob('*.jpg')))}\n"
                f"  renders : {len(list((self.root / 'out').rglob('*.jpg')))}")
