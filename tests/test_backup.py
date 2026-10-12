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

    def test_extra_destination_refs_are_retained(self):
        self.assertTrue(backup.source_matches({'refs/heads/main': 'a'}, {'refs/heads/main': 'a', 'refs/heads/deleted-upstream': 'b'}))

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
            def remove_protection(method, path):
                self.assertEqual((method, path), ('DELETE', '/projects/123/protected_branches/main'))
                saved = backup.remote_refs(str(dst), env)
                self.assertIn(first, [sha for ref, sha in saved.items() if ref.startswith(backup.RECOVERY)])
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
                    with patch.object(backup, 'command', side_effect=no_lfs), patch.object(backup, 'pages', return_value=[{'name': 'main'}]), patch.object(backup, 'api', side_effect=remove_protection):
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
            self.assertEqual(refs['refs/heads/keep-me'], first)
            self.assertIn(first, [sha for ref, sha in refs.items() if ref.startswith(backup.RECOVERY)])
            git('fsck', '--full', cwd=dst)


if __name__ == '__main__':
    unittest.main()
