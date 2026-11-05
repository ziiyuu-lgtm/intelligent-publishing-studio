#!/usr/bin/env python3
"""Install Chinese closed-round translations on the fictional local test site.

The running SQLite database is never migrated or restored by this script.
The site is guarded by pinned source hashes and the expected systemd service.
"""
import hashlib
import os
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


BASE = Path('/srv/altavien-test/backend')
PROBE = Path('/srv/altavien-pg-probe/source')
PYTHON = '/srv/altavien-test/.venv/bin/python'
UNIT = 'altavien-test-web.service'
BASE_COMMIT = 'a9fdf7ed32a9aad9435166f974c8bdd8bcc79875'
TARGET_COMMIT = 'e87d309d06d034a5cb23796b68445a86ce3daaaa'
OLD = {
    'locale/zh_Hans/LC_MESSAGES/django.po': '20d16fe485acd2bdc2877fda5754368446ac18980fd080a41701ae4a140fe274',
    'locale/zh_Hans/LC_MESSAGES/django.mo': 'eaabcb15f4b5919725146fa110a53d7f95a16888c056a8e8a1e39d5db2125ebd',
}
NEW = {
    'locale/zh_Hans/LC_MESSAGES/django.po': '0acf1d3582244bfb4b2bc7adc009c8c451a6eb5c720d588d64966385f46bcaec',
    'locale/zh_Hans/LC_MESSAGES/django.mo': 'c003931bb8f24568b0340da721073d2caa896465f65850e477da76205866192c',
}


class Stop(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Stop(code)


def command(args, *, env=None, timeout=60, check=True):
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise Stop('COMMAND_FAILED:' + Path(args[0]).name)
    return result


def text(args, **kwargs):
    return command(args, **kwargs).stdout.decode('utf-8', 'replace').strip()


def git(*args):
    return command(['runuser', '-u', 'postgres', '--', 'git', '-C', str(PROBE), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def service():
    raw = text(['systemctl', 'show', UNIT, '-p', 'LoadState', '-p', 'ActiveState',
                '-p', 'SubState', '-p', 'MainPID', '-p', 'FragmentPath'])
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def homepage():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8000/', timeout=5) as response:
        require(response.status == 200, 'STOP_HOME_HTTP')


def read_service_environment(pid):
    raw = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    env = {}
    for entry in raw:
        if b'=' in entry:
            key, value = entry.split(b'=', 1)
            env[os.fsdecode(key)] = os.fsdecode(value)
    require(env.get('DJANGO_SETTINGS_MODULE', 'config.settings') == 'config.settings',
            'STOP_UNEXPECTED_DJANGO_SETTINGS')
    return env


def preflight():
    require(os.geteuid() == 0, 'STOP_REQUIRES_ROOT')
    account = pwd.getpwnam('altavien-test')
    require(BASE.is_dir() and not BASE.is_symlink(), 'STOP_SITE_DIRECTORY')
    require(Path(PYTHON).is_file(), 'STOP_SITE_PYTHON')
    require(git('rev-parse', 'HEAD').stdout.decode().strip() == BASE_COMMIT, 'STOP_PROBE_VERSION')
    require(not git('status', '--porcelain').stdout.strip(), 'STOP_PROBE_LOCAL_CHANGES')
    git('cat-file', '-e', TARGET_COMMIT + '^{commit}')
    for rel, expected in OLD.items():
        path = BASE / rel
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(BASE.resolve()),
                'STOP_SITE_FILE:' + rel)
        require(digest(path.read_bytes()) == expected, 'STOP_SITE_VERSION:' + rel)
    for rel in NEW.keys() - OLD.keys():
        require(not (BASE / rel).exists() and not (BASE / rel).is_symlink(), 'STOP_NEW_FILE_EXISTS:' + rel)
    state = service()
    require(state.get('LoadState') == 'loaded' and state.get('ActiveState') == 'active'
            and state.get('SubState') == 'running', 'STOP_SERVICE_NOT_RUNNING')
    pid = int(state.get('MainPID', '0'))
    require(pid > 1 and Path(f'/proc/{pid}').stat().st_uid == account.pw_uid, 'STOP_SERVICE_USER')
    argv = [os.fsdecode(x) for x in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if x]
    require('runserver' in argv and '127.0.0.1:8000' in argv and '--noreload' in argv,
            'STOP_UNSUPPORTED_SERVICE_COMMAND')
    cwd = Path(os.readlink(f'/proc/{pid}/cwd'))
    manage_paths = [(Path(x) if Path(x).is_absolute() else cwd / x).resolve()
                    for x in argv if Path(x).name == 'manage.py']
    require(manage_paths == [(BASE / 'manage.py').resolve()], 'STOP_SERVICE_COMMAND')
    executable = Path(argv[0]) if Path(argv[0]).is_absolute() else cwd / argv[0]
    require(executable.parent == Path(PYTHON).parent and executable.resolve() == Path(PYTHON).resolve(),
            'STOP_SERVICE_PYTHON')
    fragment = Path(state.get('FragmentPath', ''))
    require(fragment.is_file() and fragment.resolve().is_relative_to(Path('/run/systemd')),
            'STOP_SERVICE_FRAGMENT')
    listeners = text(['ss', '-H', '-lnt', 'sport = :8000']).splitlines()
    require(len(listeners) == 1 and listeners[0].split()[3] == '127.0.0.1:8000', 'STOP_LISTENER')
    homepage()
    db = BASE / '.runtime/dev.sqlite3'
    require(db.is_file() and not db.is_symlink(), 'STOP_DATABASE_PATH')
    return account, read_service_environment(pid), fragment, db, pid


def transient_canary():
    unit = f'altavien-history-zh-canary-{os.getpid()}.service'
    started = False
    try:
        command(['systemd-run', '--unit=' + unit, '--property=Type=exec', '/bin/sleep', '120'])
        started = True
        command(['systemctl', 'restart', unit])
        command(['systemctl', 'is-active', '--quiet', unit])
    finally:
        if started:
            command(['systemctl', 'stop', unit], check=False)
    print('TRANSIENT_RESTART_CANARY_PASSED', flush=True)


def atomic_write(path, content, uid, gid, mode):
    fd, name = tempfile.mkstemp(prefix='.altavien-history-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
            os.fchown(stream.fileno(), uid, gid)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sqlite_snapshot(source, destination, uid, gid):
    with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True, timeout=10) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            require(dst.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'STOP_SNAPSHOT_INTEGRITY')
            rows = {row[0] for row in dst.execute(
                "SELECT name FROM django_migrations WHERE app = 'publishing'")}
            require({'0051_review_copy_checked_sha256', '0052_review_metadata_checked_sha256'} <= rows,
                    'STOP_REQUIRED_MIGRATIONS_MISSING')
            columns = {row[1] for row in dst.execute('PRAGMA table_info(publishing_submissionversion)')}
            require({'review_copy_checked_sha256', 'review_metadata_checked_sha256'} <= columns,
                    'STOP_REQUIRED_COLUMNS_MISSING')
    os.chmod(destination, 0o600)
    os.chown(destination, uid, gid)


def owner_tree(root, uid, gid):
    for root_dir, dirs, files in os.walk(root, followlinks=False):
        os.chown(root_dir, uid, gid)
        for name in dirs + files:
            path = Path(root_dir) / name
            if not path.is_symlink():
                os.chown(path, uid, gid)


def get_candidates():
    files = {}
    for rel, expected in NEW.items():
        data = git('show', TARGET_COMMIT + ':backend/' + rel).stdout
        require(digest(data) == expected, 'STOP_CANDIDATE_HASH:' + rel)
        files[rel] = data
    return files


def app_command(backend, env, *args):
    result = command(['runuser', '-u', 'altavien-test', '--', PYTHON,
                      str(backend / 'manage.py'), *args], env=env, timeout=90)
    return result.stdout.decode('utf-8', 'replace')


SMOKE = '''
import json
from django.test import Client
from django.urls import reverse
from django.utils import translation
from publishing.models import PublicationDraft
with translation.override('zh-hans'):
    assert translation.gettext('Recheck historical reviewer copy') == '重新核查历史审稿副本'
    assert translation.gettext('Manual check recorded for this closed round.') == '已记录该关闭轮次的人工核查。'
with translation.override('en'):
    assert translation.gettext('Recheck historical reviewer copy') == 'Recheck historical reviewer copy' 
c = Client()
assert c.get('/').status_code == 200
assert reverse('historical_review_copy_check', args=['00000000-0000-0000-0000-000000000000', 1])
paths = []
for p in PublicationDraft.objects.filter(published_at__isnull=False, published_proof__isnull=False,
        submission__journal__active=True, submission__journal__organisation__active=True).select_related('submission__journal')[:20]:
    article = reverse('test_article', args=[p.submission.journal.slug, p.pk])
    pdf = reverse('test_article_pdf', args=[p.submission.journal.slug, p.pk])
    assert c.get(article).status_code == 200
    response = c.get(pdf)
    assert response.status_code == 200
    first = next(iter(response.streaming_content), b'') if response.streaming else response.content
    assert first.startswith(b'%PDF-')
    paths.append([article, pdf])
print('ALTAVIEN_SMOKE_PATHS=' + json.dumps(paths))
'''


def stage_check(files, env, account, snapshot):
    import json
    root = Path(tempfile.mkdtemp(prefix='altavien-history-zh-stage-', dir='/srv'))
    try:
        stage = root / 'backend'
        shutil.copytree(BASE, stage, symlinks=True,
                        ignore=shutil.ignore_patterns('.runtime', '__pycache__', '*.pyc'))
        runtime = stage / '.runtime'
        runtime.mkdir(mode=0o700)
        shutil.copy2(snapshot, runtime / 'dev.sqlite3')
        key = BASE / '.runtime/review-ai-fingerprint.key'
        if key.is_file() and not key.is_symlink():
            shutil.copy2(key, runtime / key.name)
        media = BASE / '.runtime/private-media'
        if media.is_dir():
            (runtime / 'private-media').symlink_to(media, target_is_directory=True)
        for rel, data in files.items():
            dest = stage / rel
            require(dest.parent.is_dir() and not dest.parent.is_symlink(), 'STOP_STAGE_PARENT:' + rel)
            atomic_write(dest, data, account.pw_uid, account.pw_gid, 0o644)
        owner_tree(root, account.pw_uid, account.pw_gid)
        os.chmod(root, 0o700)
        app_command(stage, env, 'check')
        output = app_command(stage, env, 'shell', '-c', SMOKE)
        marker = 'ALTAVIEN_SMOKE_PATHS='
        lines = [line[len(marker):] for line in output.splitlines() if line.startswith(marker)]
        require(len(lines) == 1, 'STOP_STAGE_SMOKE')
        paths = json.loads(lines[0])
        print('STAGED_PUBLISHED_ARTICLES_OK=' + str(len(paths)), flush=True)
        print('STAGED_CHECKS_PASSED', flush=True)
        return paths
    finally:
        shutil.rmtree(root)


def restart_and_check(paths):
    command(['systemctl', 'restart', UNIT], timeout=30)
    for _ in range(30):
        state = service()
        if state.get('ActiveState') == 'active' and state.get('SubState') == 'running':
            try:
                homepage()
                break
            except Exception:
                pass
        time.sleep(0.5)
    else:
        raise Stop('STOP_RESTART_HEALTH')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for article, pdf in paths:
        require(article.startswith('/journals/') and pdf.startswith('/journals/'), 'STOP_SMOKE_PATH')
        with opener.open('http://127.0.0.1:8000' + article, timeout=10) as response:
            require(response.status == 200, 'STOP_LIVE_ARTICLE')
        with opener.open('http://127.0.0.1:8000' + pdf, timeout=10) as response:
            require(response.status == 200 and response.read(5) == b'%PDF-', 'STOP_LIVE_PDF')


def recover_service(fragment_backup):
    result = command(['systemctl', 'restart', UNIT], timeout=30, check=False)
    if result.returncode:
        result = command(['systemctl', 'start', UNIT], timeout=30, check=False)
    if result.returncode:
        runtime_unit = Path('/run/systemd/system') / UNIT
        require(not runtime_unit.exists() and not runtime_unit.is_symlink(), 'STOP_RUNTIME_UNIT_EXISTS')
        atomic_write(runtime_unit, fragment_backup.read_bytes(), 0, 0, 0o600)
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'start', UNIT], timeout=30)
    for _ in range(30):
        try:
            homepage()
            return
        except Exception:
            time.sleep(0.5)
    raise Stop('STOP_RECOVERY_HEALTH')


def main():
    import json
    backup = None
    modified = []
    try:
        account, env, fragment, db, pid = preflight()
        db_identity = (db.stat().st_dev, db.stat().st_ino)
        candidates = get_candidates()
        transient_canary()
        backup = Path(tempfile.mkdtemp(prefix='altavien-history-zh-backup-e87-', dir='/srv'))
        os.chmod(backup, 0o700)
        fragment_backup = backup / 'service.unit'
        shutil.copyfile(fragment, fragment_backup)
        os.chmod(fragment_backup, 0o600)
        metadata = {}
        for rel in OLD:
            path = BASE / rel
            stat = path.stat()
            metadata[rel] = (stat.st_uid, stat.st_gid, stat.st_mode & 0o777)
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        manifest = backup / 'file-metadata.json'
        manifest.write_text(json.dumps(metadata, sort_keys=True), encoding='utf-8')
        os.chmod(manifest, 0o600)
        snapshot = backup / 'db-before.sqlite3'
        sqlite_snapshot(db, snapshot, 0, 0)
        print('BACKUP_DIRECTORY=' + str(backup), flush=True)
        paths = stage_check(candidates, env, account, snapshot)
        _, _, current_fragment, current_db, current_pid = preflight()
        require(current_pid == pid and current_fragment == fragment and
                (current_db.stat().st_dev, current_db.stat().st_ino) == db_identity,
                'STOP_SITE_CHANGED_DURING_STAGING')
        for rel, data in candidates.items():
            path = BASE / rel
            require(path.parent.is_dir() and not path.parent.is_symlink(), 'STOP_LIVE_PARENT:' + rel)
            uid, gid, mode = metadata.get(rel, (account.pw_uid, account.pw_gid, 0o644))
            atomic_write(path, data, uid, gid, mode)
            modified.append(rel)
        restart_and_check(paths)
        for rel, expected in NEW.items():
            require(digest((BASE / rel).read_bytes()) == expected, 'STOP_POST_INSTALL_HASH:' + rel)
        print('ALTAVIEN_HISTORY_ZH_UPGRADE_READY', flush=True)
        return 0
    except Exception as exc:
        print('ALTAVIEN_HISTORY_ZH_UPGRADE_FAILED=' + str(exc), flush=True)
        if modified:
            try:
                for rel in reversed(modified):
                    path = BASE / rel
                    if rel in OLD:
                        uid, gid, mode = metadata[rel]
                        atomic_write(path, (backup / rel).read_bytes(), uid, gid, mode)
                    else:
                        require(digest(path.read_bytes()) == NEW[rel], 'STOP_ROLLBACK_NEW_FILE_CHANGED')
                        path.unlink()
                recover_service(fragment_backup)
                print('ALTAVIEN_ORIGINAL_SITE_RESTORED', flush=True)
            except Exception as recovery_exc:
                print('ALTAVIEN_RECOVERY_NEEDS_ATTENTION=' + str(recovery_exc), flush=True)
        if backup:
            print('BACKUP_DIRECTORY=' + str(backup), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
