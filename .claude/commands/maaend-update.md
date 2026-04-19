---
description: Pull from upstream/v2, rebase local patches onto it, build (no launch), force-push to fork, and summarize upstream changes in English.
allowed-tools: Bash
---

Run the MaaEnd daily update flow. Do not launch MXU at the end.

1. Capture the current `v2` tip so we can diff afterward:
   ```
   git rev-parse v2
   ```
   Save the SHA — call it `OLD_V2`.

2. Run the update + build script with launching disabled:
   ```
   NO_LAUNCH=1 ./tools/update_build_and_run.sh
   ```
   - On rebase conflict, follow CLAUDE.md guidance: keep `com.gryphline.endfield.gp` in `OpenGame.json`, merge upstream's OCR pattern additions alongside the existing English regexes (`Monthly Pass daily rewards`, `confirm Monthly Pass`). Then `git add` and `git rebase --continue`. Re-run this command after.
   - Signing is disabled in this repo's local git config — never re-enable it or pass `-S`.

3. Force-push the rebased `local` branch to the fork (origin = `CTristan/MaaEnd`, push allowed; upstream is push-blocked):
   ```
   git push --force-with-lease origin local
   ```
   Use `--force-with-lease`, not `--force`. Never push anywhere except `origin`.

4. List the upstream commits that came in:
   ```
   git log --oneline "$OLD_V2..v2"
   ```
   Substitute the captured SHA from step 1 for `$OLD_V2`.

5. Summarize the upstream commits in English. Translate any Chinese commit messages. Group by category — features / fixes / refactors / chores. Call out anything that touches paths I patch on `local` (`assets/resource/pipeline/OpenGame.json`, `agent/go-service/visitfriends/`, anything labeled "Reception Room" / "MFG Cabin" / "Growth Chamber" / "ADB" / monthly card / OCR), since those raise the chance of conflict on the next rebase.

If the range from step 4 is empty, just say "No upstream changes since last update."
