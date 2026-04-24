#!/usr/bin/env python3
"""Iterative task-dev loop for porting CN-only MaaEnd tasks to Global.

Orchestrates: snapshot → (build) → run a single task via mxu → diff.

Single-task running is achieved by fabricating a transient
`__debug__<TaskName>` MXU instance (with optionValues cloned from an existing
instance) in the MXU config JSON, invoking `mxu --autostart -i <...>
--quit-after-run`, then removing the transient on exit.

Subcommands:
    snapshot [--label X]       capture pre.png + record log offset
    run <TaskName> [...]       fabricate + run + restore
    diff [--since <label>]     parse new log events + post.png OCR coverage
    iterate <TaskName> [...]   snapshot → (build) → run → diff
    cleanup                    remove any __debug__* instances from MXU config

See /Users/chris/.claude/plans/distributed-jingling-balloon.md for rationale.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import string
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from global_audit_common import iter_pipelines  # noqa: E402
from global_log_audit import (  # noqa: E402
    LINE_RE,
    NodeStats,
    RecoEvent,
    build_pipeline_index,
    collect_ocr_texts,
    suggest_regex,
)
from global_ocr_audit import (  # noqa: E402
    DEFAULT_ROOTS as OCR_DEFAULT_ROOTS,
    capture_screen,
    compute_coverage,
    discover_serial,
    load_ocr_engine,
    run_ocr,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MXU_CONFIG = (
    Path.home()
    / "Library"
    / "Application Support"
    / "MXU"
    / "config"
    / "mxu-MaaEnd.json"
)
MXU_BIN = PROJECT_ROOT / "install" / "mxu"
# MXU's maafw.log path has shifted over time — early builds wrote it under
# the workspace `install/debug/` tree; current builds (v5.10.2+) write it
# under the OS-level data dir. Resolve dynamically so the dev-loop tracks
# whichever path the live binary is actually writing to.
MAA_LOG_CANDIDATES = (
    Path.home()
    / "Library"
    / "Application Support"
    / "MXU"
    / "debug"
    / "maafw.log",
    PROJECT_ROOT / "install" / "debug" / "maafw.log",
)


def _active_maa_log() -> Path:
    """Return the maafw.log path most likely to be written to by mxu.

    Picks whichever candidate exists and has the most recent mtime; falls
    back to the first candidate (current default) if none exist yet."""
    existing = [p for p in MAA_LOG_CANDIDATES if p.exists()]
    if not existing:
        return MAA_LOG_CANDIDATES[0]
    return max(existing, key=lambda p: p.stat().st_mtime)
DEV_LOOP_DIR = PROJECT_ROOT / "install" / "debug" / "dev-loop"
STATE_FILE = DEV_LOOP_DIR / "state.json"
BUILT_AT_FILE = DEV_LOOP_DIR / "built-at.json"
DEBUG_INSTANCE_PREFIX = "__debug__"
# Android first — it's the canonical MaaEnd ADB instance and typically has an
# up-to-date savedDevice. The rest are fallbacks if Android lacks the target task.
DEFAULT_REFERENCE_INSTANCES = ["Android", "Quick Daily", "Full Daily Routine"]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


@dataclass
class Snapshot:
    label: str
    timestamp: str
    log_offset: int
    pre_png: str | None
    log_path: str | None = None

    @classmethod
    def _from_entry(cls, entry: dict) -> "Snapshot":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in entry.items() if k in known})

    @classmethod
    def load(cls, label: str | None = None) -> "Snapshot | None":
        if not STATE_FILE.exists():
            return None
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if label is None:
            entries = data.get("snapshots") or []
            if not entries:
                return None
            return cls._from_entry(entries[-1])
        for entry in data.get("snapshots") or []:
            if entry.get("label") == label:
                return cls._from_entry(entry)
        return None

    def save(self) -> None:
        DEV_LOOP_DIR.mkdir(parents=True, exist_ok=True)
        data: dict = {"snapshots": []}
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {"snapshots": []}
        snaps = data.setdefault("snapshots", [])
        snaps[:] = [s for s in snaps if s.get("label") != self.label]
        snaps.append(asdict(self))
        # trim to last 20
        data["snapshots"] = snaps[-20:]
        _atomic_write_json(STATE_FILE, data)


# ---------------------------------------------------------------------------
# Atomic-write + helpers
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


def _short_slug() -> str:
    """Replicate MXU's 7-char lowercase alphanumeric id style."""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=7))


def _log_offset(log_path: Path | None = None) -> int:
    path = log_path if log_path is not None else _active_maa_log()
    if not path.exists():
        return 0
    return path.stat().st_size


_TASK_TERMINAL_RE = re.compile(
    r'\[msg=Tasker\.Task\.(Succeeded|Failed)\]\s+'
    r'\[details=\{[^}]*"entry":"(?P<entry>[^"]+)"'
)


def _task_terminal_event(log_path: Path, start_offset: int, task_entry: str) -> str | None:
    """Scan log from `start_offset` for a Tasker.Task.(Succeeded|Failed) event
    matching `task_entry`. Returns 'Succeeded'/'Failed' or None."""
    if not log_path.exists():
        return None
    try:
        # If mxu truncated the log between offset capture and now, the
        # offset points past EOF; reading the whole file is what the caller
        # actually wants.
        effective_offset = start_offset
        if log_path.stat().st_size < start_offset:
            effective_offset = 0
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            fh.seek(effective_offset)
            for line in fh:
                m = _TASK_TERMINAL_RE.search(line)
                if m and m.group("entry") == task_entry:
                    return m.group(1)
    except OSError:
        return None
    return None


def _wait_for_task_or_exit(
    proc: subprocess.Popen,
    *,
    task_entry: str,
    log_path: Path,
    log_offset: int,
    overall_timeout: float,
    post_task_grace: float,
    idle_timeout: float,
    poll_interval: float = 1.0,
) -> int:
    """Wait for mxu to exit, with three guardrails (in order of preference):
      1. Tasker.Task.(Succeeded|Failed) seen → wait `post_task_grace`s, then kill.
      2. maafw.log size unchanged for `idle_timeout`s → kill (silently spinning).
      3. Total runtime exceeds `overall_timeout`s → kill (final fallback).

    `log_path` and `log_offset` must be coupled — offset is meaningful only
    against the specific file it was captured from. Re-resolving mid-run
    lets us scan a different file whose byte range contains stale events
    from prior sessions, producing false terminal matches.

    Returns the process exit code (negative if killed via signal)."""
    start = time.monotonic()
    task_done_outcome: str | None = None
    task_done_at: float | None = None

    last_log_size = log_path.stat().st_size if log_path.exists() else 0
    last_log_change_at = start

    while True:
        rc = proc.poll()
        if rc is not None:
            return rc

        now = time.monotonic()
        elapsed = now - start

        if elapsed >= overall_timeout:
            print(
                f"[dev-loop] timeout after {overall_timeout:.0f}s — killing mxu",
                file=sys.stderr,
            )
            _terminate(proc)
            return proc.returncode if proc.returncode is not None else -signal.SIGKILL

        cur_log_size = log_path.stat().st_size if log_path.exists() else 0
        if cur_log_size != last_log_size:
            last_log_size = cur_log_size
            last_log_change_at = now
        elif task_done_outcome is None and now - last_log_change_at >= idle_timeout:
            print(
                f"[dev-loop] mxu log idle for {idle_timeout:.0f}s — assuming hung, "
                f"terminating",
                file=sys.stderr,
            )
            _terminate(proc)
            return (
                proc.returncode if proc.returncode is not None else -signal.SIGTERM
            )

        if task_done_outcome is None:
            outcome = _task_terminal_event(log_path, log_offset, task_entry)
            if outcome is not None:
                task_done_outcome = outcome
                task_done_at = now
                print(
                    f"[dev-loop] task '{task_entry}' {outcome.lower()} — "
                    f"giving mxu {post_task_grace:.0f}s to self-quit",
                )
        else:
            assert task_done_at is not None
            if now - task_done_at >= post_task_grace:
                print(
                    f"[dev-loop] mxu still running {post_task_grace:.0f}s after "
                    f"Tasker.Task.{task_done_outcome} — terminating",
                    file=sys.stderr,
                )
                _terminate(proc)
                return (
                    proc.returncode if proc.returncode is not None else -signal.SIGTERM
                )

        time.sleep(poll_interval)


def _terminate(proc: subprocess.Popen, sigterm_grace: float = 5.0) -> None:
    """SIGTERM → wait → SIGKILL."""
    proc.terminate()
    try:
        proc.wait(timeout=sigterm_grace)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.kill()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


# ---------------------------------------------------------------------------
# MXU instance fabrication
# ---------------------------------------------------------------------------


_ADB_CONNECT_FALLBACKS = ("127.0.0.1:5555",)  # generic Android emulator default
_MUMUTOOL_MACOS_PATH = Path("/Applications/MuMuPlayer Pro.app/Contents/MacOS/mumutool")
_tried_adb_auto_connect = False


def _query_adb_serials() -> set[str]:
    try:
        out = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    return {
        line.split()[0]
        for line in out.splitlines()[1:]
        if "\t" in line and line.strip().endswith("device")
    }


def _discover_mumu_adb_endpoints() -> list[str]:
    # MuMu Pro on macOS assigns a dynamic host-side adb port per Android device
    # (e.g. 26624 for index 0). `mumutool info <index>` exposes it as JSON. The
    # Android-internal "default 5555" shown in MuMu's settings is the guest port
    # and isn't reachable from the host, so probing 5555 would miss it entirely.
    if not _MUMUTOOL_MACOS_PATH.exists():
        return []
    endpoints: list[str] = []
    for idx in range(4):
        try:
            proc = subprocess.run(
                [str(_MUMUTOOL_MACOS_PATH), "info", str(idx)],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            break
        if proc.returncode != 0:
            break
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            break
        if data.get("errcode") != 0:
            break
        ret = data.get("return") or {}
        if ret.get("state") != "running":
            continue
        port = ret.get("adb_port")
        if isinstance(port, int):
            endpoints.append(f"127.0.0.1:{port}")
    return endpoints


def _adb_serial_responsive(serial: str, timeout: float = 3.0) -> bool:
    try:
        return subprocess.run(
            ["adb", "-s", serial, "shell", "true"],
            capture_output=True,
            timeout=timeout,
        ).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _live_adb_serials() -> set[str]:
    # `adb devices` keeps reporting TCP-attached serials as "device" long after
    # the socket has gone dead (MuMu's adbd idle-closes between iterations,
    # sleep/wake reaps it, MuMu may reshuffle its dynamic port). Probe each
    # serial with `adb shell true`; explicitly disconnect unresponsive ones so
    # the reconnect path below isn't short-circuited by `adb connect`'s no-op
    # "already connected" branch.
    global _tried_adb_auto_connect
    serials = _query_adb_serials()
    responsive = {s for s in serials if _adb_serial_responsive(s)}
    for stale in serials - responsive:
        print(
            f"[dev-loop] adb serial {stale} unresponsive — disconnecting",
            file=sys.stderr,
        )
        subprocess.run(
            ["adb", "disconnect", stale],
            capture_output=True,
            timeout=5,
        )
    if responsive or _tried_adb_auto_connect:
        return responsive
    _tried_adb_auto_connect = True
    candidates = _discover_mumu_adb_endpoints() + list(_ADB_CONNECT_FALLBACKS)
    for endpoint in candidates:
        print(
            f"[dev-loop] no responsive adb device — trying `adb connect {endpoint}`",
            file=sys.stderr,
        )
        try:
            subprocess.run(
                ["adb", "connect", endpoint],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        # Verify by probe — `adb devices` would lie about post-connect state too.
        verified = {s for s in _query_adb_serials() if _adb_serial_responsive(s)}
        if verified:
            return verified
    return set()


def _saved_device_is_live(inst: dict, live_serials: set[str]) -> bool:
    """Non-ADB controllers trivially pass. ADB controllers must match a live serial."""
    if inst.get("controllerName") != "ADB":
        return True
    saved = inst.get("savedDevice")
    if not isinstance(saved, dict):
        return False
    name = saved.get("adbDeviceName") or ""
    return any(name.startswith(s) for s in live_serials)


def _load_mxu_config() -> dict:
    if not MXU_CONFIG.exists():
        raise SystemExit(
            f"[dev-loop] MXU config not found at {MXU_CONFIG}\n"
            "Launch MXU at least once so it writes the initial config, then retry."
        )
    return json.loads(MXU_CONFIG.read_text(encoding="utf-8"))


def _save_mxu_config(data: dict) -> None:
    _atomic_write_json(MXU_CONFIG, data)


def _find_reference_instance(
    data: dict, task_name: str, preferred: str | None
) -> dict:
    """Pick the best reference instance for cloning the task's optionValues.

    When ADB is live, prefers ADB-controller instances (since the user is
    clearly targeting ADB, not PlayCover). Further prefers those whose
    savedDevice matches the live serial — avoids picking an instance with a
    stale `emulator-5554-...` device when the user is on `127.0.0.1:5555`.
    """
    instances = [i for i in (data.get("instances") or []) if isinstance(i, dict)]
    non_debug = [
        i for i in instances if not i.get("name", "").startswith(DEBUG_INSTANCE_PREFIX)
    ]
    has_task = [
        i
        for i in non_debug
        if any(t.get("taskName") == task_name for t in (i.get("tasks") or []))
    ]
    by_name = {i.get("name"): i for i in non_debug}

    if preferred:
        inst = by_name.get(preferred)
        if inst and inst in has_task:
            return inst
        if inst:
            print(
                f"[dev-loop] warning: --from-instance '{preferred}' doesn't list "
                f"task '{task_name}'; falling back",
                file=sys.stderr,
            )

    live = _live_adb_serials()

    # When ADB is connected, prefer ADB-controller references.
    if live:
        adb_with_task = [i for i in has_task if i.get("controllerName") == "ADB"]
        matches = [i for i in adb_with_task if _saved_device_is_live(i, live)]
        if matches:
            return matches[0]
        # ADB ref exists for the task but none has live-matching savedDevice;
        # preserve DEFAULT ordering, caller will warn about device mismatch.
        if adb_with_task:
            for name in DEFAULT_REFERENCE_INSTANCES:
                inst = by_name.get(name)
                if inst in adb_with_task:
                    return inst
            return adb_with_task[0]

    # No ADB preference or task not in any ADB instance: DEFAULT order.
    for name in DEFAULT_REFERENCE_INSTANCES:
        inst = by_name.get(name)
        if inst and inst in has_task:
            return inst
    if has_task:
        return has_task[0]
    if non_debug:
        return non_debug[0]

    raise SystemExit(
        "[dev-loop] no MXU instances exist — create one in the UI first "
        "so the dev-loop has something to clone from."
    )


def _borrow_live_saved_device(data: dict, live_serials: set[str]) -> dict | None:
    """Find any instance with a savedDevice whose serial matches live ADB."""
    for inst in data.get("instances") or []:
        if not isinstance(inst, dict):
            continue
        if inst.get("name", "").startswith(DEBUG_INSTANCE_PREFIX):
            continue
        saved = inst.get("savedDevice")
        if not isinstance(saved, dict):
            continue
        name = saved.get("adbDeviceName") or ""
        if any(name.startswith(s) for s in live_serials):
            return saved
    return None


def _fabricate_instance(
    data: dict,
    task_name: str,
    preferred_ref: str | None,
) -> tuple[str, dict]:
    """Insert a transient __debug__<TaskName> instance. Returns (name, full_inst)."""
    ref = _find_reference_instance(data, task_name, preferred_ref)
    debug_name = f"{DEBUG_INSTANCE_PREFIX}{task_name}"

    ref_task = next(
        (t for t in (ref.get("tasks") or []) if t.get("taskName") == task_name),
        None,
    )
    option_values = ref_task.get("optionValues", {}) if ref_task else {}

    new_inst = {
        "id": _short_slug(),
        "name": debug_name,
        "controllerName": ref.get("controllerName"),
        "resourceName": ref.get("resourceName"),
    }
    if "savedDevice" in ref:
        new_inst["savedDevice"] = ref["savedDevice"]

    # For ADB: if reference savedDevice is stale/null, borrow one from any
    # instance whose savedDevice serial matches a live adb device.
    if new_inst.get("controllerName") == "ADB":
        live = _live_adb_serials()
        if live and not _saved_device_is_live(new_inst, live):
            borrowed = _borrow_live_saved_device(data, live)
            if borrowed is not None:
                print(
                    f"[dev-loop] borrowing savedDevice={borrowed} (reference "
                    f"{ref.get('name')!r} had a stale/null device)",
                    file=sys.stderr,
                )
                new_inst["savedDevice"] = borrowed
    new_inst["tasks"] = [
        {
            "id": _short_slug(),
            "taskName": task_name,
            "enabled": True,
            "optionValues": option_values,
        }
    ]

    instances = data.setdefault("instances", [])
    # replace any stale entry with this name
    instances[:] = [i for i in instances if i.get("name") != debug_name]
    instances.append(new_inst)
    return debug_name, new_inst


def _remove_debug_instances(data: dict) -> list[str]:
    instances = data.setdefault("instances", [])
    removed = [
        i.get("name", "<?>")
        for i in instances
        if i.get("name", "").startswith(DEBUG_INSTANCE_PREFIX)
    ]
    instances[:] = [
        i
        for i in instances
        if not i.get("name", "").startswith(DEBUG_INSTANCE_PREFIX)
    ]
    return removed


# ---------------------------------------------------------------------------
# Log delta
# ---------------------------------------------------------------------------


def _iter_events_from(log_path: Path, start_offset: int):
    if not log_path.exists():
        return
    # If mxu truncated/rotated the log between snapshot and now, the recorded
    # offset is past EOF — read the whole file instead of silently returning 0
    # events.
    effective_offset = start_offset
    if log_path.stat().st_size < start_offset:
        effective_offset = 0
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(effective_offset)
        for lineno_rel, line in enumerate(fh, 1):
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
            algorithm = (
                reco.get("algorithm") if isinstance(reco, dict) else None
            )
            yield RecoEvent(
                msg=msg,
                name=name,
                algorithm=algorithm,
                reco_id=details.get("reco_id"),
                task_id=details.get("task_id"),
                details=details,
                raw_line_no=lineno_rel,
            )


def _stats_from_events(events: Iterable[RecoEvent]) -> dict[str, NodeStats]:
    stats: dict[str, NodeStats] = defaultdict(lambda: NodeStats(name="<anon>"))
    for ev in events:
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
    return stats


# ---------------------------------------------------------------------------
# Build ladder
# ---------------------------------------------------------------------------


def _git_changed_paths_since(commit: str | None) -> list[str]:
    cmd = ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain=1"]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    dirty = [
        line[3:].strip()
        for line in out.stdout.splitlines()
        if line.strip() and not line[3:].startswith("tools/")
    ]
    if not commit:
        return dirty
    cmd = [
        "git",
        "-C",
        str(PROJECT_ROOT),
        "diff",
        "--name-only",
        commit,
        "HEAD",
    ]
    diff_out = subprocess.run(cmd, capture_output=True, text=True)
    if diff_out.returncode == 0:
        dirty.extend(
            line.strip() for line in diff_out.stdout.splitlines() if line.strip()
        )
    return dirty


def _classify_changes(paths: list[str]) -> set[str]:
    """Return set of {'json', 'go', 'cpp', 'other'}."""
    kinds: set[str] = set()
    for p in paths:
        if p.startswith("assets/") and (p.endswith(".json") or p.endswith(".jsonc")):
            kinds.add("json")
        elif p.startswith("agent/go-service/"):
            kinds.add("go")
        elif p.startswith("agent/cpp-algo/"):
            kinds.add("cpp")
        elif p.startswith("install/") or p.startswith("deps/"):
            continue  # build artifacts — ignore
        elif p.startswith("tools/"):
            continue  # tool changes don't require rebuild
        elif p.startswith("tests/"):
            continue  # fixtures / MaaEndTestset submodule — not compiled into mxu
        elif p.startswith(".claude/"):
            continue  # Claude Code agent config — not a build input
        elif p.endswith(".md"):
            continue  # docs — never affect the build
        else:
            kinds.add("other")
    return kinds


def _last_built_commit() -> str | None:
    if not BUILT_AT_FILE.exists():
        return None
    try:
        return json.loads(BUILT_AT_FILE.read_text(encoding="utf-8")).get("commit")
    except json.JSONDecodeError:
        return None


def _record_built(commit: str) -> None:
    _atomic_write_json(
        BUILT_AT_FILE, {"commit": commit, "at": _utc_stamp()}
    )


def _current_head() -> str:
    return subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _run_build(kinds: set[str]) -> bool:
    """Run the minimum build for the given change kinds. Returns True on success."""
    cmd = [sys.executable, "tools/build_and_install.py"]
    if "cpp" in kinds:
        cmd.append("--cpp-algo")
    print(f"[dev-loop] building: {' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=PROJECT_ROOT).returncode
    if rc != 0:
        print(f"[dev-loop] build failed (exit {rc})", file=sys.stderr)
        return False
    _record_built(_current_head())
    return True


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_snapshot(args: argparse.Namespace) -> int:
    label = args.label or _utc_stamp()
    snap_dir = DEV_LOOP_DIR / label
    snap_dir.mkdir(parents=True, exist_ok=True)
    pre_path: str | None = None
    if not args.no_screenshot:
        try:
            serial = discover_serial(args.serial)
            png = capture_screen(serial)
            target = snap_dir / "pre.png"
            target.write_bytes(png)
            pre_path = str(target.relative_to(PROJECT_ROOT))
            print(f"[dev-loop] captured {pre_path} from {serial}")
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"[dev-loop] screenshot skipped: {exc}", file=sys.stderr)

    active_log = _active_maa_log()
    snap = Snapshot(
        label=label,
        timestamp=_utc_stamp(),
        log_offset=_log_offset(active_log),
        pre_png=pre_path,
        log_path=str(active_log),
    )
    snap.save()
    print(
        f"[dev-loop] snapshot '{label}' — log {active_log.name} "
        f"offset {snap.log_offset}, "
        f"{'pre.png saved' if pre_path else 'no screenshot'}"
    )
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if not MXU_BIN.exists():
        print(f"[dev-loop] {MXU_BIN} not found — run setup_workspace.py first",
              file=sys.stderr)
        return 2

    original = _load_mxu_config()
    # deep-copy by re-serializing
    working = json.loads(json.dumps(original))

    debug_name, new_inst = _fabricate_instance(
        working, args.task, args.from_instance
    )

    if new_inst.get("controllerName") == "ADB":
        live = _live_adb_serials()
        if not _saved_device_is_live(new_inst, live):
            print(
                f"[dev-loop] warning: cloned savedDevice="
                f"{new_inst.get('savedDevice')} doesn't match live adb devices "
                f"{sorted(live) or '<none>'}. Pass --from-instance <name> to pick "
                f"a different reference, or reconnect the emulator first.",
                file=sys.stderr,
            )

    _save_mxu_config(working)

    cmd = [
        str(MXU_BIN),
        "--autostart",
        "-i",
        debug_name,
        "--quit-after-run",
    ]
    print(f"[dev-loop] planned mxu invocation:")
    print(f"           task:       {args.task}")
    print(f"           instance:   {debug_name}")
    print(f"           controller: {new_inst.get('controllerName')}")
    if "savedDevice" in new_inst:
        print(f"           device:     {new_inst['savedDevice']}")
    print(f"           cmd:        {' '.join(cmd)}")
    print(f"           timeout:    {args.timeout}s")

    if not args.autonomous:
        try:
            resp = input("[dev-loop] proceed? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            print("[dev-loop] aborted by user — restoring MXU config")
            _save_mxu_config(original)
            return 130

    start = time.monotonic()
    rc: int | None = None
    run_log_path = _active_maa_log()
    run_log_offset = _log_offset(run_log_path)

    def _cleanup(signum=None, frame=None):
        print("\n[dev-loop] interrupted — restoring MXU config")
        _save_mxu_config(original)
        sys.exit(130)

    prev_handler = signal.signal(signal.SIGINT, _cleanup)

    try:
        proc = subprocess.Popen(cmd, cwd=PROJECT_ROOT)
        rc = _wait_for_task_or_exit(
            proc,
            task_entry=args.task,
            log_path=run_log_path,
            log_offset=run_log_offset,
            overall_timeout=args.timeout,
            post_task_grace=args.post_task_grace,
            idle_timeout=args.idle_timeout,
        )
    finally:
        signal.signal(signal.SIGINT, prev_handler)
        _save_mxu_config(original)
        print(f"[dev-loop] restored MXU config (removed {debug_name})")

    elapsed = time.monotonic() - start
    print(f"[dev-loop] mxu exit={rc} duration={elapsed:.1f}s")
    return 0 if rc == 0 else 1


def cmd_diff(args: argparse.Namespace) -> int:
    snap = Snapshot.load(args.since)
    if snap is None:
        print(
            "[dev-loop] no snapshot found — run `snapshot` first",
            file=sys.stderr,
        )
        return 2

    # Use the path recorded in the snapshot when available, so diff reads
    # the same log that was active when the snapshot was taken. Fall back to
    # whichever candidate is live now for older snapshots without log_path.
    diff_log = Path(snap.log_path) if snap.log_path else _active_maa_log()
    if not diff_log.exists():
        print(f"[dev-loop] log not found: {diff_log}", file=sys.stderr)
        return 2

    events = list(_iter_events_from(diff_log, snap.log_offset))
    stats = _stats_from_events(events)
    touched_nodes = set(stats.keys())

    succeeded = sum(s.successes for s in stats.values())
    failed = sum(s.failures for s in stats.values())
    always_failing = [
        (name, s)
        for name, s in stats.items()
        if s.failures > 0 and s.successes == 0
    ]

    print(f"=== diff since '{snap.label}' ({snap.timestamp}) ===")
    print(
        f"log delta: {len(events)} events  "
        f"{succeeded} succeeded  {failed} failed  "
        f"{len(touched_nodes)} distinct nodes"
    )

    if always_failing:
        index = build_pipeline_index([Path(r) for r in OCR_DEFAULT_ROOTS])
        print(f"newly-failing nodes: {len(always_failing)}")
        for name, s in sorted(always_failing, key=lambda kv: -kv[1].failures)[
            : args.limit
        ]:
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
                    first_text = next(
                        (t for t in s.last_detected_texts if t.strip()), None
                    )
                    if first_text:
                        print(
                            f"       suggest: add {suggest_regex(first_text)!r} "
                            f"to expected"
                        )
    elif failed:
        print(
            f"no always-failing nodes (intermittent failures: "
            f"{sum(1 for s in stats.values() if s.failures and s.successes)})"
        )
    else:
        print("no failures in delta window")

    # Post-run screenshot + OCR coverage on touched nodes
    post_png: Path | None = None
    if not args.no_screenshot and touched_nodes:
        snap_dir = DEV_LOOP_DIR / snap.label
        snap_dir.mkdir(parents=True, exist_ok=True)
        try:
            serial = discover_serial(args.serial)
            png = capture_screen(serial)
            post_png = snap_dir / "post.png"
            post_png.write_bytes(png)
            print(f"[dev-loop] captured {post_png.relative_to(PROJECT_ROOT)}")

            engine = load_ocr_engine()
            detected = run_ocr(engine, png)
            print(f"[dev-loop] OCR detected {len(detected)} text boxes on post.png")

            # Filter coverage computation to nodes touched in the log delta
            pipelines = list(iter_pipelines([Path(r) for r in OCR_DEFAULT_ROOTS]))
            touched_glob_re = re.compile(
                "^(" + "|".join(re.escape(n) for n in touched_nodes) + ")$"
            )
            coverages = [
                c
                for c in compute_coverage(pipelines, detected, None, None)
                if touched_glob_re.match(c.node)
            ]
            if coverages:
                hits = [c for c in coverages if c.matched]
                misses = [c for c in coverages if not c.matched]
                print(
                    f"post-screen coverage (ROI ∩ touched nodes): "
                    f"{len(hits)}/{len(coverages)} covered"
                )
                for c in misses[: args.limit]:
                    loc = f"{c.file}:{c.line}" if c.line else c.file
                    det_summary = ", ".join(f"{d.text!r}" for d in c.detected[:3])
                    print(f"  uncovered: {loc} [{c.node}]")
                    print(f"             detected={det_summary}")
                    if c.suggestion:
                        print(f"             suggest: add {c.suggestion!r}")
            else:
                print(
                    "post-screen: no touched-node ROI overlaps detected text "
                    "(game likely ended on a different screen)"
                )
        except (RuntimeError, subprocess.CalledProcessError) as exc:
            print(f"[dev-loop] post-screenshot skipped: {exc}", file=sys.stderr)

    if snap.pre_png:
        print(f"pre.png:  {snap.pre_png}")
    if post_png:
        print(f"post.png: {post_png.relative_to(PROJECT_ROOT)}")

    return 1 if always_failing else 0


def cmd_iterate(args: argparse.Namespace) -> int:
    label = args.label or _utc_stamp()

    # 1. Build ladder — decide if we need to build
    build_rc = 0
    if args.no_build:
        print("[dev-loop] --no-build — skipping build step")
    else:
        paths = _git_changed_paths_since(_last_built_commit())
        kinds = _classify_changes(paths)
        if args.rebuild:
            kinds.add("go")
        if args.rebuild_cpp_algo:
            kinds.add("cpp")
        if kinds & {"go", "cpp"}:
            if not _run_build(kinds):
                return 2
        elif kinds == {"json"} or not kinds:
            print("[dev-loop] no build needed (pipeline JSON changes are live via symlink)")
        else:
            print(f"[dev-loop] unclassified changes: {sorted(kinds)} — "
                  f"pass --rebuild or --no-build to proceed")
            return 2

    # 2. Snapshot
    args.label = label
    if cmd_snapshot(args) != 0:
        return 2

    # 3. Run
    if cmd_run(args) not in (0, 1):
        # 0=success, 1=nonzero mxu exit (may still be worth diffing); >1=hard failure
        return 2

    # 4. Diff
    args.since = label
    return cmd_diff(args)


def cmd_cleanup(args: argparse.Namespace) -> int:
    data = _load_mxu_config()
    removed = _remove_debug_instances(data)
    if removed:
        _save_mxu_config(data)
        print(f"[dev-loop] removed {len(removed)} orphan(s): {', '.join(removed)}")
    else:
        print("[dev-loop] no orphan __debug__* instances found")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--serial", default=None, help="ADB serial / host:port (default auto-detect)"
    )
    p.add_argument(
        "--no-screenshot",
        action="store_true",
        help="skip ADB screenshot (log-only mode)",
    )


def _add_diff_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--limit",
        type=int,
        default=20,
        help="cap findings per category in stdout (default 20)",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_snap = sub.add_parser("snapshot", help="capture pre.png + log offset")
    p_snap.add_argument("--label", default=None, help="snapshot label (default: UTC timestamp)")
    _add_common_args(p_snap)
    p_snap.set_defaults(func=cmd_snapshot)

    p_run = sub.add_parser("run", help="fabricate __debug__<Task> + mxu autostart")
    p_run.add_argument("task", help="task name (e.g. VisitFriends)")
    p_run.add_argument(
        "--from-instance",
        default=None,
        help=f"reference instance to clone optionValues from "
        f"(default tries: {', '.join(DEFAULT_REFERENCE_INSTANCES)})",
    )
    p_run.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="kill mxu if still running after N seconds (default 180, final fallback)",
    )
    p_run.add_argument(
        "--autonomous",
        action="store_true",
        help="skip the confirmation prompt (tight-iteration mode)",
    )
    p_run.add_argument(
        "--post-task-grace",
        type=float,
        default=20.0,
        help="seconds to wait for mxu to self-quit after the task's terminal "
        "event fires before killing it (default 20)",
    )
    p_run.add_argument(
        "--idle-timeout",
        type=float,
        default=60.0,
        help="kill mxu if maafw.log size is unchanged for N seconds "
        "(default 60 — catches silently-spinning hangs)",
    )
    p_run.set_defaults(func=cmd_run)

    p_diff = sub.add_parser("diff", help="log delta + post-screenshot OCR coverage")
    p_diff.add_argument(
        "--since",
        default=None,
        help="snapshot label to diff against (default: most recent)",
    )
    _add_common_args(p_diff)
    _add_diff_args(p_diff)
    p_diff.set_defaults(func=cmd_diff)

    p_iter = sub.add_parser("iterate", help="snapshot → (build) → run → diff")
    p_iter.add_argument("task", help="task name (e.g. VisitFriends)")
    p_iter.add_argument("--label", default=None, help="snapshot label (default: UTC)")
    p_iter.add_argument("--from-instance", default=None)
    p_iter.add_argument("--timeout", type=int, default=180)
    p_iter.add_argument("--post-task-grace", type=float, default=20.0)
    p_iter.add_argument("--idle-timeout", type=float, default=60.0)
    p_iter.add_argument("--autonomous", action="store_true")
    p_iter.add_argument("--no-build", action="store_true")
    p_iter.add_argument("--rebuild", action="store_true", help="force Go rebuild")
    p_iter.add_argument(
        "--rebuild-cpp-algo", action="store_true", help="force C++ algo rebuild"
    )
    _add_common_args(p_iter)
    _add_diff_args(p_iter)
    p_iter.set_defaults(func=cmd_iterate)

    p_clean = sub.add_parser("cleanup", help="remove orphan __debug__* instances")
    p_clean.set_defaults(func=cmd_cleanup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
