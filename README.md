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
- Branches and tags deleted on GitHub are deleted from the backup too. Source
  renames are matched by stable GitHub repository ID. Unrelated destinations are
  never overwritten or deleted. A new source whose name is still used in GitLab is
  stored as `<name>-github-<source ID>`.
- When a GitHub repository is deleted, its GitLab backup is deleted too, but only
  after GitHub confirms it: the stable repository ID must return HTTP 404. Renamed
  and transferred repositories still resolve, so their backups stay. GitLab.com
  keeps a deleted project read-only and restorable for 30 days, renamed to
  `<path>-deletion_scheduled-<project ID>` so a recreated repository can reuse
  the name. If the source comes back within that window, the job restores the
  backup itself. A backup you [restore by hand](https://docs.gitlab.com/user/project/working_with_projects/#restore-a-project)
  is deleted again by the next run unless you also restore the GitHub repository
  or remove `Source repository ID: N.` from the GitLab project's description.
- If GitHub reports more than 10 source repositories deleted in one run (usually
  a narrowed token or a GitHub incident), nothing is deleted and the run fails. If
  those deletions were intended, run the backup workflow manually with a higher
  `max_deletions`.
- Before replacing an existing branch/tag, the old object is saved under a
  `backup-history/<UTC run>/<ref hash>-<old object>` recovery tag. Leases prevent
  overwriting concurrent destination changes. Recovery tags have no automatic
  expiry; monitor storage and prune only after deliberate review. Deleted
  branches/tags get no recovery tag. Before deleting, the job re-reads the source,
  and it never deletes refs for a source that lists no branches or tags.
- Git refs are verified against GitHub after each write. LFS fetch/upload must
  succeed before new refs are published. Failures fail the pipeline; per-repository
  results are retained as GitHub Actions artifacts for 14 days.
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
settings, or local uncommitted files. A revoked GitHub token fails the run before
anything is deleted. A narrowed token can make private repositories look deleted,
because GitHub answers 404 for both: up to the deletion limit their backups are
deleted (restorable in GitLab, and restored by the job once the token sees them
again); beyond it, nothing is deleted and the run fails.

The GitLab projects remain backup storage. GitLab hosted runners are no longer
needed after migration. This controller repository is **public**, so its
standard GitHub-hosted runner jobs use free public-repository Actions minutes.
Its workflow logs and report artifacts are public too: they list source repository
names (including private ones), GitHub IDs, and private GitLab destination links,
but never source code, file contents, or credentials. The GitLab backup projects
stay private. Do not enable paid Actions usage automatically. Two workers limit
simultaneous temporary clones; source code is never executed, and each temporary
clone is removed after use.

Repository secrets are never passed to pull requests from forks, and workflows
from all external contributors require approval before they run. No workflow uses
`pull_request_target`, and third-party actions are pinned to full commit SHAs that
Dependabot keeps current.

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
- `.github/workflows/backup.yml`: daily backup plus manual dispatch (optional
  `max_deletions` input, default 10), serialized by a concurrency group; active
  runs are never canceled by a newer dispatch.
- `.github/workflows/check.yml`: freshness check every six hours and manual dispatch.
- The legacy GitLab manager project `87536066` is retained for rollback. Its
  schedule `4487882` and hosted runners are disabled after the first GitHub backup
  and health check both pass. Runtime CI variables are then removed there.
- The obsolete GitLab watchdog PAT and GitHub `GITLAB_MONITOR_TOKEN` secret are
  removed after the new health workflow is verified.
- Source IDs identify destinations across source renames; no second initial copy
  is needed. Older bundle versions remain retained. Recovery refs are saved before
  any rewritten source tip replaces the destination, and leased pushes protect
  against concurrent changes.
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

Changes to `main` require a pull request and a passing **Backup and monitor tests**
check on an up-to-date branch. No reviewer approval is required for this sole-owner
repository. Branch protection also blocks force pushes and deletion of `main`,
including for administrators. The test workflow receives no backup secrets and
does not perform live backups. Repository visibility is a separate setting.

Run `python3 -m unittest discover -s tests -v`. Tests exercise actual local Git
pushes, rewritten-history recovery, deleted-ref mirroring, bundle restore with a
malformed historical commit, corrupted archive detection, credential isolation,
redirect authentication, and fresh/stale/missing workflow checks. LFS transport
is verified by real cloud backup runs rather than mocked local tests.

A successful live backup requires `report.json` to contain one successful result
per source repository, no `deletion_errors`, and `complete: true`. The public
Actions artifact records source IDs, destination links, backup format, the Scrapy
bundle package version, and `deleted_missing_sources` (backups deleted in that run).

## License

[MIT](LICENSE)
