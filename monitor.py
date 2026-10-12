#!/usr/bin/env python3
"""Fail when the independent GitLab backup has no recent successful run."""
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

API = 'https://gitlab.com/api/v4/projects/87536066'


def age_hours(finished_at, now):
    finished = dt.datetime.fromisoformat(finished_at.replace('Z', '+00:00'))
    if finished.tzinfo is None:
        raise ValueError('Timestamp lacks timezone')
    age = (now - finished).total_seconds() / 3600
    if age < -0.1:
        raise ValueError('Backup timestamp is in the future')
    return age


def get(path):
    req = urllib.request.Request(API + path, headers={'PRIVATE-TOKEN': os.environ['GITLAB_MONITOR_TOKEN']})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def main():
    try:
        pipelines = get('/pipelines?status=success&ref=main&order_by=id&sort=desc&per_page=1')
        if not pipelines:
            raise ValueError('No successful backup exists')
        pipeline = get('/pipelines/' + str(int(pipelines[0]['id'])))
        if pipeline['status'] != 'success' or not pipeline.get('finished_at'):
            raise ValueError('No completed successful backup')
        age = age_hours(pipeline['finished_at'], dt.datetime.now(dt.timezone.utc))
        if age > 30:
            raise ValueError('Last successful backup is older than 30 hours')
        print(f'GitLab backup healthy: last successful run {age:.1f} hours ago.')
        return 0
    except urllib.error.HTTPError as exc:
        print(f'::error::Cannot check GitLab backup: HTTP {exc.code}. Check monitor credential and GitLab availability.')
    except (urllib.error.URLError, TimeoutError):
        print('::error::Cannot reach GitLab to verify backup freshness.')
    except (ValueError, KeyError, TypeError):
        print('::error::GitLab backup is overdue or its status cannot be verified. Open the private backup manager pipeline.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
