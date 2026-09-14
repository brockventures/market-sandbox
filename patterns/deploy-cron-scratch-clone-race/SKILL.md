---
title: Never push other work through a cron's persistent scratch clone
author: amos
verified_by: null
scar_level: silent
triggers: [deploy cron, scratch clone, fast-forward guard, git push through automation clone, persistent working copy]
pr_evidence: ["https://github.com/brockventures/market-sandbox/pull/46"]
---

## Problem

A cron that deploys on green CI keeps its own persistent local clone (here,
the deploy cron's `repos/market-sandbox`) and fast-forward-guards it: pull,
check `HEAD` actually moved, deploy only then. That guard assumes the clone
only ever gets a `git pull` from `main`.

On 2026-09-13 I needed to check out and push a PR branch
(`.github/workflows/deploy.yml`, PR #46) and reused that same scratch
clone instead of a fresh one — it already had the remote configured, and
that felt like the path of least resistance. Pushing my branch through it
moved its working tree off `main`. The cron's next fast-forward pull found
nothing to fast-forward (HEAD was already on my branch, not behind
`main`), silently skipped the deploy, and production sat ~10 minutes stale
with no error anywhere — the guard was designed to prevent a bad deploy,
not detect that its own precondition (clone tracks `main`, always) had
been broken by something other than the cron itself.

## Why it's silent

The fast-forward guard's failure mode when its assumption breaks is "no
new commits to deploy," which is indistinguishable from the correct,
common case of "nothing changed." Nothing logs as an error. The only tell
is production timestamps lagging `main`.

## Fix

Never write to a cron's own persistent working clone for anything other
than what the cron itself does with it. Any one-off checkout-and-push —
opening a PR branch, testing a rebase, anything exploratory — uses its own
ephemeral clone in a temp directory, used once and discarded. See
`recipe.py`.

If a persistent clone's purpose is narrow (pull `main`, deploy), treat that
as an invariant worth asserting, not just assuming: the recipe below
checks the clone is on the expected branch before doing anything with it,
and refuses instead of guessing.
