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

2. Capture the current `v2` tip *and* the current MaaEndTestset `main` tip so we can diff afterward:
   ```
   git rev-parse v2
   git -C tests/MaaEndTestset rev-parse main
   ```
   Save the SHAs — call them `OLD_V2` and `OLD_TESTSET`.

3. Run the update + build script with launching disabled:
   ```
   NO_LAUNCH=1 ./tools/update_build_and_run.sh
   ```
   - On rebase conflict, follow CLAUDE.md guidance: keep `com.gryphline.endfield.gp` in `OpenGame.json`, merge upstream's OCR pattern additions alongside the existing English regexes (`Monthly Pass daily rewards`, `confirm Monthly Pass`). Then `git add` and `git rebase --continue`. Re-run this command after.
   - The script syncs all submodules to the SHAs MaaEnd/v2 pins (`git submodule update --init --recursive`). `MaaUtils` and `assets/resource/model` stay at those pinned SHAs — we don't track their upstreams independently, because MaaEnd/v2's CI validates specific submodule SHAs against the v2 code and we want to stay in that tested combo.
   - Signing is disabled in this repo's local git config — never re-enable it or pass `-S`.

4. Rebase `MaaEndTestset`'s patches onto its own upstream. This is the one submodule we carry patches on (`origin` = `CTristan/MaaEndTestset`, `upstream` = `MaaEnd/MaaEndTestset`, push-blocked).
   ```
   git -C tests/MaaEndTestset fetch upstream main
   git -C tests/MaaEndTestset checkout main
   git -C tests/MaaEndTestset rebase upstream/main
   git -C tests/MaaEndTestset push --force-with-lease origin main
   ```
   - Use `--force-with-lease`, not `--force`. Never push to `upstream`; its push URL is `no_push`.
   - If the rebase conflicts, resolve inside `tests/MaaEndTestset` and `git -C tests/MaaEndTestset rebase --continue`. Conflict likelihood is low — MaaEndTestset churn is roughly 1 commit/month and our patches are screenshots/fixtures, not code.
   - If `upstream/main` is unchanged or already contained in `main`, the rebase and push are no-ops — continue.

5. If MaaEndTestset's SHA advanced, commit the pointer bump on the superproject so it survives the next rebase. From the superproject root:
   ```
   git status --porcelain tests/MaaEndTestset
   ```
   If non-empty, stage and commit the bump:
   ```
   git add tests/MaaEndTestset
   git commit -m "local: bump MaaEndTestset (rebase onto upstream/main)"
   ```
   If empty, skip this step.

6. Force-push the rebased `local` branch to the fork (origin = `CTristan/MaaEnd`, push allowed; upstream is push-blocked):
   ```
   git push --force-with-lease origin local
   ```
   Use `--force-with-lease`, not `--force`. Never push anywhere except `origin`.

7. List the upstream commits that came in:
   ```
   git log --oneline "$OLD_V2..v2"
   git -C tests/MaaEndTestset log --oneline "$OLD_TESTSET..main"
   ```
   Substitute the captured SHAs from step 2. In the MaaEndTestset range, one commit will be the rebased local patch — the rest are the upstream changes that landed. Also note any bumped submodule SHAs by running `git diff "$OLD_V2" v2 -- .gitmodules` and `git submodule status` if you want the full picture of what MaaEnd/v2 moved.

8. Summarize the upstream commits in English. Translate any Chinese commit messages. Group by category — features / fixes / refactors / chores. Call out anything that touches paths I patch on `local` (`assets/resource/pipeline/OpenGame.json`, `agent/go-service/visitfriends/`, anything labeled "Reception Room" / "MFG Cabin" / "Growth Chamber" / "ADB" / monthly card / OCR), since those raise the chance of conflict on the next rebase. Include a line noting any new MaaUtils / model / MaaEndTestset SHAs if they advanced.

If the range from step 7 is empty *and* MaaEndTestset had no upstream changes, just say "No upstream changes since last update."
