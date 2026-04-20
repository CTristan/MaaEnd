#!/usr/bin/env python3
"""Post-run audit of maafw.log for recognition failures.

Parses MaaFramework's event log (install/debug/maafw.log by default) and
reports nodes where recognition failed, showing:
    - The detected text (or template scores) that MaaFramework actually saw.
    - The expected patterns from the pipeline JSON.
    - A suggested (?i)-escaped regex when the failure is an OCR miss with a
      clearly-detected text in-ROI.

Runs offline against the existing log — no device, no new OCR inference.
Complementary to global_ocr_audit.py (which is proactive, this is post-mortem).

Usage:
    python tools/global_log_audit.py                     # audit latest run
    python tools/global_log_audit.py --node Reception*   # filter by node name glob
    python tools/global_log_audit.py --log path/to.log   # explicit log path
    python tools/global_log_audit.py --algorithm OCR     # only OCR failures
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from global_audit_common import (
    expected_patterns,
    iter_pipelines,
    ocr_params,
)

DEFAULT_LOG = Path("install/debug/maafw.log")
FALLBACK_LOGS = [Path("tests/maatools/maa.log")]
DEFAULT_ROOTS = [
    Path("assets/resource/pipeline"),
    Path("assets/resource_adb/pipeline"),
    Path("assets/resource_fast/pipeline"),
    Path("assets/resource_playcover/pipeline"),
    Path("assets/resource_wlroots/pipeline"),
]

LINE_RE = re.compile(
    r"\[msg=(?P<msg>[^\]]+)\] \[details=(?P<details>\{.*\})\]\s*$"
)


@dataclass
class RecoEvent:
    msg: str
    name: str
    algorithm: str | None
    reco_id: int | None
    task_id: int | None
    details: dict
    raw_line_no: int


@dataclass
class NodeStats:
    name: str
    attempts: int = 0
    failures: int = 0
    successes: int = 0
    last_algorithm: str | None = None
    last_detected_texts: list[str] = field(default_factory=list)
    last_detail: dict | None = None


def iter_events(log_path: Path):
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            m = LINE_RE.search(line)
            if not m:
                continue
            msg = m.group("msg")
            if not msg.startswith("Node.Recognition."):
                continue
            try:
                details = json.loads(m.group("details"))
            except json.JSONDecodeError:
                continue
            name = details.get("name") or "<anon>"
            reco = details.get("reco_details") or {}
            algorithm = reco.get("algorithm") if isinstance(reco, dict) else None
            yield RecoEvent(
                msg=msg,
                name=name,
                algorithm=algorithm,
                reco_id=details.get("reco_id"),
                task_id=details.get("task_id"),
                details=details,
                raw_line_no=lineno,
            )


def collect_ocr_texts(detail: Any) -> list[tuple[str, list[int], float]]:
    """Walk a reco_details.detail tree and pull OCR text boxes."""
    found: list[tuple[str, list[int], float]] = []
    if not detail:
        return found
    if isinstance(detail, list):
        for item in detail:
            if isinstance(item, dict) and item.get("algorithm") == "OCR":
                d = item.get("detail") or {}
                for hit in (d.get("all") or []):
                    text = hit.get("text", "")
                    if text:
                        found.append(
                            (text, hit.get("box") or [], float(hit.get("score", 0.0)))
                        )
            elif isinstance(item, dict) and "detail" in item:
                found.extend(collect_ocr_texts(item.get("detail")))
    elif isinstance(detail, dict):
        for hit in (detail.get("all") or []):
            text = hit.get("text", "")
            if text:
                found.append(
                    (text, hit.get("box") or [], float(hit.get("score", 0.0)))
                )
    return found


def build_pipeline_index(roots: list[Path]) -> dict[str, tuple[Path, list[str]]]:
    """Map node name -> (pipeline file path, expected patterns list)."""
    index: dict[str, tuple[Path, list[str]]] = {}
    for pf in iter_pipelines(roots):
        for name, body in pf.data.items():
            if not isinstance(body, dict):
                continue
            ocr = ocr_params(body)
            if ocr is None:
                continue
            patterns = expected_patterns(ocr)
            if not patterns:
                continue
            index.setdefault(name, (pf.path, patterns))
    return index


def suggest_regex(detected: str) -> str:
    return f"(?i){re.escape(detected)}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=None,
                        help=f"log file (default: {DEFAULT_LOG} or {FALLBACK_LOGS[0]})")
    parser.add_argument("--node", default=None, help="filter to node-name glob")
    parser.add_argument("--algorithm", default=None,
                        help="filter to algorithm (OCR|TemplateMatch|ColorMatch|And|Or|...)")
    parser.add_argument("--roots", nargs="*", type=Path, default=DEFAULT_ROOTS)
    parser.add_argument("--only-failed", action="store_true",
                        help="suppress nodes that ever succeeded")
    parser.add_argument("--limit", type=int, default=20,
                        help="cap findings per node in stdout (0=unlimited)")
    args = parser.parse_args()

    if args.log:
        log_path = args.log
    elif DEFAULT_LOG.exists():
        log_path = DEFAULT_LOG
    else:
        log_path = next((p for p in FALLBACK_LOGS if p.exists()), DEFAULT_LOG)

    if not log_path.exists():
        print(f"[log-audit] no log found at {log_path}", file=sys.stderr)
        return 2

    print(f"[log-audit] parsing {log_path}")

    stats: dict[str, NodeStats] = defaultdict(lambda: NodeStats(name="<anon>"))
    failed_events: list[RecoEvent] = []

    for ev in iter_events(log_path):
        if args.node and not fnmatch.fnmatch(ev.name, args.node):
            continue
        if args.algorithm and ev.algorithm != args.algorithm:
            continue
        s = stats.setdefault(ev.name, NodeStats(name=ev.name))
        if ev.msg == "Node.Recognition.Starting":
            s.attempts += 1
        elif ev.msg == "Node.Recognition.Succeeded":
            s.successes += 1
            s.last_algorithm = ev.algorithm
        elif ev.msg == "Node.Recognition.Failed":
            s.failures += 1
            s.last_algorithm = ev.algorithm
            reco = ev.details.get("reco_details") or {}
            detail = reco.get("detail")
            s.last_detail = reco if isinstance(reco, dict) else None
            s.last_detected_texts = [t for t, _, _ in collect_ocr_texts(detail)]
            failed_events.append(ev)

    if not stats:
        print("[log-audit] no recognition events matched filters")
        return 0

    index = build_pipeline_index([Path(r) for r in args.roots])

    nodes_with_failures = {n for n, s in stats.items() if s.failures > 0}
    nodes_always_failing = {
        n for n, s in stats.items() if s.failures > 0 and s.successes == 0
    }

    print(f"[log-audit] {len(stats)} distinct nodes, "
          f"{len(nodes_with_failures)} with at least one failure, "
          f"{len(nodes_always_failing)} that never succeeded")

    shown = 0
    for name in sorted(nodes_always_failing):
        s = stats[name]
        if args.only_failed and s.successes > 0:
            continue
        shown += 1
        if args.limit > 0 and shown > args.limit:
            break
        src = index.get(name)
        print(f"  FAIL [{name}]  {s.failures} fail(s)  algo={s.last_algorithm}")
        if src:
            print(f"       pipeline: {src[0]}")
            print(f"       expected: {src[1]}")
        if s.last_detected_texts:
            uniq = Counter(s.last_detected_texts).most_common(5)
            preview = ", ".join(f"{t!r}" for t, _ in uniq)
            print(f"       detected: {preview}")
            if src and not any(
                re.search(pat, text)
                for text in s.last_detected_texts
                for pat in src[1]
                if text
            ):
                first_text = next((t for t in s.last_detected_texts if t.strip()), None)
                if first_text:
                    print(f"       suggest: add {suggest_regex(first_text)!r} to expected")

    elided = len(nodes_always_failing) - min(shown, len(nodes_always_failing))
    if args.limit > 0 and elided > 0:
        print(f"  ... {elided} more always-failing nodes (use --limit 0 for all)")

    intermittent = sorted(nodes_with_failures - nodes_always_failing)
    if intermittent:
        print(f"[log-audit] {len(intermittent)} nodes with intermittent failures "
              f"(eventually succeeded): {', '.join(intermittent[:10])}"
              + ("..." if len(intermittent) > 10 else ""))

    return 0 if not nodes_always_failing else 1


if __name__ == "__main__":
    sys.exit(main())
