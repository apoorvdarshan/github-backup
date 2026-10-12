#!/usr/bin/env python3
"""Cloud Git backups. Never executes source repository code or deletes backups."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

GITHUB = "https://api.github.com"
GITLAB = "https://gitlab.com/api/v4"
OWNER = os.environ.get("GITHUB_OWNER", "apoorvdarshan")
NAMESPACE_ID = os.environ.get("GITLAB_NAMESPACE_ID", "126304343")
NAMESPACE_PATH = os.environ.get("GITLAB_NAMESPACE_PATH", "apoorvdarshan")
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
RECOVERY = "refs/tags/backup-history/"
CONTEXT = threading.local()
# Explicitly approved exception: GitLab rejects a historical malformed email.
# Keep the original objects in a restorable bundle, never rewrite their history.
BUNDLE_SOURCE_IDS = {1287169174}


class BackupError(Exception):
    pass


def api(method, path, data=None, github=False, missing_ok=False):
    token = os.environ["GITHUB_TOKEN" if github else "GITLAB_TOKEN"]
    base = GITHUB if github else GITLAB
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    headers["Content-Type"] = "application/json"
    for attempt in range(4):
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=None if data is None else json.dumps(data).encode())
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return None
            if exc.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(min(30, 2 ** (attempt + 1)))
                continue
            # Never log response bodies, URLs with credentials, or request headers.
            raise BackupError(f"{'GitHub' if github else 'GitLab'} API {method} failed: HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise BackupError("API connection failed after retries") from None
            time.sleep(2 ** attempt)


def pages(path, github=False):
    out = []
    for page in range(1, 1001):
        sep = "&" if "?" in path else "?"
        batch = api("GET", f"{path}{sep}per_page=100&page={page}", github=github)
        if not isinstance(batch, list):
            raise BackupError("Expected a paginated list")
        out.extend(batch)
        if len(batch) < 100:
            return out
    raise BackupError("Pagination limit exceeded")


def command(args, cwd=None, env=None, timeout=1200):
    # Log only fixed operation labels, never command arguments or remote output.
    operation = "Git operation"
    if args[0] == "git":
        if "lfs" in args:
            operation = "Git LFS " + ("fetch" if "fetch" in args else "push")
        elif args[1] in ("fetch", "push", "ls-remote"):
            operation = "Git " + args[1]
    repository = getattr(CONTEXT, "repository", None)
    if repository and operation != "Git operation":
        print(json.dumps({"event": "operation_started", "repository": repository, "operation": operation}), flush=True)
    # HTTP/2 uploads on the hosted runner repeatedly stalled, while the same
    # repository completed and verified over HTTP/1.1. Keep auth unchanged.
    run_args = ["git", "-c", "http.version=HTTP/1.1", *args[1:]] if args[0] == "git" else args
    with subprocess.Popen(run_args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # Git launches helpers (including LFS) that inherit its pipes. Killing
            # only Git can leave communicate() waiting forever for those helpers.
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise BackupError(f"{operation} timed out after {timeout} seconds") from None
    if process.returncode:
        # Output from remotes is untrusted and could echo a credential.
        hints = ['authentication failed', 'http basic: access denied', 'repository not found',
                 'could not read username', 'could not read password', 'not allowed',
                 'unable to access', 'no such file or directory', 'permission denied',
                 'protected branch', 'exceeds', 'lfs', 'not found']
        hints.insert(0, 'you cannot use default branch name to create a tag')
        hint = next((h for h in hints if h in stderr.lower()), 'remote operation error')
        # GitLab's own rejection reason is needed to distinguish limits and
        # invalid source history from transport failures. Never print full Git
        # output, headers, URLs, or credential-helper diagnostics.
        diagnostics = [line.removeprefix('remote: ') for line in stderr.splitlines()]
        reason = '; '.join(line for line in diagnostics
                           if line.startswith(('GitLab:', 'error:', 'fatal:')))[:1200]
        if reason:
            for key, secret in (env or os.environ).items():
                if re.search(r'TOKEN|PASSWORD|SECRET', key, re.I) and len(secret) >= 8:
                    reason = reason.replace(secret, '[redacted]')
            reason = re.sub(r'(?:glpat-|github_pat_|gh[pousr]_)[A-Za-z0-9_.-]+', '[redacted]', reason)
            reason = re.sub(r'https?://\S+', '[remote URL]', reason)
            hint = reason[:400]
        raise BackupError(f"{args[0]} {args[1]} failed (exit {process.returncode}; {hint})")
    return stdout


def git_env(token, root):
    helper = Path(root) / "askpass.py"
    if not helper.exists():
        helper.write_text("#!/usr/bin/env python3\nimport os,sys\nprint('oauth2' if 'username' in sys.argv[1].lower() else os.environ['BACKUP_GIT_TOKEN'])\n")
        helper.chmod(0o700)
    env = dict(os.environ)
    # Only the one credential needed by this Git subprocess is inherited.
    env.pop("GITHUB_TOKEN", None)
    env.pop("GITLAB_TOKEN", None)
    # Hosted runners inject proactive CI-job credentials through Git config env.
    # They must not override the explicit source/destination backup credential.
    for key in list(env):
        if key.startswith('GIT_CONFIG_') or key in ('GIT_DIR', 'GIT_WORK_TREE'):
            env.pop(key)
    env.update(BACKUP_GIT_TOKEN=token, GIT_ASKPASS=str(helper), GIT_TERMINAL_PROMPT="0",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null", GIT_LFS_SKIP_SMUDGE="1")
    return env


def remote_refs(url, env):
    result = {}
    # Do not inherit the manager checkout's local CI-job credential configuration.
    with tempfile.TemporaryDirectory(prefix='git-ref-check-') as clean_cwd:
        output = command(["git", "ls-remote", "--refs", url, "refs/heads/*", "refs/tags/*"], cwd=clean_cwd, env=env, timeout=180)
    for line in output.splitlines():
        sha, ref = line.split("\t", 1)
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
            raise BackupError("Invalid remote object ID")
        result[ref] = sha
    return result


def source_matches(source, destination):
    return all(destination.get(ref) == sha for ref, sha in source.items())


def ref_batches(refs, size=256):
    items = list(refs.items())
    for offset in range(0, len(items), size):
        yield dict(items[offset:offset + size])


def assigned_repositories(repos, index, total):
    if total < 1 or not 1 <= index <= total:
        raise BackupError("Invalid backup shard configuration")
    return [repo for repo in repos if repo["id"] % total == index - 1]


def recovery_plan(source, destination):
    """Keep every destination tip that will be replaced; deleted refs remain."""
    plan = {}
    for ref, sha in source.items():
        old = destination.get(ref)
        if old and old != sha:
            suffix = hashlib.sha256(ref.encode()).hexdigest()[:24]
            plan[f"{RECOVERY}{RUN_ID}/{suffix}-{old}"] = old
    return plan


class NoPackageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def package_file(method, path, local, missing_ok=False):
    """Stream package files; never forward GitLab credentials to object storage."""
    opener = urllib.request.build_opener(NoPackageRedirect())
    url = GITLAB + path
    for redirect in range(5):
        headers = {}
        if urllib.parse.urlsplit(url).netloc == urllib.parse.urlsplit(GITLAB).netloc:
            headers['PRIVATE-TOKEN'] = os.environ['GITLAB_TOKEN']
        upload = None
        try:
            if method == 'PUT':
                upload = Path(local).open('rb')
                headers.update({'Content-Type': 'application/octet-stream',
                                'Content-Length': str(Path(local).stat().st_size)})
            req = urllib.request.Request(url, method=method, headers=headers, data=upload)
            with opener.open(req, timeout=300) as response:
                if method == 'GET':
                    with Path(local).open('wb') as output:
                        while chunk := response.read(1024 * 1024):
                            output.write(chunk)
                else:
                    response.read()
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and missing_ok:
                return False
            if method == 'GET' and exc.code in (301, 302, 303, 307, 308):
                target = urllib.parse.urljoin(url, exc.headers.get('Location', ''))
                parsed = urllib.parse.urlsplit(target)
                if parsed.scheme != 'https' or parsed.username or parsed.password:
                    raise BackupError('Unsafe package download redirect') from None
                url = target
                continue
            raise BackupError(f'Package {method} failed: HTTP {exc.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise BackupError(f'Package {method} connection failed') from None
        finally:
            if upload:
                upload.close()
    raise BackupError('Too many package download redirects')


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as data:
        while chunk := data.read(1024 * 1024):
            digest.update(chunk)
    return {'sha256': digest.hexdigest(), 'size': Path(path).stat().st_size}


def verify_bundle(bundle, source, root, env):
    restored = Path(root) / 'restored.git'
    # The approved archive must preserve even the malformed historical objects.
    # Clone verifies pack checksums; compare every original branch and tag too.
    command(['git', '-c', 'fetch.fsckObjects=false', 'clone', '--mirror', str(bundle), str(restored)], env=env)
    if remote_refs(str(restored), env) != source:
        raise BackupError('Bundle restore did not reproduce every source ref')


def sync_bundle(repo, project, root, source_env):
    root = Path(root)
    source_url = f"https://github.com/{OWNER}/{repo['name']}.git"
    source = remote_refs(source_url, source_env)
    if not source:
        raise BackupError('Cannot bundle an empty source')
    version = hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
    base = f"/projects/{project['id']}/packages/generic/git-history/{version}"
    if not project.get('packages_enabled'):
        api('PUT', f"/projects/{project['id']}", {'packages_enabled': True})
    manifest_path = root / 'manifest.json'
    if package_file('GET', base + '/manifest.json', manifest_path, missing_ok=True):
        manifest = json.loads(manifest_path.read_text())
        if manifest.get('source_id') != repo['id'] or manifest.get('refs') != source:
            raise BackupError('Bundle manifest does not match source')
        expected = manifest.get('files', {})
        if 'repository.bundle' not in expected or set(expected) - {'repository.bundle', 'lfs.tar'}:
            raise BackupError('Bundle manifest file list invalid')
        packages = pages(f"/projects/{project['id']}/packages?package_type=generic&package_name=git-history&package_version={version}")
        stored = [f for p in packages if p['name'] == 'git-history' and p['version'] == version
                  for f in pages(f"/projects/{project['id']}/packages/{p['id']}/package_files")]
        for name, info in expected.items():
            matches = [f for f in stored if f['file_name'] == name]
            if not matches or any(f.get('file_sha256') != info['sha256'] or f.get('size') != info['size'] for f in matches):
                raise BackupError('Stored bundle checksum or size mismatch')
        if remote_refs(source_url, source_env) != source:
            raise BackupError('Source changed during bundle check; retry next run')
        return {'status': 'unchanged', 'format': 'git-bundle', 'refs': len(source), 'package_version': version}
    repository = root / 'repository.git'
    command(['git', 'init', '--bare', str(repository)])
    command(['git', 'remote', 'add', 'origin', source_url], cwd=repository)
    command(['git', 'fetch', '--no-tags', 'origin', '+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*'], cwd=repository, env=source_env)
    if remote_refs(str(repository), source_env) != source:
        raise BackupError('Source changed while fetching bundle; retry next run')
    lfs = repository / 'lfs'
    command(['git', '-c', f'lfs.storage={lfs}', '-c', f'lfs.url={source_url}/info/lfs', 'lfs', 'fetch', '--all', 'origin'], cwd=repository, env=source_env)
    bundle = root / 'repository.bundle'
    command(['git', 'bundle', 'create', str(bundle), '--branches', '--tags'], cwd=repository, env=source_env)
    files = [bundle]
    if any(p.is_file() for p in (lfs / 'objects').rglob('*')):
        archive = root / 'lfs.tar'
        with tarfile.open(archive, 'w') as output:
            output.add(lfs / 'objects', arcname='objects')
        files.append(archive)
    manifest = {'source_id': repo['id'], 'refs': source, 'default_branch': repo.get('default_branch'),
                'created_at': RUN_ID, 'format': 'git-bundle', 'files': {f.name: file_digest(f) for f in files}}
    for file in files:
        package_file('PUT', base + '/' + file.name, file)
        downloaded = root / ('downloaded-' + file.name)
        package_file('GET', base + '/' + file.name, downloaded)
        if file_digest(downloaded) != manifest['files'][file.name]:
            raise BackupError('Uploaded bundle download checksum mismatch')
        if file.name == 'repository.bundle':
            verify_bundle(downloaded, source, root, source_env)
    if remote_refs(source_url, source_env) != source:
        raise BackupError('Source changed during bundle upload; retry next run')
    # Publish the completion marker only after download and actual restore pass.
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + '\n')
    package_file('PUT', base + '/manifest.json', manifest_path)
    readback = root / 'manifest-readback.json'
    package_file('GET', base + '/manifest.json', readback)
    if readback.read_bytes() != manifest_path.read_bytes():
        raise BackupError('Bundle completion manifest verification failed')
    return {'status': 'synced', 'format': 'git-bundle', 'refs': len(source), 'package_version': version}


def project_settings(repo):
    return {
        "visibility": "private",
        "description": f"Private backup of https://github.com/{OWNER}/{repo['name']}. Development stays on GitHub. Source repository ID: {repo['id']}.",
        "issues_access_level": "disabled", "merge_requests_access_level": "disabled",
        "wiki_access_level": "disabled", "snippets_access_level": "disabled",
        "builds_access_level": "disabled", "releases_access_level": "disabled",
        "pages_access_level": "disabled", "analytics_access_level": "disabled",
        "container_registry_access_level": "disabled", "packages_enabled": False,
        "request_access_enabled": False, "emails_enabled": False, "auto_devops_enabled": False,
        "lfs_enabled": True,
    }


def destination_project(repo, existing):
    marker = f"Source repository ID: {repo['id']}."
    matches = [p for p in existing if marker in (p.get("description") or "")]
    if len(matches) > 1:
        raise BackupError("Multiple destinations have the same source ID")
    path = repo["name"].lower()
    if matches:
        project = matches[0]
        if project["visibility"] != "private":
            api("PUT", f"/projects/{project['id']}", {"visibility": "private"})
        return project
    if any(p["path"].lower() == path for p in existing):
        # Never adopt or overwrite an unrelated repository, including the manager.
        path = f"{path}-github-{repo['id']}"
    if any(p["path"].lower() == path for p in existing):
        raise BackupError("Destination path collision; refusing to overwrite")
    settings = project_settings(repo)
    settings.update(name=repo["name"], path=path, namespace_id=int(NAMESPACE_ID), initialize_with_readme=False)
    try:
        project = api("POST", "/projects", settings)
    except BackupError:
        # An earlier job may have created it after this run's inventory snapshot.
        project = api("GET", "/projects/" + urllib.parse.quote(f"{NAMESPACE_PATH}/{path}", safe=""), missing_ok=True)
        if not project or marker not in (project.get("description") or ""):
            raise BackupError("Could not create or safely identify backup destination") from None
    # Backup namespaces have no human contributions; the bot must update rewritten refs.
    for branch in pages(f"/projects/{project['id']}/protected_branches"):
        api("DELETE", f"/projects/{project['id']}/protected_branches/{urllib.parse.quote(branch['name'], safe='')}")
    return project


def sync_git(source_url, destination_url, root, source_env, dest_env, project=None):
    source = remote_refs(source_url, source_env)
    if any(ref.startswith(RECOVERY) for ref in source):
        raise BackupError("Source uses reserved backup-history tag namespace")
    destination = remote_refs(destination_url, dest_env)
    if source_matches(source, destination):
        return {"status": "unchanged", "refs": len(source)}
    repo_dir = Path(root) / "repository.git"
    command(["git", "init", "--bare", str(repo_dir)])
    command(["git", "remote", "add", "origin", source_url], cwd=repo_dir)
    command(["git", "fetch", "--no-tags", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], cwd=repo_dir, env=source_env)
    fetched = {}
    for line in command(["git", "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads", "refs/tags"], cwd=repo_dir).splitlines():
        sha, ref = line.split(" ", 1)
        fetched[ref] = sha
    if source != fetched:
        raise BackupError("Source changed while fetching; retry next run")
    command(["git", "remote", "add", "backup", destination_url], cwd=repo_dir)
    # Fetch all LFS objects reachable from current branches and tags; do this before
    # publishing refs so a failed LFS upload is retried on the next run.
    lfs_storage = repo_dir / "lfs"
    lfs_config = ["-c", f"lfs.storage={lfs_storage}"]
    command(["git", *lfs_config, "-c", f"lfs.url={source_url}/info/lfs", "lfs", "fetch", "--all", "origin"], cwd=repo_dir, env=source_env)
    # A successful --all fetch into fresh, explicitly local storage establishes
    # whether there are any LFS objects to upload. Avoid a second history scan
    # for repositories with no LFS data.
    if any(p.is_file() for p in (lfs_storage / "objects").rglob("*")):
        command(["git", *lfs_config, "-c", f"lfs.url={destination_url}/info/lfs", "-c", f"lfs.pushurl={destination_url}/info/lfs", "lfs", "push", "--all", "backup"], cwd=repo_dir, env=dest_env)
    recovery = recovery_plan(source, destination)
    if recovery:
        command(["git", "fetch", "--no-tags", "backup", "+refs/heads/*:refs/backup-old/heads/*", "+refs/tags/*:refs/backup-old/tags/*"], cwd=repo_dir, env=dest_env)
        for ref, sha in recovery.items():
            command(["git", "update-ref", ref, sha], cwd=repo_dir)
        for batch in ref_batches(recovery):
            command(["git", "push", "--atomic", "backup"] + [f"{ref}:{ref}" for ref in batch], cwd=repo_dir, env=dest_env)
        if project and any(ref.startswith('refs/heads/') and destination.get(ref) != sha
                           for ref, sha in source.items() if ref in destination):
            # GitLab may auto-protect the default branch after the initial push.
            # Old tips are already saved; permit leased updates in this managed
            # backup so a future source rewrite does not silently stop syncing.
            for branch in pages(f"/projects/{project['id']}/protected_branches"):
                api('DELETE', f"/projects/{project['id']}/protected_branches/{urllib.parse.quote(branch['name'], safe='')}")
    original_default = project.get("default_branch") if project else None
    collision = f"refs/tags/{original_default}" if original_default else None
    switched_default = False
    if collision in source and destination.get(collision) != source[collision]:
        # GitLab rejects creating/updating a tag named like its current default
        # branch. Temporarily select another ORIGINAL source branch, preserving
        # every commit and ref name; switch back after publishing the full copy.
        candidates = [r for r in source if r.startswith("refs/heads/") and f"refs/tags/{r[11:]}" not in source]
        if not candidates:
            raise BackupError("Default-branch tag collision has no alternate source branch")
        alternate = sorted(candidates)[0]
        if destination.get(alternate) != source[alternate]:
            command(["git", "push", f"--force-with-lease={alternate}:{destination.get(alternate, '')}", "backup", f"{alternate}:{alternate}"], cwd=repo_dir, env=dest_env)
            destination[alternate] = source[alternate]
        api("PUT", f"/projects/{project['id']}", {"default_branch": alternate[11:]})
        switched_default = True
    changed = {ref: sha for ref, sha in source.items() if destination.get(ref) != sha}
    # Thousands of tags can exceed the OS argument-size limit. Each bounded
    # batch is atomic and leased; success still requires the full final ref set.
    for batch in ref_batches(changed):
        leases = [f"--force-with-lease={ref}:{destination.get(ref, '')}" for ref in batch]
        try:
            command(["git", "push", "--atomic"] + leases + ["backup"] + [f"{ref}:{ref}" for ref in batch], cwd=repo_dir, env=dest_env, timeout=300)
        except BackupError:
            # A connection can fail after GitLab has committed the transaction.
            observed = remote_refs(destination_url, dest_env)
            if not source_matches(batch, observed) or not source_matches(recovery, observed):
                raise
    if switched_default and f"refs/heads/{original_default}" in source:
        api("PUT", f"/projects/{project['id']}", {"default_branch": original_default})
    actual = remote_refs(destination_url, dest_env)
    if not source_matches(source, actual) or not source_matches(recovery, actual):
        raise BackupError("Post-push verification failed")
    if remote_refs(source_url, source_env) != source:
        raise BackupError("Source changed during backup; retry next run")
    return {"status": "synced", "refs": len(source), "recovery_refs": len(recovery)}


def process(repo, existing):
    started = time.monotonic()
    CONTEXT.repository = repo["full_name"]
    result = {"repository": repo["full_name"], "source_id": repo["id"]}
    try:
        project = destination_project(repo, existing)
        result["destination"] = project["web_url"]
        with tempfile.TemporaryDirectory(prefix="git-backup-") as root:
            source_env = git_env(os.environ["GITHUB_TOKEN"], root)
            dest_env = git_env(os.environ["GITLAB_TOKEN"], root)
            if repo['id'] in BUNDLE_SOURCE_IDS:
                result.update(sync_bundle(repo, project, root, source_env))
            else:
                result.update(sync_git(f"https://github.com/{OWNER}/{repo['name']}.git", project["http_url_to_repo"], root, source_env, dest_env, project=project))
        if repo.get("default_branch") and result.get("refs") and result.get('format') != 'git-bundle':
            if project.get("default_branch") != repo["default_branch"]:
                api("PUT", f"/projects/{project['id']}", {"default_branch": repo["default_branch"]})
    except (BackupError, subprocess.TimeoutExpired) as exc:
        result.update(status="failed", error=str(exc) if isinstance(exc, BackupError) else "Git operation timed out")
    except Exception as exc:
        result.update(status="failed", error=f"Unexpected backup error ({type(exc).__name__}); no secrets logged")
    result["seconds"] = round(time.monotonic() - started, 1)
    print(json.dumps(result), flush=True)
    return result


def main():
    report = {"started_at": RUN_ID, "results": [], "complete": False}
    try:
        me = api("GET", "/user", github=True)
        if me["login"].lower() != OWNER.lower():
            raise BackupError("GitHub token owner mismatch")
        namespace = api("GET", f"/namespaces/{NAMESPACE_ID}")
        identity = api("GET", "/user")
        if namespace["full_path"] != NAMESPACE_PATH or namespace["kind"] != "user" or identity["username"] != NAMESPACE_PATH:
            raise BackupError("GitLab personal namespace or token owner mismatch")
        repos = pages("/user/repos?affiliation=owner&sort=full_name", github=True)
        repos = [r for r in repos if r["owner"]["login"].lower() == OWNER.lower()]
        if not repos:
            raise BackupError("GitHub returned zero repositories; refusing empty backup")
        existing = pages(f"/users/{identity['id']}/projects?order_by=id&sort=asc")
        existing = [p for p in existing if str(p["namespace"]["id"]) == NAMESPACE_ID]
        # Stable IDs allow source renames without duplicate destination projects.
        source_ids = {r["id"] for r in repos}
        report["retained_missing_sources"] = [p["path"] for p in existing if (m := re.search(r"Source repository ID: (\d+)\.", p.get("description") or "")) and int(m.group(1)) not in source_ids]
        report["repository_count"] = len(repos)
        shard_total = int(os.environ.get("CI_NODE_TOTAL", "1"))
        shard_index = int(os.environ.get("CI_NODE_INDEX", "1"))
        assigned = assigned_repositories(repos, shard_index, shard_total)
        report.update(shard_index=shard_index, shard_total=shard_total, assigned_count=len(assigned))
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(os.environ.get("BACKUP_WORKERS", "4"))) as pool:
            futures = [pool.submit(process, repo, existing) for repo in assigned]
            for future in concurrent.futures.as_completed(futures):
                report["results"].append(future.result())
        report["complete"] = len(report["results"]) == len(assigned) and all(r["status"] != "failed" for r in report["results"])
    except BackupError as exc:
        report["error"] = str(exc)
        print(str(exc), file=sys.stderr)
    finally:
        report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        Path("report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
