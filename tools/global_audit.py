#!/usr/bin/env python3
"""Static adaptation linter for CN→Global fork of MaaEnd.

Flags pipeline nodes that likely assume the CN client:
    R1  CJK-only `expected` on an OCR node (no English alternate, no (?i) regex)
    R2  Title-like OCR node with narrow ROI that will clip English text
    R3  CN package literal in a StartApp action
    R4  ClickKey key=4 — BlueStacks back-key quirk on Global
    R5  English word in `expected` without a `(?i)` prefix
    R6  Node touched by upstream AND previously patched locally  (needs --since)

Exit code 0 unless a rule with severity=error fires, or --strict escalates.
"""

from __future__ import annotations

import argparse
import dataclasses
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
    action_info,
    expected_patterns,
    find_node_line,
    has_case_insensitive_prefix,
    has_cjk,
    has_latin,
    iter_nodes,
    iter_pipelines,
    load_pipeline,
    ocr_params,
    roi,
)

SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2}

DEFAULT_ROOTS = [
    Path("assets/resource/pipeline"),
    Path("assets/resource_adb/pipeline"),
    Path("assets/resource_fast/pipeline"),
    Path("assets/resource_playcover/pipeline"),
    Path("assets/resource_wlroots/pipeline"),
]

CN_PACKAGE = "com.hypergryph.endfield"
ROI_TITLE_MIN_WIDTH = 200
TITLE_NAME_RE = re.compile(r"^In[A-Z]|Title|Menu")


@dataclass
class Finding:
    file: str
    node: str
    rule: str
    severity: str
    message: str
    line: int | None = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        if not self.extra:
            d.pop("extra")
        return d


def rule_r1(pf: PipelineFile) -> Iterable[Finding]:
    for name, node in iter_nodes(pf.data):
        ocr = ocr_params(node)
        if ocr is None:
            continue
        patterns = expected_patterns(ocr)
        if not patterns:
            continue
        if all(has_cjk(p) and not has_latin(p) for p in patterns):
            yield Finding(
                file=str(pf.path),
                node=name,
                rule="R1",
                severity="warn",
                message=f"OCR node has CJK-only expected patterns ({len(patterns)}) with no English/(?i) alternate",
                line=find_node_line(pf.text, name),
                extra={"patterns": patterns},
            )


def _looks_like_title(name: str, ocr: dict) -> bool:
    if TITLE_NAME_RE.search(name):
        return True
    r = roi(ocr)
    if r is not None:
        _, y, _, h = r
        if y < 100 and h < 100:
            return True
    return False


def rule_r2(pf: PipelineFile) -> Iterable[Finding]:
    for name, node in iter_nodes(pf.data):
        ocr = ocr_params(node)
        if ocr is None:
            continue
        r = roi(ocr)
        if r is None:
            continue
        if not _looks_like_title(name, ocr):
            continue
        x, y, w, h = r
        if w == 0:
            continue  # dynamic/offset-based ROI
        if w >= ROI_TITLE_MIN_WIDTH:
            continue
        yield Finding(
            file=str(pf.path),
            node=name,
            rule="R2",
            severity="info",
            message=f"title-like OCR node has ROI width {w}<{ROI_TITLE_MIN_WIDTH} — may clip English",
            line=find_node_line(pf.text, name),
            extra={"roi": [x, y, w, h]},
        )


def rule_r3(pf: PipelineFile) -> Iterable[Finding]:
    for name, node in iter_nodes(pf.data):
        action_type, param = action_info(node)
        if action_type != "StartApp":
            continue
        pkg = param.get("package")
        if isinstance(pkg, str) and CN_PACKAGE in pkg:
            yield Finding(
                file=str(pf.path),
                node=name,
                rule="R3",
                severity="error",
                message=f"StartApp targets CN package '{pkg}' — should be 'com.gryphline.endfield.gp' for Global",
                line=find_node_line(pf.text, name),
                extra={"package": pkg},
            )


def rule_r4(pf: PipelineFile) -> Iterable[Finding]:
    for name, node in iter_nodes(pf.data):
        action_type, param = action_info(node)
        if action_type != "ClickKey":
            continue
        key = param.get("key")
        if key == 4:
            yield Finding(
                file=str(pf.path),
                node=name,
                rule="R4",
                severity="info",
                message="ClickKey key=4 (BACK) — on BlueStacks Global this opens 'Switch to controller?' dialog",
                line=find_node_line(pf.text, name),
            )


def rule_r5(pf: PipelineFile) -> Iterable[Finding]:
    for name, node in iter_nodes(pf.data):
        ocr = ocr_params(node)
        if ocr is None:
            continue
        for pattern in expected_patterns(ocr):
            if has_cjk(pattern):
                continue  # CJK-dominant pattern with incidental Latin (e.g. UID)
            if has_case_insensitive_prefix(pattern):
                continue
            letters = re.findall(r"[A-Za-z]", pattern)
            if len(letters) < 2:
                continue
            has_upper = any(c.isupper() for c in letters)
            has_lower = any(c.islower() for c in letters)
            if not (has_upper and has_lower):
                continue  # all-caps tokens like 'BAKER' rarely need (?i)
            yield Finding(
                file=str(pf.path),
                node=name,
                rule="R5",
                severity="info",
                message=f"mixed-case English pattern without (?i): {pattern!r}",
                line=find_node_line(pf.text, name),
                extra={"pattern": pattern},
            )


def rule_r6(roots: list[Path], since: str, base: str) -> Iterable[Finding]:
    """R6: node body differs between `base` and `since` AND between `base` and HEAD.

    Intended usage: after fetching upstream but before rebasing, invoke with
    `--since upstream/v2 --base v2` (where v2 is the previous upstream head on
    local). Findings list nodes that upstream and local have both modified — the
    classic 3-way merge collision set.
    """
    try:
        upstream_files = set(
            subprocess.check_output(
                ["git", "diff", "--name-only", f"{base}..{since}"], text=True
            ).splitlines()
        )
        local_files = set(
            subprocess.check_output(
                ["git", "diff", "--name-only", f"{base}..HEAD"], text=True
            ).splitlines()
        )
    except subprocess.CalledProcessError as exc:
        print(f"[global-audit] R6 skipped: git diff failed ({exc})", file=sys.stderr)
        return
    candidate_files = [Path(p) for p in sorted(upstream_files & local_files) if p]
    if not candidate_files:
        return

    root_prefixes = [str(r) for r in roots]

    def _under_pipeline_root(p: Path) -> bool:
        s = str(p)
        return any(s.startswith(prefix) for prefix in root_prefixes)

    for f in candidate_files:
        if not _under_pipeline_root(f):
            continue
        base_bodies = _node_bodies_at_rev(f, base)
        upstream_bodies = _node_bodies_at_rev(f, since)
        head_bodies = _node_bodies_at_rev(f, "HEAD")
        shared = base_bodies.keys() & upstream_bodies.keys() & head_bodies.keys()
        for node_name in sorted(shared):
            upstream_changed = base_bodies[node_name] != upstream_bodies[node_name]
            local_changed = base_bodies[node_name] != head_bodies[node_name]
            if upstream_changed and local_changed:
                try:
                    pf = load_pipeline(f) if f.exists() else None
                    line = find_node_line(pf.text, node_name) if pf else None
                except Exception:
                    line = None
                yield Finding(
                    file=str(f),
                    node=node_name,
                    rule="R6",
                    severity="warn",
                    message=f"node changed upstream ({base}..{since}) AND locally ({base}..HEAD) — review merge",
                    line=line,
                )


def _node_bodies_at_rev(path: Path, rev: str) -> dict[str, str]:
    try:
        text = subprocess.check_output(
            ["git", "show", f"{rev}:{path}"], text=True, stderr=subprocess.DEVNULL
        )
    except subprocess.CalledProcessError:
        return {}
    from validate_schema import strip_jsonc_comments
    try:
        data = json.loads(strip_jsonc_comments(text))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: json.dumps(v, sort_keys=True, ensure_ascii=False)
        for k, v in data.items()
        if isinstance(v, dict)
    }


ALL_RULES = {
    "R1": rule_r1,
    "R2": rule_r2,
    "R3": rule_r3,
    "R4": rule_r4,
    "R5": rule_r5,
}


def run_audit(
    roots: list[Path],
    rules: set[str],
    since: str | None,
    base: str | None,
) -> list[Finding]:
    findings: list[Finding] = []
    for pf in iter_pipelines(roots):
        for code, fn in ALL_RULES.items():
            if code not in rules:
                continue
            findings.extend(fn(pf))
    if "R6" in rules and since and base:
        findings.extend(rule_r6(roots, since, base))
    findings.sort(key=lambda f: (f.file, f.line or 0, f.rule, f.node))
    return findings


def print_summary(findings: list[Finding], limit: int, report_path: Path) -> None:
    by_rule: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for f in findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
    if not findings:
        print("[global-audit] no findings")
        return
    print(f"[global-audit] {len(findings)} findings "
          f"({', '.join(f'{k}={v}' for k, v in sorted(by_sev.items()))})")

    shown_per_rule: dict[str, int] = {}
    elided_per_rule: dict[str, int] = {}
    for f in findings:
        shown = shown_per_rule.get(f.rule, 0)
        if limit > 0 and shown >= limit:
            elided_per_rule[f.rule] = elided_per_rule.get(f.rule, 0) + 1
            continue
        loc = f"{f.file}:{f.line}" if f.line else f.file
        print(f"  {f.severity.upper():5s} {f.rule} {loc} [{f.node}] {f.message}")
        shown_per_rule[f.rule] = shown + 1

    for rule, n in sorted(elided_per_rule.items()):
        print(f"  ... {n} more {rule} findings (see {report_path})")
    print(f"[global-audit] by rule: {', '.join(f'{k}={v}' for k, v in sorted(by_rule.items()))}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roots", nargs="*", type=Path, default=DEFAULT_ROOTS,
                        help="pipeline directories to scan")
    parser.add_argument("--rules", default="R1,R2,R3,R4,R5",
                        help="comma-separated rule codes (R1-R6). Default excludes R6.")
    parser.add_argument("--since", default=None,
                        help="R6: 'upstream' ref being compared (e.g. upstream/v2). Implies R6.")
    parser.add_argument("--base", default="v2",
                        help="R6: merge-base ref to compare against (default: v2).")
    parser.add_argument("--files", nargs="*", type=Path, default=None,
                        help="restrict scan to these files (overrides --roots)")
    parser.add_argument("--node", default=None,
                        help="filter findings to a single node name (exact or glob)")
    parser.add_argument("--report", type=Path, default=Path("tools/global_audit_report.json"),
                        help="write machine-readable report here")
    parser.add_argument("--limit", type=int, default=20,
                        help="cap stdout findings per rule (0 = unlimited). Full list always in --report.")
    parser.add_argument("--strict", action="store_true",
                        help="exit nonzero on warn-level findings too (ENV: GLOBAL_AUDIT_STRICT=1)")
    args = parser.parse_args()

    rules = {r.strip() for r in args.rules.split(",") if r.strip()}
    if args.since:
        rules.add("R6")

    if args.files:
        roots = []
        # Pretend files are their own root so iter_pipelines picks them up.
        findings: list[Finding] = []
        for f in args.files:
            if not f.exists():
                print(f"[global-audit] missing: {f}", file=sys.stderr)
                continue
            pf = load_pipeline(f)
            for code, fn in ALL_RULES.items():
                if code not in rules:
                    continue
                findings.extend(fn(pf))
        if "R6" in rules and args.since:
            findings.extend(rule_r6(args.files, args.since, args.base))
    else:
        findings = run_audit([Path(r) for r in args.roots], rules, args.since, args.base)

    if args.node:
        findings = [f for f in findings if fnmatch.fnmatch(f.node, args.node)]

    findings.sort(key=lambda f: (f.file, f.line or 0, f.rule, f.node))

    try:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(
            json.dumps([f.to_dict() for f in findings], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[global-audit] could not write report: {exc}", file=sys.stderr)

    print_summary(findings, args.limit, args.report)

    strict = args.strict or (__import__("os").environ.get("GLOBAL_AUDIT_STRICT") == "1")
    threshold = SEVERITY_ORDER["warn"] if strict else SEVERITY_ORDER["error"]
    has_failure = any(SEVERITY_ORDER[f.severity] >= threshold for f in findings)
    return 1 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
