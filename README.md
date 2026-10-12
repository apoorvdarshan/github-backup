# GitHub backup

Private, cloud-only backups of all repositories owned by `apoorvdarshan` on GitHub,
including forks, archived repositories, and private repositories. GitHub Actions hosted
runners run the scheduled job; no Mac, local cron, or self-hosted runner is needed.

## Behavior

- Daily schedule: `47 20 * * *` UTC, about 02:17 Asia/Kolkata the next day
  (GitHub can queue/delay runs).
  New repositories are discovered automatically by paginating
  the GitHub API. API identity and destination personal namespace are checked before writing.
- Each source has a separate **private** project directly under `gitlab.com/apoorvdarshan`.
  GitHub visibility, settings, source code, and notifications are never changed.
- All branches, tags, reachable Git history, and Git LFS objects are copied.
  Unchanged repositories skip cloning. Source code is never checked out or run.
- Original commit IDs and signatures are preserved. GitLab's Verified badge is
  platform-specific: a GitHub-signed commit can remain Unverified on GitLab.
  Backup jobs never rewrite or re-sign source history to change that badge.
- Issues, merge requests, CI, wiki, packages, and routine emails are disabled on
  backup projects, except the private package registry for the approved Scrapy
  bundle below. This GitHub repository alone runs backup automation.
- Missing source repositories and deleted branches/tags are retained. Source
  renames are matched by stable GitHub repository ID. Unrelated destinations are
  never overwritten.
- Before replacing an existing branch/tag, the old object is saved under a
  `backup-history/<UTC run>/<ref hash>-<old object>` recovery tag. Leases prevent
  overwriting concurrent destination changes. Recovery tags have no automatic
  expiry; monitor storage and prune only after deliberate review.
- Git refs are verified against GitHub after each write. LFS fetch/upload must
  succeed before new refs are published. Failures fail the pipeline; per-repository
  results are retained as private GitHub Actions artifacts for 14 days.
- If GitLab accepts refs but the push connection fails, a separate read must
  confirm every required ref before the repository is counted as successful.
  Timed-out Git processes and their helpers are terminated together.
- A tag matching GitLab's default branch is handled by temporarily selecting
  another original source branch during the push, then restoring the default.
  If no suitable alternate exists, the backup fails explicitly rather than
  silently omitting or renaming the tag.

## Scope and limits

This is a Git-data backup, not a full GitHub account export. It does not copy issues,
PR discussions, release assets, wiki repositories, packages, GitHub Actions history,
settings, or local uncommitted files. A revoked/narrowed GitHub token can hide repos;
missing known sources are listed in the report and never removed from GitLab.

The GitLab projects remain backup storage. GitLab hosted runners are no longer
needed after migration. This controller stays **private**: its reports and logs
contain private repository metadata. Its standard GitHub runner jobs consume the
account's private-repository Actions allowance. Do not make it public or enable
paid usage automatically. Two workers limit simultaneous temporary clones; source
code is never executed, and each temporary clone is removed after use.

## Credentials and alerts

Two existing dedicated credentials are stored as GitHub Actions repository secrets:

- `SOURCE_GITHUB_TOKEN`: the read-only GitHub source token. Passed to the backup
  script as `GITHUB_TOKEN`; it covers all repositories owned by the account.
- `GITLAB_BACKUP_TOKEN`: the personal GitLab API token used to create and update
  private destinations. Passed to the script as `GITLAB_TOKEN`. Its existing
  expiration is **2027-10-12**. Rotation requires updating this secret.

The existing token display names remain accurate: `gitlab-backup-readonly` and
`github-backup-personal-cloud`. They describe the destination and purpose, and
neither token needs rotation merely because the runner moves to GitHub.

GitHub supplies a separate short-lived `GITHUB_TOKEN` for checking out this
controller with read-only contents permission. The health workflow uses its own
automatic token with `actions: read` to check the latest successful `backup.yml`
run every six hours. It fails when that run is missing, unverifiable, or older
than 30 hours. It does not need a long-lived GitLab monitoring token.

Use GitHub's existing failed-workflow email notifications. The backup workflow
fails when any repository fails; the health workflow detects missing/stale runs.
Both now depend on GitHub Actions, so the health check is no longer an independent
provider monitor and cannot alert during a GitHub-wide outage. GitLab retains the
last completed copies even when GitHub is unavailable.

## Deployment and migration

- Controller: `apoorvdarshan/github-backup` (renamed from `github-backup-monitor`).
- GitLab personal namespace ID: `126304343`. All backup projects remain private.
- `.github/workflows/backup.yml`: daily backup plus manual dispatch, serialized
  by a concurrency group; active runs are never canceled by a newer dispatch.
- `.github/workflows/check.yml`: freshness check every six hours and manual dispatch.
- The legacy GitLab manager project `87536066` is retained for rollback. Its
  schedule `4487882` and hosted runners are disabled after the first GitHub backup
  and health check both pass. Runtime CI variables are then removed there.
- The obsolete GitLab watchdog PAT and GitHub `GITLAB_MONITOR_TOKEN` secret are
  removed after the new health workflow is verified.
- Source IDs identify destinations across source renames; no second initial copy
  is needed. Missing source repositories, deleted refs, and older bundle versions
  remain retained. Recovery refs are saved before any rewritten source tip replaces
  the destination, and leased pushes protect against concurrent changes.
- The local checkout is only for development. No Mac service, cron, or local
  checkout is required for normal operation.

## Restore

Clone the desired backup repository and choose its current branch or a recovery
tag. Recovery tags preserve prior tips, including rewritten history. To migrate
development to GitLab, disable the backup schedule first, then restore the desired
project features and update your Git remotes. Never develop in an active backup.

### Scrapy bundle exception

The Scrapy fork (GitHub source ID `1287169174`) contains an old malformed author
email that GitLab rejects during both Git push and native import. With the owner's
approval, it is stored as an exact Git bundle in that private project's package
registry instead. Its GitLab repository stays empty; it is not a normal Git mirror.
No commits are rewritten or re-signed.

Each distinct set of source branches/tags gets an immutable `git-history` package
version derived from their SHA-256 digest. The job uploads `repository.bundle`,
plus `lfs.tar` if LFS objects exist. It downloads and checks their SHA-256 hashes,
actually restores the downloaded bundle, and compares all original refs before
publishing `manifest.json` as the completion marker. Later unchanged runs check
the manifest and GitLab's stored file checksums. Previous versions are retained.

Open the Scrapy project's **Deploy > Package registry**, choose the package version
recorded in the latest backup report, and download its files. Check the SHA-256
values against `manifest.json`, then restore without checking out source code:

```sh
git -c fetch.fsckObjects=false clone --mirror repository.bundle scrapy.git
```

The special clone option permits the already-known malformed history; original
object IDs remain intact. If `lfs.tar` is present, extract its `objects` directory
under `scrapy.git/lfs/`. To move to a host that rejects the malformed commit, an
exact normal push will still fail; keep the bundle or explicitly choose a separate
history-repair migration. Never mistake a rewritten migration for this exact backup.

## Validation

Run `python3 -m unittest discover -s tests -v`. Tests exercise actual local Git
pushes, rewritten-history recovery, deleted-ref retention, bundle restore with a
malformed historical commit, corrupted archive detection, credential isolation,
redirect authentication, and fresh/stale/missing workflow checks. LFS transport
is verified by real cloud backup runs rather than mocked local tests.

A successful live backup requires `report.json` to contain one successful result
per source repository and `complete: true`. The private Actions artifact records
source IDs, destination links, backup format, and the Scrapy bundle package version.
