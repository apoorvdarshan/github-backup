# GitLab backup watchdog

Independent cloud-only freshness check for the private GitLab
[backup manager](https://gitlab.com/apoorvdarshan/github-backup-manager).
GitHub Actions checks every six hours, and fails if the latest successful backup
is older than 30 hours, unavailable, or cannot be authenticated. Detection is
therefore normally within 30–36 hours of the last success, plus scheduler delays.

The GitHub Actions secret `GITLAB_MONITOR_TOKEN` contains a dedicated read-only
GitLab API token. It cannot push code or modify GitLab. No source repository token
is needed. No Mac, local process, or self-hosted runner is required.

Use GitHub's failed-workflow notifications for alerts. A green check is quiet when
Actions notifications are set to failed workflows only. This monitor cannot send
alerts if GitHub itself is unavailable; the GitLab backup job separately reports
GitHub read failures via its pipeline email integration. Neither service can
guarantee notification delivery during a simultaneous outage.

This private repository is itself included in the next automatic GitLab backup.
No existing GitHub repository or notification setting is modified by this code.

Run `python3 -m unittest -v` to validate fresh, stale, missing, and invalid timestamps.
