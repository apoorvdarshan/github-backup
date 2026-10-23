import os
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import backup


def backup_project(project_id, source_id, **fields):
    description = f'Private backup of https://github.com/{backup.OWNER}/example. Development stays on GitHub. Source repository ID: {source_id}.'
    return {'id': project_id, 'path': f'example-{project_id}', 'name': f'example-{project_id}', 'web_url': f'https://gitlab.example.invalid/example-{project_id}',
            'description': description, 'visibility': 'private', 'namespace': {'id': int(backup.NAMESPACE_ID)}, **fields}


def listed_repo(source_id):
    return {'id': source_id, 'name': f'repo-{source_id}', 'full_name': f'{backup.OWNER}/repo-{source_id}', 'owner': {'login': backup.OWNER}}


def deletion_api(resolvable=(), failing=()):
    """Fake api() for deletion checks that follows the real GitHub/GitLab routing contract."""
    calls = []
    def api(method, path, data=None, github=False, missing_ok=False):
        calls.append((method, path, github, missing_ok))
        if path.startswith('/repositories/'):
            # GitLab answers 404 for this route too, so a misrouted check would delete everything.
            if not github:
                raise AssertionError('Repository lookups must go to GitHub')
            source_id = int(path.rsplit('/', 1)[1])
            if source_id in failing:
                raise backup.BackupError('GitHub API GET failed: HTTP 500')
            if source_id in resolvable:
                return {'id': source_id}
            if not missing_ok:
                raise backup.BackupError('GitHub API GET failed: HTTP 404')
            return None
        if github or method != 'DELETE':
            raise AssertionError(f'Unexpected API call {method} {path}')
        return {'message': '202 Accepted'}
    return api, calls


def run_main(repos, listings, lookup, limit='10'):
    """Run main() against fake APIs; return its exit code, report, and processed repositories."""
    def api(method, path, data=None, github=False, missing_ok=False):
        if path == '/user':
            return {'login': backup.OWNER} if github else {'username': backup.NAMESPACE_PATH, 'id': 5}
        if path.startswith('/namespaces/'):
            return {'full_path': backup.NAMESPACE_PATH, 'kind': 'user'}
        return lookup(method, path, data, github, missing_ok)
    listings = iter(listings)
    def pages(path, github=False):
        return repos if github else next(listings)
    processed = []
    def process(repo, existing):
        processed.append((repo['id'], existing))
        return {'repository': repo['full_name'], 'status': 'synced'}
    with tempfile.TemporaryDirectory() as tmp:
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            with patch.dict(os.environ, BACKUP_MAX_DELETIONS=limit), patch.object(backup, 'api', side_effect=api), \
                    patch.object(backup, 'pages', side_effect=pages), patch.object(backup, 'process', side_effect=process), \
                    patch('builtins.print'):
                code = backup.main()
            return code, json.loads(Path('report.json').read_text()), processed
        finally:
            os.chdir(cwd)



def local_repositories(root):
    """A source checkout and a destination bare repository for real local pushes."""
    env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_COMMITTER_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_EMAIL='test@example.invalid')
    original = backup.command
    def git(*args, cwd=None):
        return original(['git', *args], cwd=cwd, env=env).strip()
    src, dst = root / 'source', root / 'destination.git'
    git('init', '-b', 'master', str(src))
    git('init', '--bare', str(dst))
    git('symbolic-ref', 'HEAD', 'refs/heads/master', cwd=dst)
    git('commit', '--allow-empty', '-m', 'original', cwd=src)
    return src, dst, env, git


def fake_gitlab(dst, env, git, calls):
    """api() that, like GitLab, only switches to an existing default branch and moves HEAD."""
    def api(method, path, data=None):
        refs = backup.remote_refs(str(dst), env)
        calls.append((method, path, data, 'refs/heads/master' in refs))
        if data:
            if 'refs/heads/' + data['default_branch'] not in refs:
                raise backup.BackupError('GitLab API PUT failed: HTTP 400')
            git('symbolic-ref', 'HEAD', 'refs/heads/' + data['default_branch'], cwd=dst)
    return api

class SafetyTests(unittest.TestCase):
    def test_package_redirect_does_not_forward_gitlab_token(self):
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                if len(requests) == 1:
                    raise backup.urllib.error.HTTPError(request.full_url, 302, 'redirect', {'Location': 'https://storage.example.invalid/file'}, None)
                return io.BytesIO(b'archive')
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, GITLAB_TOKEN='test-only-secret'), patch.object(backup.urllib.request, 'build_opener', return_value=Opener()):
                backup.package_file('GET', '/projects/1/packages/generic/test/1/file', Path(root) / 'file')
        self.assertEqual(requests[0].get_header('Private-token'), 'test-only-secret')
        self.assertIsNone(requests[1].get_header('Private-token'))

    def test_remote_diagnostics_redact_credentials_and_urls(self):
        secret = 'test-credential-value'
        env = dict(os.environ, BACKUP_GIT_TOKEN=secret)
        script = "import os,sys; sys.stderr.write('error: denied '+os.environ['BACKUP_GIT_TOKEN']+' https://example.invalid/private\\n'); sys.exit(1)"
        with self.assertRaises(backup.BackupError) as result:
            backup.command([sys.executable, '-c', script], env=env)
        self.assertNotIn(secret, str(result.exception))
        self.assertNotIn('https://', str(result.exception))
        self.assertIn('[redacted]', str(result.exception))

    def test_timeout_also_terminates_helpers_holding_output_pipes(self):
        script = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)']); time.sleep(4)"
        started = time.monotonic()
        with self.assertRaisesRegex(backup.BackupError, 'timed out'):
            backup.command([sys.executable, '-c', script], timeout=0.2)
        self.assertLess(time.monotonic() - started, 2)

    def test_shards_cover_every_repository_exactly_once(self):
        repos = [{'id': i} for i in range(319)]
        shards = [backup.assigned_repositories(repos, i, 4) for i in range(1, 5)]
        ids = [r['id'] for shard in shards for r in shard]
        self.assertEqual(sorted(ids), list(range(319)))
        for i, shard in enumerate(shards, 1):
            self.assertEqual(shard, backup.assigned_repositories(list(reversed(repos)), i, 4)[::-1])
        with self.assertRaises(backup.BackupError):
            backup.assigned_repositories(repos, 0, 4)

    def test_runner_credentials_cannot_override_backup_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'credential.helper', 'GIT_CONFIG_VALUE_0': 'injected', 'GITHUB_TOKEN': 'source-secret', 'GITLAB_TOKEN': 'dest-secret'}):
                env = backup.git_env('expected-secret', root)
                self.assertNotIn('GIT_CONFIG_COUNT', env)
                self.assertNotIn('GIT_CONFIG_VALUE_0', env)
                self.assertNotIn('GITHUB_TOKEN', env)
                self.assertNotIn('GITLAB_TOKEN', env)
                self.assertEqual(env['BACKUP_GIT_TOKEN'], 'expected-secret')

    def test_deleted_refs_are_stale_but_recovery_tags_stay(self):
        recovery = backup.RECOVERY + 'run/abc-old'
        destination = {'refs/heads/main': 'a', 'refs/heads/deleted-upstream': 'b', 'refs/tags/gone': 'c', recovery: 'd'}
        self.assertEqual(backup.stale_refs({'refs/heads/main': 'a'}, destination),
                         {'refs/heads/deleted-upstream': 'b', 'refs/tags/gone': 'c'})

    def test_changed_or_empty_source_never_deletes_refs(self):
        destination = {'refs/heads/main': 'a', 'refs/heads/gone': 'b'}
        for first, again in [({'refs/heads/main': 'a'}, {'refs/heads/main': 'c'}), ({}, {})]:
            with patch.object(backup, 'remote_refs', side_effect=[first, destination, again]), \
                    patch.object(backup, 'command') as command, patch.object(backup, 'api') as api:
                with self.assertRaisesRegex(backup.BackupError, 'refusing to delete'):
                    backup.sync_git('source', 'destination', 'unused', {}, {}, project={'id': 1, 'default_branch': 'main'})
            command.assert_not_called()
            api.assert_not_called()

    def test_recovery_for_rewritten_branches_and_tags(self):
        src = {'refs/heads/main': 'new', 'refs/tags/v1': 'newtag'}
        dst = {'refs/heads/main': 'old', 'refs/tags/v1': 'oldtag', 'refs/heads/deleted': 'keep'}
        recovery = backup.recovery_plan(src, dst)
        self.assertEqual(set(recovery.values()), {'old', 'oldtag'})
        self.assertTrue(all(r.startswith(backup.RECOVERY) for r in recovery))

    def test_existing_unrelated_repository_never_adopted(self):
        repo = {'name': 'Example', 'id': 123}
        existing = [{'path': 'example', 'description': 'unrelated'}, {'path': 'example-github-123', 'description': 'also unrelated'}]
        with patch.object(backup, 'api') as api:
            with self.assertRaises(backup.BackupError):
                backup.destination_project(repo, existing)
            api.assert_not_called()

    def test_new_destination_avoids_retained_name_and_path(self):
        repo = {'name': 'Example', 'id': 123}
        retained = {'path': 'example', 'name': 'Example', 'description': 'Source repository ID: 9.'}
        renamed = {'path': 'other', 'name': 'example', 'description': 'unrelated'}
        for existing in ([retained], [renamed]):
            created = []
            def api(method, path, data=None, missing_ok=False):
                created.append(data)
                return {'id': 1, 'description': data['description']}
            with patch.object(backup, 'api', side_effect=api), patch.object(backup, 'pages', return_value=[]):
                backup.destination_project(repo, existing)
            self.assertEqual((created[0]['name'], created[0]['path']), ('Example-github-123', 'example-github-123'))

    def test_failed_destination_creation_reports_api_status(self):
        def api(method, path, data=None, missing_ok=False):
            if method == 'POST':
                raise backup.BackupError('GitLab API POST failed: HTTP 400')
        with patch.object(backup, 'api', side_effect=api):
            with self.assertRaisesRegex(backup.BackupError, 'HTTP 400'):
                backup.destination_project({'name': 'example', 'id': 123}, [])

    def test_only_backups_of_confirmed_deleted_sources_are_deleted(self):
        existing = [backup_project(1, 10), backup_project(2, 20), backup_project(3, 30),
                    backup_project(4, 40, marked_for_deletion_on='2026-10-23'),
                    backup_project(6, 60, path='example-deletion_scheduled-6'),
                    {'id': 5, 'path': 'manager', 'description': 'Source repository ID: 50.'}]
        # Source 30 was renamed or transferred, so GitHub still resolves its ID.
        api, calls = deletion_api(resolvable={30})
        with patch.object(backup, 'api', side_effect=api), patch('builtins.print'):
            deleted, errors = backup.delete_removed_sources([{'id': 10}], existing)
        self.assertEqual((deleted, errors), (['example-2'], []))
        self.assertIn(('GET', '/repositories/20', True, True), calls)
        self.assertEqual([c for c in calls if c[0] == 'DELETE'], [('DELETE', '/projects/2', False, False)])
        for skipped in (10, 40, 50, 60):
            self.assertFalse([c for c in calls if c[1] == f'/repositories/{skipped}'])

    def test_deletion_limit_counts_only_confirmed_deletions(self):
        existing = [backup_project(i, i) for i in range(1, 13)]
        for limit, resolvable, deleted_count in [('10', (), 0), ('0', (), 0), ('12', (), 12), ('10', {1, 2}, 10),
                                                  ('1', set(range(1, 12)), 1)]:
            api, calls = deletion_api(resolvable=resolvable)
            with patch.dict(os.environ, BACKUP_MAX_DELETIONS=limit), patch.object(backup, 'api', side_effect=api), patch('builtins.print'):
                deleted, errors = backup.delete_removed_sources([{'id': 999}], existing)
            self.assertEqual(len(deleted), deleted_count)
            self.assertEqual(len([c for c in calls if c[0] == 'DELETE']), deleted_count)
            self.assertEqual(bool(errors), deleted_count == 0)
        for limit in ('-1', 'all', ''):
            with patch.dict(os.environ, BACKUP_MAX_DELETIONS=limit), patch.object(backup, 'api') as api:
                with self.assertRaises(backup.BackupError):
                    backup.delete_removed_sources([{'id': 999}], existing)
                api.assert_not_called()

    def test_failed_deletion_check_is_reported_and_others_continue(self):
        api, calls = deletion_api(failing={10})
        with patch.object(backup, 'api', side_effect=api), patch('builtins.print'):
            deleted, errors = backup.delete_removed_sources([{'id': 99}], [backup_project(1, 10), backup_project(2, 20)])
        self.assertEqual(deleted, ['example-2'])
        self.assertEqual(len(errors), 1)
        self.assertIn('HTTP 500', errors[0])

    def test_returning_source_restores_scheduled_backup(self):
        project = backup_project(1, 123, marked_for_deletion_on='2026-10-23')
        restored = backup_project(1, 123)
        with patch.object(backup, 'api', return_value=restored) as api:
            self.assertEqual(backup.destination_project({'name': 'example', 'id': 123}, [project]), restored)
        api.assert_called_once_with('POST', '/projects/1/restore')

    def test_main_deletes_before_creating_destinations(self):
        before = [backup_project(1, 99, path='repo-7', name='repo-7')]
        after = [backup_project(1, 99, path='repo-7-deletion_scheduled-1', name='repo-7-deletion_scheduled-1',
                                marked_for_deletion_on='2026-10-23')]
        lookup, calls = deletion_api()
        code, report, processed = run_main([listed_repo(7)], [before, after], lookup)
        self.assertEqual(code, 0)
        self.assertEqual(processed, [(7, after)])
        self.assertEqual(report['deleted_missing_sources'], ['repo-7'])
        self.assertEqual(report['retained_missing_sources'], [])
        self.assertTrue(report['complete'])

    def test_main_fails_without_deleting_when_too_many_sources_vanish(self):
        existing = [backup_project(i, i) for i in range(1, 13)]
        lookup, calls = deletion_api()
        code, report, processed = run_main([listed_repo(99)], [existing], lookup)
        self.assertEqual(code, 1)
        self.assertFalse(report['complete'])
        self.assertIn('refusing', report['deletion_errors'][0])
        self.assertFalse([c for c in calls if c[0] == 'DELETE'])
        self.assertEqual([repo_id for repo_id, _ in processed], [99])
        self.assertEqual(len(report['retained_missing_sources']), 12)

    def test_private_and_quiet_settings(self):
        settings = backup.project_settings({'name': 'example', 'id': 123})
        self.assertEqual(settings['visibility'], 'private')
        for field in ['issues_access_level', 'merge_requests_access_level', 'builds_access_level']:
            self.assertEqual(settings[field], 'disabled')
        self.assertFalse(settings['emails_enabled'])


class GitIntegrationTests(unittest.TestCase):
    def test_bundle_restores_malformed_history_and_checks_stored_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.git'
            env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
            original = backup.command
            original(['git', 'init', '--bare', str(source)], env=env)
            tree = subprocess.run(['git', 'mktree'], cwd=source, input='', text=True, capture_output=True, check=True).stdout.strip()
            raw = f'tree {tree}\nauthor Test <bad<email> 1700000000 +0000\ncommitter Test <test@example.invalid> 1700000000 +0000\n\nOriginal malformed history\n'
            sha = subprocess.run(['git', 'hash-object', '--literally', '-t', 'commit', '-w', '--stdin'], cwd=source, input=raw, text=True, capture_output=True, check=True).stdout.strip()
            original(['git', 'update-ref', 'refs/heads/main', sha], cwd=source, env=env)
            original(['git', 'update-ref', 'refs/tags/original', sha], cwd=source, env=env)
            invalid = subprocess.run(['git', 'fsck', '--strict'], cwd=source, capture_output=True, text=True)
            self.assertNotEqual(invalid.returncode, 0)
            self.assertIn('badEmail', invalid.stderr)
            stored = {}
            corrupt = False
            corrupt_download = False
            def transport(method, path, local, missing_ok=False):
                if method == 'PUT':
                    stored[path] = Path(local).read_bytes()
                elif path in stored:
                    Path(local).write_bytes(b'damaged' if corrupt_download and path.endswith('.bundle') else stored[path])
                elif missing_ok:
                    return False
                else:
                    raise backup.BackupError('Missing test package')
                return True
            def git(args, **kwargs):
                if 'lfs' in args:
                    return ''
                args = [str(source) if a == 'https://github.com/apoorvdarshan/scrapy.git' else a for a in args]
                return original(args, **kwargs)
            def pages(path):
                if '/package_files' in path:
                    return [{'file_name': p.rsplit('/', 1)[1], 'size': len(data),
                             'file_sha256': 'corrupt' if corrupt else hashlib.sha256(data).hexdigest()}
                            for p, data in stored.items()]
                version = next(iter(stored)).split('/')[-2]
                return [{'id': 1, 'name': 'git-history', 'version': version}]
            def sync():
                with tempfile.TemporaryDirectory(dir=root) as work:
                    with patch.object(backup, 'command', side_effect=git), patch.object(backup, 'package_file', side_effect=transport), patch.object(backup, 'pages', side_effect=pages):
                        return backup.sync_bundle({'id': 1287169174, 'name': 'scrapy', 'default_branch': 'main'}, {'id': 123, 'packages_enabled': True}, work, env)
            self.assertEqual(sync()['status'], 'synced')
            manifest = json.loads(next(data for path, data in stored.items() if path.endswith('/manifest.json')))
            self.assertEqual(manifest['refs'], {'refs/heads/main': sha, 'refs/tags/original': sha})
            self.assertEqual(sync()['status'], 'unchanged')
            corrupt = True
            with self.assertRaisesRegex(backup.BackupError, 'checksum'):
                sync()
            stored.clear()
            corrupt = False
            corrupt_download = True
            with self.assertRaisesRegex(backup.BackupError, 'checksum'):
                sync()
            self.assertFalse(any(path.endswith('/manifest.json') for path in stored))

    def test_deleted_branches_and_tags_are_mirrored_without_fetching(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, env, git = local_repositories(root)
            sha = git('rev-parse', 'HEAD', cwd=src)
            for branch in ('main', 'feature'):
                git('branch', branch, cwd=src)
            for tag in ('v1', 'old-tag'):
                git('tag', tag, cwd=src)
            git('push', str(dst), 'refs/heads/*:refs/heads/*', 'refs/tags/*:refs/tags/*', cwd=src)
            recovery = f'{backup.RECOVERY}run/abc-{sha}'
            git('update-ref', recovery, sha, cwd=dst)
            # The source switched its default to main and deleted master, a branch and a tag.
            git('checkout', '-q', 'main', cwd=src)
            git('branch', '-D', 'master', 'feature', cwd=src)
            git('tag', '-d', 'old-tag', cwd=src)
            original, commands, calls = backup.command, [], []
            def record(args, **kw):
                commands.append(args)
                return original(args, **kw)
            project = {'id': 123, 'default_branch': 'master'}
            with tempfile.TemporaryDirectory(dir=root) as work:
                with patch.object(backup, 'command', side_effect=record), patch.object(backup, 'api', side_effect=fake_gitlab(dst, env, git, calls)), \
                        patch.object(backup, 'pages', return_value=[{'name': 'master'}]):
                    result = backup.sync_git(str(src), str(dst), work, env, env, project=project, default_branch='main')
            self.assertEqual((result['status'], result['deleted_refs']), ('synced', 3))
            self.assertEqual(backup.remote_refs(str(dst), env), {'refs/heads/main': sha, 'refs/tags/v1': sha, recovery: sha})
            # The default moves before its old branch is deleted; the destination refuses to delete HEAD.
            self.assertEqual(calls, [('PUT', '/projects/123', {'default_branch': 'main'}, True),
                                     ('DELETE', '/projects/123/protected_branches/master', None, True)])
            self.assertEqual(project['default_branch'], 'main')
            self.assertFalse([c for c in commands if 'fetch' in c])

    def test_renamed_default_is_published_before_the_old_branch_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, env, git = local_repositories(root)
            git('push', str(dst), 'master', cwd=src)
            git('branch', '-m', 'master', 'main', cwd=src)
            original, calls = backup.command, []
            def no_lfs(args, **kw):
                return '' if 'lfs' in args else original(args, **kw)
            with tempfile.TemporaryDirectory(dir=root) as work:
                with patch.object(backup, 'command', side_effect=no_lfs), patch.object(backup, 'api', side_effect=fake_gitlab(dst, env, git, calls)), \
                        patch.object(backup, 'pages', return_value=[]):
                    result = backup.sync_git(str(src), str(dst), work, env, env, project={'id': 123, 'default_branch': 'master'}, default_branch='main')
            self.assertEqual((result['status'], result['deleted_refs']), ('synced', 1))
            self.assertEqual(set(backup.remote_refs(str(dst), env)), {'refs/heads/main'})
            self.assertEqual(calls, [('PUT', '/projects/123', {'default_branch': 'main'}, True)])

    def test_refs_blocking_a_renamed_path_are_deleted_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, env, git = local_repositories(root)
            original = backup.command
            def no_lfs(args, **kw):
                return '' if 'lfs' in args else original(args, **kw)
            def sync():
                with tempfile.TemporaryDirectory(dir=root) as work:
                    with patch.object(backup, 'command', side_effect=no_lfs), patch.object(backup, 'api'), patch.object(backup, 'pages', return_value=[]):
                        return backup.sync_git(str(src), str(dst), work, env, env, project={'id': 123, 'default_branch': 'master'})
            git('branch', 'docs', cwd=src)
            git('tag', 'v1', cwd=src)
            self.assertEqual(sync()['status'], 'synced')
            # Git cannot hold docs and docs/intro (or v1 and v1/intro) at once, in either direction.
            for old, new in [('docs', 'docs/intro'), ('docs/intro', 'docs')]:
                git('branch', '-m', old, new, cwd=src)
                old_tag, new_tag = old.replace('docs', 'v1'), new.replace('docs', 'v1')
                sha = git('rev-parse', f'refs/tags/{old_tag}', cwd=src)
                git('tag', '-d', old_tag, cwd=src)
                git('tag', new_tag, sha, cwd=src)
                self.assertEqual(sync()['status'], 'synced')
                self.assertEqual(backup.remote_refs(str(dst), env), backup.remote_refs(str(src), env))
            self.assertEqual(sync()['status'], 'unchanged')

    def test_deletion_lease_keeps_a_ref_that_changed_meanwhile(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst, env, git = local_repositories(root)
            git('branch', 'gone', cwd=src)
            git('push', str(dst), 'master', 'gone', cwd=src)
            git('branch', '-D', 'gone', cwd=src)
            moved = git('commit-tree', git('rev-parse', 'master^{tree}', cwd=src), '-p', 'refs/heads/gone', '-m', 'moved', cwd=dst)
            original = backup.command
            def move_first(args, **kw):
                # Someone updates the branch between the listing and the deletion.
                if ':refs/heads/gone' in args:
                    git('update-ref', 'refs/heads/gone', moved, cwd=dst)
                return original(args, **kw)
            with tempfile.TemporaryDirectory(dir=root) as work:
                with patch.object(backup, 'command', side_effect=move_first), patch.object(backup, 'api'), patch.object(backup, 'pages', return_value=[]):
                    with self.assertRaises(backup.BackupError):
                        backup.sync_git(str(src), str(dst), work, env, env, project={'id': 123, 'default_branch': 'master'})
            self.assertEqual(backup.remote_refs(str(dst), env)['refs/heads/gone'], moved)

    def test_default_branch_tag_collision_preserves_original_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, dst = root / 'source', root / 'destination.git'
            env = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_COMMITTER_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_EMAIL='test@example.invalid')
            original = backup.command
            def git(*args, cwd=None):
                return original(['git', *args], cwd=cwd, env=env).strip()
            git('init', '-b', 'main', str(src))
            git('init', '--bare', str(dst))
            git('commit', '--allow-empty', '-m', 'original', cwd=src)
            git('branch', 'alternate', cwd=src)
            git('tag', 'main', cwd=src)
            sha = git('rev-parse', 'HEAD', cwd=src)
            subprocess.run(['git', 'update-ref', '--stdin'], cwd=src, env=env,
                           input=''.join(f'create refs/tags/test-{i:04d} {sha}\n' for i in range(600)),
                           text=True, check=True, capture_output=True)
            expected = backup.remote_refs(str(src), env)
            defaults = []
            def update(method, path, data):
                self.assertEqual((method, path), ('PUT', '/projects/123'))
                branch = data['default_branch']
                self.assertIn('refs/heads/' + branch, backup.remote_refs(str(dst), env))
                defaults.append(branch)
            def no_lfs(args, **kw):
                return '' if 'lfs' in args else original(args, **kw)
            with tempfile.TemporaryDirectory(dir=root) as work:
                with patch.object(backup, 'command', side_effect=no_lfs), patch.object(backup, 'api', side_effect=update):
                    result = backup.sync_git(str(src), str(dst), work, env, env, project={'id': 123, 'default_branch': 'main'})
            self.assertEqual(result['status'], 'synced')
            self.assertEqual(defaults, ['alternate', 'main'])
            self.assertEqual(backup.remote_refs(str(dst), env), expected)

    def test_sync_rewrite_delete_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / 'source'
            dst = root / 'destination.git'
            env = dict(os.environ, GIT_AUTHOR_NAME='Backup Test', GIT_COMMITTER_NAME='Backup Test', GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_EMAIL='test@example.invalid')
            def git(*args, cwd=None):
                return backup.command(['git', *args], cwd=cwd, env=env).strip()
            git('init', '-b', 'main', str(src))
            git('init', '--bare', str(dst))
            (src / 'file').write_text('first')
            git('add', '.', cwd=src)
            git('commit', '-m', 'first', cwd=src)
            first = git('rev-parse', 'HEAD', cwd=src)
            git('branch', 'keep-me', cwd=src)
            git('tag', 'v1', cwd=src)
            original = backup.command
            lfs_uploads = []
            include_lfs = False
            simulate_lost_ack = False
            unprotected = []
            protected = [{'name': 'main'}]
            def remove_protection(method, path):
                self.assertEqual((method, path), ('DELETE', '/projects/123/protected_branches/main'))
                saved = backup.remote_refs(str(dst), env)
                self.assertIn(first, [sha for ref, sha in saved.items() if ref.startswith(backup.RECOVERY)])
                # Protection must go before the protected branch is force-updated.
                self.assertEqual(saved['refs/heads/main'], first)
                protected.clear()
                unprotected.append(True)
            def no_lfs(args, **kw):
                if 'lfs' in args:
                    if 'fetch' in args and include_lfs:
                        obj = Path(kw['cwd']) / 'lfs' / 'objects' / 'aa' / 'bb' / ('a' * 64)
                        obj.parent.mkdir(parents=True)
                        obj.write_bytes(b'test object')
                    if 'push' in args:
                        lfs_uploads.append(True)
                    return ''
                output = original(args, **kw)
                if simulate_lost_ack and '--atomic' in args and any(a.startswith('--force-with-lease=') for a in args):
                    raise backup.BackupError('Git push timed out after server accepted refs')
                return output
            def sync():
                with tempfile.TemporaryDirectory(dir=root) as work:
                    with patch.object(backup, 'command', side_effect=no_lfs), patch.object(backup, 'pages', side_effect=lambda path: list(protected)), patch.object(backup, 'api', side_effect=remove_protection):
                        return backup.sync_git(str(src), str(dst), work, env, env, project={'id': 123, 'default_branch': 'main'})
            self.assertEqual(sync()['status'], 'synced')
            self.assertEqual(lfs_uploads, [])
            self.assertEqual(sync()['status'], 'unchanged')
            self.assertEqual(unprotected, [])
            (src / 'file').write_text('rewrite')
            git('add', '.', cwd=src)
            git('commit', '--amend', '-m', 'rewritten', cwd=src)
            git('tag', '-f', 'v1', cwd=src)
            git('branch', '-D', 'keep-me', cwd=src)
            include_lfs = True
            simulate_lost_ack = True
            self.assertEqual(sync()['status'], 'synced')
            self.assertEqual(lfs_uploads, [True])
            self.assertEqual(unprotected, [True])
            refs = backup.remote_refs(str(dst), env)
            self.assertEqual(refs['refs/heads/main'], git('rev-parse', 'HEAD', cwd=src))
            self.assertNotIn('refs/heads/keep-me', refs)
            self.assertIn(first, [sha for ref, sha in refs.items() if ref.startswith(backup.RECOVERY)])
            git('fsck', '--full', cwd=dst)


if __name__ == '__main__':
    unittest.main()
