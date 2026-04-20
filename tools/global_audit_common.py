"""Shared helpers for the global-audit tools (Phase 1/2/2b).

Parses the MaaEnd pipeline JSONC corpus and exposes OCR/action primitives
in a way that's agnostic to V1 vs V2 recognition/action shapes.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
from validate_schema import strip_jsonc_comments  # reuse JSONC strip logic


CJK_RE = re.compile(r"[\u3000-\u303f\u3040-\u309f\u30a0-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
ENGLISH_WORD_RE = re.compile(r"[A-Za-z]{2,}")
CASE_INSENSITIVE_PREFIX_RE = re.compile(r"\(\?i[-\w]*\)")


@dataclass
class PipelineFile:
    path: Path
    data: dict
    text: str  # original text, for line-number lookup


def load_pipeline(path: Path) -> PipelineFile:
    text = path.read_text(encoding="utf-8")
    stripped = strip_jsonc_comments(text)
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError(f"pipeline file {path} does not have an object at top level")
    return PipelineFile(path=path, data=data, text=text)


def iter_pipelines(roots: Iterable[Path]) -> Iterator[PipelineFile]:
    seen: set[Path] = set()
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for suffix in ("*.json", "*.jsonc"):
            for p in sorted(root.rglob(suffix)):
                resolved = p.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                try:
                    yield load_pipeline(p)
                except Exception as exc:
                    print(f"[global-audit] skip {p}: {exc}", file=sys.stderr)


def iter_nodes(pipeline: dict) -> Iterator[tuple[str, dict]]:
    for name, body in pipeline.items():
        if isinstance(body, dict):
            yield name, body


def ocr_params(node: dict) -> dict | None:
    """Return the OCR recognition params dict, or None if not an OCR node.

    Handles V1 (recognition: 'OCR' with fields inline) and V2
    (recognition: {type: 'OCR', param: {...}}) shapes.
    """
    rec = node.get("recognition")
    if rec == "OCR":
        return node
    if isinstance(rec, dict) and rec.get("type") == "OCR":
        param = rec.get("param")
        return param if isinstance(param, dict) else {}
    return None


def action_info(node: dict) -> tuple[str | None, dict]:
    """Return (action_type, action_param_dict). Handles V1/V2."""
    act = node.get("action")
    if isinstance(act, str):
        return act, node
    if isinstance(act, dict):
        param = act.get("param")
        return act.get("type"), param if isinstance(param, dict) else {}
    return None, {}


def expected_patterns(ocr: dict) -> list[str]:
    raw = ocr.get("expected")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, str)]
    return []


def roi(ocr: dict) -> tuple[int, int, int, int] | None:
    r = ocr.get("roi")
    if isinstance(r, list) and len(r) == 4 and all(isinstance(v, (int, float)) for v in r):
        return tuple(int(v) for v in r)
    return None


def has_cjk(s: str) -> bool:
    return bool(CJK_RE.search(s))


def has_latin(s: str) -> bool:
    return bool(ASCII_LETTER_RE.search(s))


def has_case_insensitive_prefix(pattern: str) -> bool:
    return bool(CASE_INSENSITIVE_PREFIX_RE.search(pattern))


def find_node_line(text: str, node_name: str) -> int | None:
    """Best-effort line number for a top-level node, 1-indexed."""
    escaped = re.escape(node_name)
    pattern = re.compile(rf'^\s*"{escaped}"\s*:', re.MULTILINE)
    m = pattern.search(text)
    if not m:
        return None
    return text.count("\n", 0, m.start()) + 1
