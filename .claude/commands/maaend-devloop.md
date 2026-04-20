---
description: Iterative dev-loop for porting a CN-only task to the Global client. Fabricates a transient MXU instance containing only the target task, runs it on BlueStacks, parses the log delta + post-screen OCR, and steps through pipeline edits until the task passes.
allowed-tools: Bash, Read, Edit, Grep, Glob
---

Dev-loop for task: `$ARGUMENTS`

**Safety first.** Every `iterate` launches `mxu --autostart`, which drives the real game state on BlueStacks (clicks, screen transitions, potentially spending stamina/items). Never invoke autonomously without explicit user consent — follow the confirmation gates in the steps below.

## 0. Parse $ARGUMENTS

The user is allowed to free-type. `$ARGUMENTS` may be anything from a bare task name (`VisitFriends`) to a full-sentence description (`let's fix the crafting menu title on Global`).

1. **Enumerate the authoritative task list** — filenames under `install/tasks/` are the canonical task names:
   ```bash
   ls install/tasks/*.json 2>/dev/null | xargs -n1 basename | sed 's/\.json$//' | sort
   ```
   These are the names the dev-loop can run.

2. **Extract flags from `$ARGUMENTS`** (look anywhere in the string):
   - `--autonomous` — drop the per-iteration confirm prompt. Only honor it if the user explicitly typed it (or later says "keep going / autonomous mode").
   - `--note "..."` — explicit note. If they didn't pass `--note` but the input includes free-form description beyond a task name, treat the surplus text as an **implicit note** and echo it back once at the top.

3. **Resolve the task name** by matching `$ARGUMENTS` against the task list, in order:
   - **Exact match** (case-insensitive) against a full task name → use it.
   - **Substring match** — check if any task name is a case-insensitive substring of the input (e.g. `crafting` → `Crafting`, `visit friends` → `VisitFriends` after stripping spaces). If exactly one hit, use it.
   - **Acronym / token match** — split the input on whitespace and try to match token sequences to task names (e.g. `auto eco farm` → `AutoEcoFarm`).
   - **Fuzzy fallback** — for each task name, score by number of matching case-folded tokens; if one name scores strictly higher than all others, use it.

4. **Ambiguity handling**:
   - **Exactly one resolved task** → proceed with it. Briefly confirm: "Working on `<Task>`" (and echo the implicit note if any). Don't ask for approval — the user's intent is clear.
   - **Multiple candidates** (e.g. `auto` matches `AutoCollect`, `AutoEcoFarm`, `AutoEssence`, `AutoSell`, `AutoStockpile`, `AutoStockStaple`, `AutoUseSpMedication`) → show the shortlist (up to 8) and ask the user to pick. Stop.
   - **Zero candidates** → show the full task list (grouped sensibly if long) and ask which task they mean. Stop.

5. **Empty `$ARGUMENTS`** → ask which task, offering the list.

Once a task is resolved, continue to step 1 with that name in place of `<TaskName>` throughout.

## 1. Preflight

Run these in parallel, report back:

```bash
adb devices | tail -n +2 | head -5
ls -la install/mxu install/resource 2>/dev/null
git -C . status --porcelain | head -20
```

- If `adb devices` is empty, tell the user: "BlueStacks isn't exposing ADB — start BlueStacks and make sure port 5555 is reachable." Stop.
- If `install/mxu` is missing, suggest `./tools/update_build_and_run.sh` first.
- If the working tree has pending pipeline JSON changes (`assets/resource/**/*.json`), flag them so the user knows `iterate` will run against those edits (the symlink makes them live without rebuild).
- If they have pending Go or C++ changes, mention that `iterate` will auto-run `tools/build_and_install.py` (fast) before `mxu`.

## 2. Confirm starting state

Ask: "Is BlueStacks currently on the expected starting screen for `<TaskName>`? (y/n — or describe what's on screen if unsure)"

- If the user says "not sure" or describes an unexpected screen, take a quick read-only snapshot so we can look at it together:
  ```bash
  python3 tools/global_ocr_audit.py --save preflight-$(date +%s) 2>&1 | tail -20
  ```
  Then read what was detected and help them navigate before proceeding.
- If they confirm ready, go to step 3. If `--autonomous` was passed, skip the prompt.

## 3. Run iterate

Invoke the dev-loop. If `--autonomous` was in args, add it; otherwise omit so the tool's own confirm-gate fires.

```bash
python3 tools/global_dev_loop.py iterate <TaskName> [--autonomous]
```

Stream the output. Capture:
- The build line (`build: none` vs `build: go-service rebuilt`).
- The log-delta summary (`X events, Y succeeded, Z failed`).
- Every `FAIL [<node>]` block — these are the always-failing nodes with pipeline file + line + detected text + suggested regex.
- The post-screen coverage block (uncovered nodes whose ROI overlaps detected text).
- `post.png` path — use it if OCR suggestions look off and you need to eyeball the actual frame (via Read, it renders inline).

If `mxu` exits nonzero or the tool reports a hard error (exit code 2), stop and surface the error to the user before any edits.

## 4. Propose edits — one node at a time

For each always-failing node in the diff output:

1. Read the reported pipeline file at the reported line. Pull enough surrounding context to understand the node (recognition type, roi, expected patterns, `next`).
2. Decide the right fix:
   - **OCR miss with detected English text** → add the suggested `(?i)...` pattern alongside the existing CJK. Do not replace the CN pattern.
   - **ROI too narrow** (detected text clipped, e.g. `GUEST TERMII` where a wider English label doesn't fit) → widen the ROI. Use `pipeline-guide` skill conventions (720p baseline). Measure from `post.png` if needed.
   - **Wrong screen entirely** (no text in ROI matches anything) → don't touch the regex; this is a flow bug, not an OCR bug. Flag it to the user and stop — root cause likely in an earlier node's `next` list.
3. Present the proposed Edit to the user inline (show the exact `old_string` / `new_string` you'd apply) and ask "Apply this edit?" — wait for y/n.
4. On yes: apply with Edit. On no: skip and move to the next node.

Do not batch-apply. One node at a time keeps the user in control and lets them redirect when a suggestion is wrong (OCR hallucinates, especially on timers and partially-rendered text).

## 5. Re-run to verify

After any applied edits, re-run:

```bash
python3 tools/global_dev_loop.py iterate <TaskName> [--autonomous]
```

Compare the new diff against the previous:
- Did the previously-failing nodes now succeed? Good — move to the next unresolved node.
- Same nodes still failing? Re-read the node with the now-known detected text; the first suggestion may have missed (e.g. wrong case, diacritics, whitespace). Try a refined regex.
- New failures appearing? Could be flow progress — we got past the first blocker and hit a later one. Normal. Handle the new one next.

## 6. Stop conditions

End the loop when any of:
- The diff shows zero always-failing nodes for this task AND the post-screen coverage is clean.
- The user says "stop", "done", or similar.
- Three consecutive iterations with no progress (same nodes failing) — stop and debug with the user; it's probably not an OCR fix.

## 7. Wrap up

Summarize in a few lines:
- Which nodes were edited (pipeline file + node name + nature of change).
- Whether the task now completes end-to-end.
- If anything still fails, what you think the root cause is (flow bug / ROI / Go-side algo).
- Remind the user to run `python3 tools/global_dev_loop.py cleanup` if anything seems off — it sweeps orphan `__debug__*` instances.

Do not commit or push anything. The user commits on their own cadence.
