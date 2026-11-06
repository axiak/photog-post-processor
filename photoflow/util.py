"""Small shared helpers: subprocess, parallelism, json io."""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, TypeVar

T = TypeVar("T")
R = TypeVar("R")

RAW_EXT = {
    ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf",
    ".rw2", ".orf", ".pef", ".dng", ".srw", ".raw", ".rwl", ".iiq", ".3fr",
    ".fff", ".mos", ".mrw", ".x3f", ".erf", ".kdc", ".mef", ".nkc",
}
RASTER_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".heic"}
IMAGE_EXT = RAW_EXT | RASTER_EXT


class ToolError(RuntimeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str):
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"command failed ({returncode}): {' '.join(self.argv[:4])}...\n{stderr.strip()[:1200]}"
        )


def run(argv: Sequence[str], *, check: bool = True, timeout: Optional[float] = None) -> str:
    """Run a command, capture stdout. Raises ToolError with real stderr on failure.

    Decoding is deliberately lenient: darktable 5.6 emits non-UTF-8 bytes inside
    its own warning text (e.g. the module-order fallback notice), and a strict
    decode turned a successful export into a UnicodeDecodeError traceback.
    """
    proc = subprocess.run(
        [str(a) for a in argv],
        capture_output=True, text=True, timeout=timeout, check=False,
        encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        # darktable reports real failures on *stdout* while stderr only carries
        # a harmless notice, so surfacing stderr alone hid the cause twice.
        detail = "\n".join(s for s in (proc.stderr, proc.stdout) if s and s.strip())
        raise ToolError(argv, proc.returncode, detail)
    return proc.stdout


def require(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        raise ToolError([tool], 127, f"required tool {tool!r} not found on PATH")
    return path


def parallel_map_slot(fn: Callable[[T, int], R], items: Iterable[T],
                      jobs: int = 1) -> list[R]:
    """Thread pool that hands each call an *exclusive* slot in ``[0, jobs)``.

    Order is preserved. A slot is checked out for the whole call and only
    returned when it finishes, so no two calls ever hold the same slot.

    This exists because pinning a resource to ``enumerate() index % jobs`` is
    wrong: threads pull the next item and finish out of order, so a fast frame
    can pick up a slot whose previous owner is still running. With darktable
    that means two processes sharing one configdir, and darktable exits 1 with
    ``can't acquire database lock, aborting`` (on stdout, not stderr). Measured
    on this shoot: 2 of 16 frames failed nondeterministically at --jobs 4.
    """
    items = list(items)
    if jobs <= 1 or len(items) <= 1:
        return [fn(i, 0) for i in items]
    free: "queue.Queue[int]" = queue.Queue()
    for s in range(jobs):
        free.put(s)

    def run(item: T) -> R:
        slot = free.get()
        try:
            return fn(item, slot)
        finally:
            free.put(slot)

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return list(pool.map(run, items))


def parallel_map(fn: Callable[[T], R], items: Iterable[T], jobs: int = 1) -> list[R]:
    """Thread pool over subprocess-heavy work; order is preserved."""
    return parallel_map_slot(lambda item, _slot: fn(item), items, jobs)


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, data: Any) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return p


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr, flush=True)
