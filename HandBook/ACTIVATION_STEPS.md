# Activation Steps

Everything I wrote is **already pushed** to
`arena/019fc5c7-autonomous-bursa-agentv3-3` (commit `f92aeb3`).

The only thing left is the two workflow files. They could not be pushed from my
session — GitHub rejects workflow files from an App without the `workflows`
permission:

```
! [remote rejected] refusing to allow a GitHub App to create or update
  workflow `.github/workflows/keepalive.yml` without `workflows` permission
```

So they're parked in `scripts/` and you move them with one command. This is a
permissions boundary, not a broken file — the YAML is validated and correct.

---

## Step 1 — Get the branch locally

```bash
git fetch origin
git checkout arena/019fc5c7-autonomous-bursa-agentv3-3
git pull
```

---

## Step 2 — Activate the two workflows

```bash
git mv scripts/keepalive.workflow.yml      .github/workflows/keepalive.yml
git mv scripts/trading-cycle.workflow.yml  .github/workflows/trading-cycle.yml

git commit -m "ci: enable keepalive + headless trading cycle"
git push origin arena/019fc5c7-autonomous-bursa-agentv3-3
```

This push comes from **you**, not an App, so it goes through.

> If you'd rather not use the CLI: open each `scripts/*.workflow.yml` on GitHub,
> copy the contents, and create the file at the `.github/workflows/` path through
> the web UI. Same result.

---

## Step 3 — Add secrets and variables

**Settings → Secrets and variables → Actions**

Under the **Secrets** tab:

| Name | Value |
|---|---|
| `GIST_TOKEN` | A **classic** PAT with the `gist` scope — [create one](https://github.com/settings/tokens). Must be classic, not fine-grained. |

Under the **Variables** tab:

| Name | Value |
|---|---|
| `STREAMLIT_APP_URL` | `https://your-app.streamlit.app` |
| `GIST_ID` | The id of the gist your app already backs up to |

**Finding your `GIST_ID`:** it's shown in the app's Settings tab, or it's the
hex string at the end of the gist URL:
`https://gist.github.com/fongway94/`**`a1b2c3d4e5f6...`**

Use the **same gist the Streamlit app already uses** — that's what keeps both
looking at one brain.

---

## Step 4 — Test before trusting the cron

**Actions** tab → run each workflow manually via **Run workflow**.

1. **Keepalive** — run it. Expect `OK: app is awake and rendered`.
2. **Trading cycle** — run it with **`dry_run` = true** first. This restores the
   brain and prints your position/prior counts without trading. Confirm the
   restore line shows a sensible byte count and that the state matches what the
   Streamlit app shows.
3. Only after a clean dry run, let the cron drive real cycles.

---

## Step 5 — Clear the stale brain

Your existing priors, WFO parameters and ML classifier were all fitted under
findings #1 and #3 (wrong training universe, contaminated EMA200). They are
**not valid** on the corrected pipeline, and averaging good new data against bad
old posteriors just slows the recovery.

In the app: **Learning tab** → clear state priors / retrain. Then let the noop
period repopulate from scratch.

---

## Step 6 (optional, later) — Make Streamlit read-only

Once you trust the headless runner, stop the app from also scheduling cycles.
Both write to the same gist and the workflow's concurrency group prevents two
cycles racing, so **running both for a while is safe** — do that first.

To retire the in-app scheduler, drop the `sched.ensure_started(...)` call around
`app.py:290`. After that the app is purely a viewer, and keepalive is only for
dashboard convenience rather than for the agent to function.

---

## Opening a PR (optional)

The branch is a normal branch — merge it whenever you're ready:

```bash
gh pr create --base main \
  --head arena/019fc5c7-autonomous-bursa-agentv3-3 \
  --title "Fix training universe, gate partial scans, decouple brain from Streamlit" \
  --fill
```

CI (`tests.yml`) runs on the PR. Expect **645 passed, 8 failed** — those 8 are
pre-existing failures in `test_live_trigger.py` and `test_moomoo_us_adapter.py`
that also fail on `main` at `c525dc9`; they came from the earlier
`trigger_filter` commit and are unrelated to this work.
