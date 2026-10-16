# KB sync

Jesinia keeps the KB's git repository committed and pushed (ADR 0006).

## Detection

On start and whenever the KB folder changes:

| Found | Mode |
|---|---|
| `.git` with an upstream | **push**: commit and push |
| `.git` without a remote or upstream | **commit**: local commits only |
| no `.git` | **off**: Settings offers *Initialize git* |

Detached HEAD, or a rebase or merge in progress, blocks the cycle. The status explains
why, and the repository is left alone.

## The cycle

Every 4 hours (`kbsync.interval`), on *Sync now* (Settings, `jesinia sync now`, the
`kb_sync_now` tool), and once at shutdown:

1. Clean tree and nothing to push: skip, with no network access.
2. Wait until the KB has been quiet for 30 s (`kbsync.quiet`). If the folder changed or
   a rebase started meanwhile, stop.
3. Stage everything except `*.jesinia-tmp` (retried once if a write lands mid-add).
4. Commit with a summary (`kb: 2 updated, 1 added (Notes/…)`) and the trailer
   `Jesinia-Auto-Sync: true`.
5. In push mode, `git pull --rebase --autostash`, then `git push`. Never forced.

Failures back off: 5 min, then 10, 20, and so on, up to the interval. Errors are recorded
with credentials redacted.

## When sync pauses (a conflict)

A rebase conflict, or files left conflicted by the autostash, pauses sync. Every app
shows a *Sync paused* banner. To fix it:

```bash
cd "<your KB folder>"
git status                      # see the conflicted files
git pull --rebase               # replay your commits on the remote
# edit each conflicted file, keep what you want, remove the <<<<<<< >>>>>>> markers
git add <file>…
git rebase --continue           # repeat until the rebase finishes
git push
```

Then choose **Resume** in the banner or in Settings → Git sync. Jesinia never
auto-resolves and never force-pushes.

## Credentials

Git uses your own credentials: ssh-agent (the LaunchAgent gets `SSH_AUTH_SOCK`) or the
Keychain helper. `jesinia doctor` runs `git ls-remote` to check that the remote is
reachable.
