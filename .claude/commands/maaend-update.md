---
description: Pull from upstream/v2, rebase local patches onto it, build (no launch), force-push to fork, and summarize upstream changes in English.
allowed-tools: Bash
---

Run the MaaEnd daily update flow. Do not launch MXU at the end.

1. Safeguard any uncommitted work before the rebase. Run:
   ```
   git status --porcelain
   ```
   If the output is non-empty (modified tracked files or untracked files), do not proceed to step 2 until everything is committed and pushed — we do not want in-progress work to be lost if the rebase fails, or left behind to block the rebase.
   - Inspect the changes with `git diff` (staged + unstaged) and `git log --oneline -5` to match this repo's commit message style (short lowercase subject, e.g. `local: add ...`).
   - Group related changes into one or more logical commits. Stage files explicitly by name — never `git add .` or `git add -A` (may sweep in secrets/binaries). Do not commit files that look like secrets (`.env`, credentials, tokens).
   - Create the commits. Signing is disabled in this repo's local git config — never re-enable it or pass `-S`.
   - Push to the fork with a regular (non-force) push so the pre-rebase commits exist on `origin` as a safety net:
     ```
     git push origin local
     ```
     If the push is rejected because `origin/local` has diverged from your local tip, stop and ask — do not `--force` or `--force-with-lease` at this stage. A divergence here means the previous rebase's force-push hasn't happened yet or something else is off; resolve it explicitly before continuing.
   - Re-run `git status --porcelain` to confirm the tree is clean before proceeding.
   - If step 1's initial status output was already empty, skip this whole step.

2. Capture the current `v2` tip so we can diff afterward:
   ```
   git rev-parse v2
   ```
   Save the SHA — call it `OLD_V2`.

3. Run the update + build script with launching disabled:
   ```
   NO_LAUNCH=1 ./tools/update_build_and_run.sh
   ```
   - On rebase conflict, follow CLAUDE.md guidance: keep `com.gryphline.endfield.gp` in `OpenGame.json`, merge upstream's OCR pattern additions alongside the existing English regexes (`Monthly Pass daily rewards`, `confirm Monthly Pass`). Then `git add` and `git rebase --continue`. Re-run this command after.
   - Signing is disabled in this repo's local git config — never re-enable it or pass `-S`.

4. Force-push the rebased `local` branch to the fork (origin = `CTristan/MaaEnd`, push allowed; upstream is push-blocked):
   ```
   git push --force-with-lease origin local
   ```
   Use `--force-with-lease`, not `--force`. Never push anywhere except `origin`.

5. List the upstream commits that came in:
   ```
   git log --oneline "$OLD_V2..v2"
   ```
   Substitute the captured SHA from step 2 for `$OLD_V2`.

6. Summarize the upstream commits in English. Translate any Chinese commit messages. Group by category — features / fixes / refactors / chores. Call out anything that touches paths I patch on `local` (`assets/resource/pipeline/OpenGame.json`, `agent/go-service/visitfriends/`, anything labeled "Reception Room" / "MFG Cabin" / "Growth Chamber" / "ADB" / monthly card / OCR), since those raise the chance of conflict on the next rebase.

If the range from step 5 is empty, just say "No upstream changes since last update."
