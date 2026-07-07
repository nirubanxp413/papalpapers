#!/usr/bin/env python3
"""Remove fake checkerboard backgrounds from render JPEGs and write true PNGs.

Reads JPEGs from data/render-1-gemini/images/ and writes RGBA PNGs to
data/render-1-gemini/images-png/ (separate folder, same filenames).

Uses border flood-fill for standard checkerboards; falls back to rembg when
detection looks unreliable (dark studio backgrounds, near-zero/near-full mask).

    python3 scripts/remove_render_backgrounds.py status
    python3 scripts/remove_render_backgrounds.py run --limit 20
    python3 scripts/remove_render_backgrounds.py run
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
INPUT_DIR = ROOT / "data" / "render-1-gemini" / "images"
OUTPUT_DIR = ROOT / "data" / "render-1-gemini" / "images-png"
MANIFEST_PATH = ROOT / "data" / "render-1-gemini" / "png_manifest.jsonl"
CHECKLIST_PATH = ROOT / "data" / "render-1-gemini" / "checklist.json"

DEFAULT_WORKERS = 4
MIN_TRANSPARENT = 0.06
MAX_TRANSPARENT = 0.94
DARK_BORDER_LUM = 45
CHECKERBOARD_TOL = 22
CLASSIC_BG_COLORS = (
    (255, 255, 255),
    (204, 204, 204),
    (206, 206, 206),
    (208, 208, 208),
    (210, 210, 210),
    (192, 192, 192),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def append_manifest(event: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    event = {"ts": utc_now(), **event}
    with MANIFEST_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def output_path_for(jpeg_path: Path) -> Path:
    return OUTPUT_DIR / f"{jpeg_path.stem}.png"


def list_inputs() -> list[Path]:
    if not INPUT_DIR.exists():
        raise SystemExit(f"Input directory not found: {INPUT_DIR}")
    return sorted(INPUT_DIR.glob("*.jpg"))


def sample_border_colors(arr: np.ndarray, band: int = 40) -> list[tuple[int, int, int]]:
    h, w = arr.shape[:2]
    points: list[tuple[int, int, int]] = []
    for x in range(0, w, 4):
        for y in range(min(band, h)):
            points.append(tuple(int(v) for v in arr[y, x]))
        for y in range(max(0, h - band), h):
            points.append(tuple(int(v) for v in arr[y, x]))
    for y in range(0, h, 4):
        for x in range(min(band, w)):
            points.append(tuple(int(v) for v in arr[y, x]))
        for x in range(max(0, w - band), w):
            points.append(tuple(int(v) for v in arr[y, x]))
    return [color for color, _ in Counter(points).most_common(8)]


def border_mean_luminance(colors: list[tuple[int, int, int]]) -> float:
    if not colors:
        return 255.0
    vals = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in colors[:4]]
    return sum(vals) / len(vals)


def build_background_mask(arr: np.ndarray, tol: int = CHECKERBOARD_TOL) -> tuple[np.ndarray, float]:
    h, w = arr.shape[:2]
    border_colors = sample_border_colors(arr)
    bg = np.zeros((h, w), dtype=bool)

    for color in border_colors:
        target = np.array(color, dtype=np.int16)
        bg |= np.all(np.abs(arr - target) <= tol, axis=2)

    for color in CLASSIC_BG_COLORS:
        target = np.array(color, dtype=np.int16)
        bg |= np.all(np.abs(arr - target) <= tol, axis=2)

    visited = np.zeros((h, w), dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    for x in range(w):
        queue.append((0, x))
        queue.append((h - 1, x))
    for y in range(h):
        queue.append((y, 0))
        queue.append((y, w - 1))

    while queue:
        y, x = queue.popleft()
        if y < 0 or y >= h or x < 0 or x >= w or visited[y, x]:
            continue
        if not bg[y, x]:
            continue
        visited[y, x] = True
        queue.extend([(y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)])

    return visited, visited.mean()


def rgba_from_mask(arr: np.ndarray, mask: np.ndarray) -> Image.Image:
    alpha = np.where(mask, 0, 255).astype(np.uint8)
    rgba = np.dstack([arr.astype(np.uint8), alpha])
    return Image.fromarray(rgba, "RGBA")


def remove_checkerboard(rgb: Image.Image) -> tuple[Image.Image, float, list[tuple[int, int, int]]]:
    arr = np.array(rgb.convert("RGB"), dtype=np.int16)
    mask, ratio = build_background_mask(arr)
    border_colors = sample_border_colors(arr)
    return rgba_from_mask(arr, mask), ratio, border_colors


def remove_rembg(jpeg_bytes: bytes) -> Image.Image:
    from rembg import remove

    result = remove(jpeg_bytes)
    return Image.open(BytesIO(result)).convert("RGBA")


def should_use_rembg(transparent_ratio: float, border_colors: list[tuple[int, int, int]]) -> bool:
    if transparent_ratio < MIN_TRANSPARENT or transparent_ratio > MAX_TRANSPARENT:
        return True
    if border_mean_luminance(border_colors) < DARK_BORDER_LUM and transparent_ratio < 0.70:
        return True
    return False


def process_image(jpeg_path: Path, *, force: bool = False) -> dict:
    out_path = output_path_for(jpeg_path)
    if out_path.exists() and not force:
        return {
            "source": jpeg_path.name,
            "output": out_path.relative_to(ROOT).as_posix(),
            "status": "skipped",
            "method": None,
        }

    started = time.perf_counter()
    jpeg_bytes = jpeg_path.read_bytes()
    rgb = Image.open(BytesIO(jpeg_bytes)).convert("RGB")

    checker_img, transparent_ratio, border_colors = remove_checkerboard(rgb)
    method = "checkerboard"
    final_img = checker_img

    if should_use_rembg(transparent_ratio, border_colors):
        final_img = remove_rembg(jpeg_bytes)
        method = "rembg"
        transparent_ratio = float((np.array(final_img)[:, :, 3] == 0).mean())

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    final_img.save(out_path, format="PNG", optimize=True)

    elapsed = time.perf_counter() - started
    return {
        "source": jpeg_path.name,
        "output": out_path.relative_to(ROOT).as_posix(),
        "status": "complete",
        "method": method,
        "transparent_ratio": round(transparent_ratio, 4),
        "elapsed_s": round(elapsed, 2),
        "bytes": out_path.stat().st_size,
    }


def cmd_status(_: argparse.Namespace) -> None:
    inputs = list_inputs()
    outputs = list(OUTPUT_DIR.glob("*.png")) if OUTPUT_DIR.exists() else []
    manifest_methods: Counter[str] = Counter()
    if MANIFEST_PATH.exists():
        for line in MANIFEST_PATH.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            if event.get("status") == "complete" and event.get("method"):
                manifest_methods[event["method"]] += 1

    print(f"Input JPEGs:  {INPUT_DIR} ({len(inputs)})")
    print(f"Output PNGs:  {OUTPUT_DIR} ({len(outputs)})")
    print(f"Remaining:    {max(0, len(inputs) - len(outputs))}")
    if manifest_methods:
        print("Methods used:")
        for method, count in sorted(manifest_methods.items()):
            print(f"  {method}: {count}")


def cmd_run(args: argparse.Namespace) -> None:
    inputs = list_inputs()
    pending = inputs
    if not args.force:
        pending = [p for p in inputs if not output_path_for(p).exists()]
    if args.limit is not None:
        pending = pending[: args.limit]

    if not pending:
        print("Nothing to process.")
        return

    print(f"Processing {len(pending)} image(s) → {OUTPUT_DIR}")
    if args.dry_run:
        for path in pending:
            print(f"  {path.name} -> {output_path_for(path).name}")
        return

    completed = 0
    failed = 0
    methods: Counter[str] = Counter()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_image, path, force=args.force): path for path in pending
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                result = {
                    "source": path.name,
                    "status": "failed",
                    "error": str(exc),
                }
                append_manifest(result)
                print(f"FAILED {path.name}: {exc}", file=sys.stderr)
                continue

            append_manifest(result)
            if result["status"] == "complete":
                completed += 1
                methods[result["method"]] += 1
                print(
                    f"OK {result['source']} [{result['method']}, "
                    f"{result['transparent_ratio']*100:.1f}% transparent]"
                )
            else:
                print(f"SKIP {result['source']}")

    print(
        f"Done — complete={completed}, failed={failed}, "
        f"checkerboard={methods.get('checkerboard', 0)}, rembg={methods.get('rembg', 0)}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Remove fake checkerboard backgrounds from render JPEGs"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show input/output counts")

    p_run = sub.add_parser("run", help="Process JPEGs into PNGs")
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p_run.add_argument("--force", action="store_true", help="Reprocess even if PNG exists")
    p_run.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if args.command == "status":
        cmd_status(args)
    elif args.command == "run":
        cmd_run(args)


if __name__ == "__main__":
    main()
