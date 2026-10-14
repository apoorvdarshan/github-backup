#!/usr/bin/env python3
"""Fail when the GitHub Actions backup has no recent successful run."""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API = 'https://api.github.com/repos/' + os.environ.get('GITHUB_REPOSITORY', 'apoorvdarshan/github-backup')


def age_hours(finished_at, now):
    finished = dt.datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
    if finished.tzinfo is None:
        raise ValueError('Timestamp lacks timezone')
    age = (now - finished).total_seconds() / 3600
    if age < -0.1:
        raise ValueError('Backup timestamp is in the future')
    return age


def get(path):
    req = urllib.request.Request(API + path, headers={'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'], 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def main():
    try:
        runs = get('/actions/workflows/backup.yml/runs?status=success&branch=main&per_page=1')['workflow_runs']
        if not runs:
            raise ValueError('No successful backup exists')
        run = runs[0]
        if run['status'] != 'completed' or run['conclusion'] != 'success' or run['head_branch'] != 'main':
            raise ValueError('No completed successful backup')
        age = age_hours(run['updated_at'], dt.datetime.now(dt.timezone.utc))
        if age > 30:
            raise ValueError('Last successful backup is older than 30 hours')
        print(f'GitHub-to-GitLab backup healthy: last successful run {age:.1f} hours ago.')
        return 0
    except urllib.error.HTTPError as exc:
        print(f'::error::Cannot check backup: GitHub API HTTP {exc.code}. Check workflow permissions and GitHub availability.')
    except (urllib.error.URLError, TimeoutError):
        print('::error::Cannot reach GitHub to verify backup freshness.')
    except (ValueError, KeyError, TypeError):
        print('::error::Backup is overdue or its status cannot be verified. Open the backup workflow.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
