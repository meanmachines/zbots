# Vendored hermes-agent snapshot

`vendor/hermes-agent/` is a **frozen source snapshot** of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
vendored as a static file tree — no `.git` directory, no upstream git
remote, no auto-update. This is deliberate: hermes-agent moves at roughly
40 commits/day with zero tagged releases, so a frozen pin is the only
stable foundation for a distributable product.

- **Pinned commit:** `0cde4dd93aa794c65fee6cc85b0b5e4eee77e8e2`
- **Commit date:** 2026-08-22T17:38:27Z (author: Teknium)
- **Vendored on:** 2026-08-22
- **Source:** `https://github.com/NousResearch/hermes-agent/tree/0cde4dd93aa794c65fee6cc85b0b5e4eee77e8e2`

Original license (MIT, © 2025 Nous Research) is preserved at
`vendor/hermes-agent/LICENSE`, unmodified.

## Updating this snapshot

Do not `git pull` or re-clone over this directory casually — updates
happen deliberately, via a quarterly manual upstream-diff cycle (see the
project's own roadmap for the full policy), not automatically. To refresh:

1. Pick a new commit from `NousResearch/hermes-agent`'s `main` branch.
2. Download that commit's source tree fresh (do not merge/rebase git
   history — this directory has none by design).
3. Diff the new snapshot against the old one to see what changed upstream.
4. Re-apply any local patches/workarounds zBots carries on top of this
   vendored code (check `backend/` for anything that assumes specific
   upstream behavior).
5. Update this file's pinned commit hash and date.
