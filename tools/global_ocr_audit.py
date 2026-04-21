#!/usr/bin/env python3
"""Live OCR audit: capture emulator frame + check pipeline expected coverage.

Flow:
    1. Discover ADB device (or accept --serial). If no device is attached,
       falls back to common Android-emulator ADB ports (127.0.0.1:5555/5565/5575).
       For MuMu Player Pro users, run `tools/global_dev_loop.py` first — it
       auto-connects MuMu's dynamic port via `mumutool` — then run this script.
    2. adb exec-out screencap -p to grab the current frame.
    3. Run the SHIPPED PaddleOCR models (assets/resource/model/ocr/*) via
       rapidocr-onnxruntime so results match MaaFramework's runtime.
    4. For every OCR node whose ROI overlaps any detected text box, report
       match vs. miss. For misses, suggest a (?i)-prefixed regex.
    5. Optional --save stores the capture in tests/MaaEndTestset/ADB/Global/.

Dependencies:
    pip install rapidocr-onnxruntime

Usage:
    python tools/global_ocr_audit.py                       # broad audit, current screen
    python tools/global_ocr_audit.py --node InCrafting     # filter to one node
    python tools/global_ocr_audit.py --file Crafting.json  # filter to one pipeline file
    python tools/global_ocr_audit.py --save main-menu      # save PNG fixture
    python tools/global_ocr_audit.py --image shot.png      # use local PNG, skip adb
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from global_audit_common import (
    PipelineFile,
    expected_patterns,
    find_node_line,
    iter_nodes,
    iter_pipelines,
    load_pipeline,
    ocr_params,
    roi as node_roi,
)

OCR_MODEL_DIR = Path("assets/resource/model/ocr")
DEFAULT_ROOTS = [
    Path("assets/resource/pipeline"),
    Path("assets/resource_adb/pipeline"),
    Path("assets/resource_fast/pipeline"),
    Path("assets/resource_playcover/pipeline"),
    Path("assets/resource_wlroots/pipeline"),
]
ADB_FALLBACK_CANDIDATES = ["127.0.0.1:5555", "127.0.0.1:5565", "127.0.0.1:5575"]
FIXTURE_DIR = Path("tests/MaaEndTestset/ADB/Global")


@dataclass
class DetectedText:
    text: str
    box: tuple[int, int, int, int]  # x, y, w, h
    confidence: float

    def intersects(self, r: tuple[int, int, int, int]) -> bool:
        rx, ry, rw, rh = r
        return not (
            self.box[0] + self.box[2] <= rx
            or rx + rw <= self.box[0]
            or self.box[1] + self.box[3] <= ry
            or ry + rh <= self.box[1]
        )


@dataclass
class Coverage:
    file: str
    node: str
    line: int | None
    roi: tuple[int, int, int, int]
    patterns: list[str]
    detected: list[DetectedText] = field(default_factory=list)
    matched: bool = False
    suggestion: str | None = None


def discover_serial(explicit: str | None) -> str:
    if explicit:
        return explicit
    out = subprocess.run(
        ["adb", "devices"], capture_output=True, text=True, check=True
    ).stdout
    serials = [
        line.split()[0]
        for line in out.splitlines()[1:]
        if line.strip() and "\t" in line and line.strip().endswith("device")
    ]
    if serials:
        return serials[0]
    for candidate in ADB_FALLBACK_CANDIDATES:
        rc = subprocess.run(
            ["adb", "connect", candidate], capture_output=True, text=True
        )
        if "connected to" in rc.stdout or "already connected" in rc.stdout:
            return candidate
    raise RuntimeError(
        "no ADB device attached and default emulator ports unreachable — "
        "pass --serial, run `tools/global_dev_loop.py` first to auto-connect "
        "MuMu Pro, or `adb connect <host:port>` manually"
    )


def capture_screen(serial: str) -> bytes:
    proc = subprocess.run(
        ["adb", "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        check=True,
    )
    if not proc.stdout:
        raise RuntimeError(f"empty screencap from {serial}")
    return proc.stdout


def load_ocr_engine():
    """Build a det+rec pipeline pinned to MaaFramework's shipped ONNX assets.

    RapidOCR's kwarg API doesn't route a custom keys file into the recognizer,
    so we drive the TextDetector + TextRecognizer primitives directly.
    """
    try:
        from rapidocr_onnxruntime.ch_ppocr_v3_det.text_detect import (  # type: ignore
            TextDetector,
        )
        from rapidocr_onnxruntime.ch_ppocr_v3_rec.text_recognize import (  # type: ignore
            TextRecognizer,
        )
        from rapidocr_onnxruntime.utils import LoadImage  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "rapidocr-onnxruntime is not installed. Install with:\n"
            "    pip install rapidocr-onnxruntime\n"
            f"(original error: {exc})"
        )
    det = OCR_MODEL_DIR / "det.onnx"
    rec = OCR_MODEL_DIR / "rec.onnx"
    keys = OCR_MODEL_DIR / "keys.txt"
    for p in (det, rec, keys):
        if not p.exists():
            raise SystemExit(f"missing OCR asset: {p}")
    det_cfg = {
        "use_cuda": False,
        "model_path": str(det),
        "limit_side_len": 736,
        "limit_type": "min",
        "thresh": 0.3,
        "box_thresh": 0.5,
        "max_candidates": 1000,
        "unclip_ratio": 1.6,
        "use_dilation": True,
        "score_mode": "fast",
    }
    rec_cfg = {
        "use_cuda": False,
        "model_path": str(rec),
        "keys_path": str(keys),
        "rec_img_shape": [3, 48, 320],
        "rec_batch_num": 6,
    }
    return {
        "detector": TextDetector(det_cfg),
        "recognizer": TextRecognizer(rec_cfg),
        "loader": LoadImage(),
    }


def _crop_rotated(img, box_pts):
    import cv2  # type: ignore
    import numpy as np  # type: ignore

    pts = np.array(box_pts, dtype=np.float32)
    w = int(max(np.linalg.norm(pts[0] - pts[1]), np.linalg.norm(pts[2] - pts[3])))
    h = int(max(np.linalg.norm(pts[0] - pts[3]), np.linalg.norm(pts[1] - pts[2])))
    if w <= 0 or h <= 0:
        return None
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(pts, dst)
    return cv2.warpPerspective(img, M, (w, h))


def run_ocr(engine, png_bytes: bytes) -> list[DetectedText]:
    import io

    import numpy as np  # type: ignore
    from PIL import Image  # type: ignore

    img_rgb = np.array(Image.open(io.BytesIO(png_bytes)).convert("RGB"))
    img_bgr = img_rgb[:, :, ::-1].copy()
    boxes, _ = engine["detector"](img_bgr)
    if boxes is None or len(boxes) == 0:
        return []
    crops = []
    kept_boxes = []
    for pts in boxes:
        crop = _crop_rotated(img_bgr, pts)
        if crop is None or crop.size == 0:
            continue
        crops.append(crop)
        kept_boxes.append(pts)
    if not crops:
        return []
    rec_out, _ = engine["recognizer"](crops)
    detected: list[DetectedText] = []
    for pts, (text, conf) in zip(kept_boxes, rec_out):
        if not text:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x, y = int(min(xs)), int(min(ys))
        w, h = int(max(xs) - x), int(max(ys) - y)
        detected.append(DetectedText(text=text, box=(x, y, w, h), confidence=float(conf)))
    return detected


def _match_any(patterns: list[str], text: str) -> bool:
    for pat in patterns:
        try:
            if re.search(pat, text):
                return True
        except re.error:
            if pat in text:
                return True
    return False


def _suggest_regex(detected_text: str) -> str:
    return f"(?i){re.escape(detected_text)}"


def compute_coverage(
    pipelines: Iterable[PipelineFile],
    detected: list[DetectedText],
    node_filter: str | None,
    file_filter: str | None,
) -> list[Coverage]:
    coverages: list[Coverage] = []
    for pf in pipelines:
        if file_filter and not fnmatch.fnmatch(pf.path.name, file_filter):
            continue
        for name, node in iter_nodes(pf.data):
            if node_filter and not fnmatch.fnmatch(name, node_filter):
                continue
            ocr = ocr_params(node)
            if ocr is None:
                continue
            r = node_roi(ocr)
            if r is None:
                continue
            patterns = expected_patterns(ocr)
            if not patterns:
                continue
            in_roi = [d for d in detected if d.intersects(r)]
            if not in_roi and not node_filter:
                continue  # broad mode: skip nodes with nothing on screen
            matched = any(_match_any(patterns, d.text) for d in in_roi)
            suggestion = None
            if not matched and in_roi:
                suggestion = _suggest_regex(in_roi[0].text)
            coverages.append(
                Coverage(
                    file=str(pf.path),
                    node=name,
                    line=find_node_line(pf.text, name),
                    roi=r,
                    patterns=patterns,
                    detected=in_roi,
                    matched=matched,
                    suggestion=suggestion,
                )
            )
    return coverages


def print_coverage(coverages: list[Coverage]) -> None:
    if not coverages:
        print("[ocr-audit] no OCR nodes overlap detected text on this frame")
        return
    hits = [c for c in coverages if c.matched]
    misses = [c for c in coverages if not c.matched]
    print(f"[ocr-audit] {len(hits)} matched, {len(misses)} uncovered "
          f"(of {len(coverages)} nodes overlapping detected text)")
    for c in misses:
        loc = f"{c.file}:{c.line}" if c.line else c.file
        det_summary = ", ".join(f"{d.text!r}" for d in c.detected[:3])
        print(f"  MISS {loc} [{c.node}]")
        print(f"       roi={list(c.roi)}  detected={det_summary}")
        print(f"       patterns={c.patterns}")
        if c.suggestion:
            print(f"       suggest: add {c.suggestion!r} to expected")
    if hits:
        print(f"[ocr-audit] {len(hits)} nodes matched (use --verbose for list)")


def save_fixture(png_bytes: bytes, label: str) -> Path:
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    dest = FIXTURE_DIR / f"{label}.png"
    dest.write_bytes(png_bytes)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default=None, help="ADB serial / host:port")
    parser.add_argument("--image", type=Path, default=None,
                        help="use a local PNG instead of capturing via ADB")
    parser.add_argument("--save", default=None,
                        help="save the captured PNG as tests/MaaEndTestset/ADB/Global/<label>.png")
    parser.add_argument("--node", default=None, help="filter to node name (glob)")
    parser.add_argument("--file", default=None, help="filter to pipeline filename (glob)")
    parser.add_argument("--roots", nargs="*", type=Path, default=DEFAULT_ROOTS)
    parser.add_argument("--report", type=Path, default=Path("tools/global_ocr_audit_report.json"))
    parser.add_argument("--verbose", action="store_true", help="list matched nodes too")
    args = parser.parse_args()

    if args.image:
        png_bytes = args.image.read_bytes()
        print(f"[ocr-audit] using local image: {args.image}")
    else:
        try:
            serial = discover_serial(args.serial)
        except RuntimeError as exc:
            print(f"[ocr-audit] {exc}", file=sys.stderr)
            return 2
        print(f"[ocr-audit] capturing from {serial}")
        try:
            png_bytes = capture_screen(serial)
        except subprocess.CalledProcessError as exc:
            print(f"[ocr-audit] screencap failed: {exc}", file=sys.stderr)
            return 2

    if args.save:
        dest = save_fixture(png_bytes, args.save)
        print(f"[ocr-audit] saved fixture: {dest}")

    engine = load_ocr_engine()
    detected = run_ocr(engine, png_bytes)
    print(f"[ocr-audit] detected {len(detected)} text boxes")

    pipelines = list(iter_pipelines([Path(r) for r in args.roots]))
    coverages = compute_coverage(pipelines, detected, args.node, args.file)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(
            [
                {
                    "file": c.file,
                    "node": c.node,
                    "line": c.line,
                    "roi": list(c.roi),
                    "patterns": c.patterns,
                    "matched": c.matched,
                    "suggestion": c.suggestion,
                    "detected": [
                        {"text": d.text, "box": list(d.box), "confidence": d.confidence}
                        for d in c.detected
                    ],
                }
                for c in coverages
            ],
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print_coverage(coverages)
    if args.verbose:
        for c in coverages:
            if c.matched:
                loc = f"{c.file}:{c.line}" if c.line else c.file
                print(f"  OK   {loc} [{c.node}]")

    return 0


if __name__ == "__main__":
    sys.exit(main())
