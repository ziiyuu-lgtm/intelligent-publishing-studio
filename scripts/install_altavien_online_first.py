#!/usr/bin/env python3
"""Guarded upgrade of the fictional local test site only.

Back up SQLite before the upgrade. The migration is additive, and recovery
never restores the database over possible writes made after the service starts.
"""
import hashlib
import json
import os
import pwd
import re
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
RUNTIME_UNIT = Path('/run/systemd/system') / UNIT
TRANSIENT_UNIT = Path('/run/systemd/transient') / UNIT
BASE_COMMIT = 'a9fdf7ed32a9aad9435166f974c8bdd8bcc79875'
TARGET_COMMIT = '30c82ec0f0bc258ba865e7d3d449e67c9ba0fd48'
MIGRATION = '0053_publishedissueproposal'
TABLE = 'publishing_publishedissueproposal'
OLD = {
    'publishing/models.py': '24ffdd5c870c0ac4003ba80a0fbd0a60fd342c681f16aed5f7883fbeb526a696',
    'publishing/production_views.py': '2e5bb321066da9bd27e712abaff426a77c342b2818ca287a514c9991954f6b0b',
    'publishing/urls.py': '423860ae49c4141caac3b5ef41fcc29dc0c0a45c1acada11570d4725a64818d1',
    'templates/publishing/production_detail.html': 'df64795384c2e8a87c061830804b50ac8ba77114dd6ebff051c704c691f98e35',
    'locale/zh_Hans/LC_MESSAGES/django.po': '0acf1d3582244bfb4b2bc7adc009c8c451a6eb5c720d588d64966385f46bcaec',
    'locale/zh_Hans/LC_MESSAGES/django.mo': 'c003931bb8f24568b0340da721073d2caa896465f65850e477da76205866192c',
}
NEW = {
    'publishing/models.py': '7c67d19772c8471cd427a3ca58041f3808aead47b78d99918469e81ff8fa544b',
    'publishing/production_views.py': 'a2d099c425f0efb7b2787e00c1755b5ea7f28ca0e0d2d61526bd0f204cdb7634',
    'publishing/urls.py': 'c0c4dfe616abb5545b4c2a0c3c64d52c6a54bd46d19cdc84586f63be9af0ed90',
    'publishing/published_issue.py': '6ead826c73613b67e413355e01c77c32b628f38957ada9a28aa04e32471b7212',
    'publishing/published_issue_views.py': '66daa11e930da3ab5abc53123ccbd33c2b829e5fc515e29dc0d49aa9e29fb8c2',
    'publishing/migrations/0053_publishedissueproposal.py': 'b2adf9ef19a5106db273fa2ff7928464c58e9b8b450c16e20b34808114925dc3',
    'templates/publishing/production_detail.html': 'd98bf5f6f45502ecd73bf526f1471375fe12af08d9c9391fbb63463b6a10c60b',
    'locale/zh_Hans/LC_MESSAGES/django.po': '84805d7e46a5ffea56b2520ee1e36df1698fe1672131e5a7af26a237632a8c92',
    'locale/zh_Hans/LC_MESSAGES/django.mo': '3737e17162cefbf39d4b359dbccbaaddd0947b7573516a0864052e98f05afc6f',
}
PRESERVED_TABLES = ('auth_user', 'publishing_submission', 'publishing_publicationdraft',
                    'publishing_publicationproof', 'publishing_publicationapproval', 'publishing_journalissue')
ISSUE_COLUMNS = {'id', 'source_fingerprint', 'issue_snapshot', 'status', 'created_at', 'resolved_at',
                 'draft_id', 'issue_id', 'proposed_by_id', 'resolved_by_id'}


class Stop(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Stop(code)


def command(args, *, env=None, timeout=90, check=True):
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise Stop('COMMAND_FAILED:' + Path(args[0]).name + ':' + str(result.returncode))
    return result


def text(args, **kwargs):
    return command(args, **kwargs).stdout.decode('utf-8', 'replace').strip()


def git(*args):
    return command(['runuser', '-u', 'postgres', '--', 'git', '-C', str(PROBE), *args])


def digest(data):
    return hashlib.sha256(data).hexdigest()


def service():
    raw = text(['systemctl', 'show', UNIT, '-p', 'LoadState', '-p', 'ActiveState', '-p', 'SubState',
                '-p', 'MainPID', '-p', 'FragmentPath', '-p', 'DropInPaths', '-p', 'User'])
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def homepage():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8000/', timeout=5) as response:
        require(response.status == 200, 'STOP_HOME_HTTP')


def service_environment(pid):
    raw = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    env = {}
    for entry in raw:
        if b'=' in entry:
            key, value = entry.split(b'=', 1)
            env[os.fsdecode(key)] = os.fsdecode(value)
    require(env.get('DJANGO_SETTINGS_MODULE', 'config.settings') == 'config.settings', 'STOP_SETTINGS')
    require(env.get('IPS_LOCAL_PUBLICATION_ENABLED') == '1', 'STOP_PUBLICATION_DISABLED')
    return env


def database_state(path):
    with sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=10) as db:
        require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'STOP_DATABASE_INTEGRITY')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require(set(PRESERVED_TABLES) <= tables, 'STOP_DATABASE_TABLES')
        applied = {row[0] for row in db.execute("SELECT name FROM django_migrations WHERE app = 'publishing'")}
        require({'0051_review_copy_checked_sha256', '0052_review_metadata_checked_sha256'} <= applied,
                'STOP_REQUIRED_MIGRATIONS')
        require((MIGRATION in applied) == (TABLE in tables), 'STOP_INCONSISTENT_0053')
        if TABLE in tables:
            columns = {row[1] for row in db.execute('PRAGMA table_info(' + TABLE + ')')}
            require(ISSUE_COLUMNS <= columns, 'STOP_INCONSISTENT_0053_COLUMNS')
        counts = {name: db.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0]
                  for name in PRESERVED_TABLES}
        return {'applied': MIGRATION in applied, 'counts': counts}


def snapshot(source, destination):
    with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True, timeout=10) as src:
        with sqlite3.connect(destination, timeout=10) as dst:
            src.backup(dst)
    os.chmod(destination, 0o600)
    return database_state(destination)


def preflight():
    require(os.geteuid() == 0, 'STOP_REQUIRES_ROOT')
    account = pwd.getpwnam('altavien-test')
    require(BASE.is_dir() and not BASE.is_symlink() and Path(PYTHON).is_file(), 'STOP_SITE_PATH')
    require(git('rev-parse', 'HEAD').stdout.decode().strip() == BASE_COMMIT, 'STOP_PROBE_VERSION')
    require(not git('status', '--porcelain').stdout.strip(), 'STOP_PROBE_LOCAL_CHANGES')
    git('cat-file', '-e', TARGET_COMMIT + '^{commit}')
    for rel, expected in OLD.items():
        path = BASE / rel
        require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(BASE.resolve()),
                'STOP_SITE_FILE:' + rel)
        require(digest(path.read_bytes()) == expected, 'STOP_SITE_VERSION:' + rel)
    for rel in NEW.keys() - OLD.keys():
        path = BASE / rel
        require(not path.exists() and not path.is_symlink(), 'STOP_NEW_FILE_EXISTS:' + rel)
        require(path.parent.is_dir() and not path.parent.is_symlink() and
                path.parent.resolve().is_relative_to(BASE.resolve()), 'STOP_SITE_PARENT:' + rel)
    state = service()
    require(state.get('LoadState') == 'loaded' and state.get('ActiveState') == 'active' and
            state.get('SubState') == 'running' and state.get('User') == 'altavien-test', 'STOP_SERVICE_STATE')
    require(not state.get('DropInPaths'), 'STOP_SERVICE_DROPINS')
    pid = int(state.get('MainPID', '0'))
    require(pid > 1 and Path(f'/proc/{pid}').stat().st_uid == account.pw_uid, 'STOP_SERVICE_USER')
    argv = [os.fsdecode(x) for x in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0') if x]
    require('runserver' in argv and '127.0.0.1:8000' in argv and '--noreload' in argv, 'STOP_SERVICE_COMMAND')
    cwd = Path(os.readlink(f'/proc/{pid}/cwd'))
    manages = [(Path(x) if Path(x).is_absolute() else cwd / x).resolve() for x in argv if Path(x).name == 'manage.py']
    require(manages == [(BASE / 'manage.py').resolve()], 'STOP_SERVICE_MANAGE')
    executable = Path(argv[0]) if Path(argv[0]).is_absolute() else cwd / argv[0]
    require(executable.parent == Path(PYTHON).parent and executable.resolve() == Path(PYTHON).resolve(), 'STOP_SERVICE_PYTHON')
    fragment = Path(state.get('FragmentPath', ''))
    require(fragment in (TRANSIENT_UNIT, RUNTIME_UNIT) and fragment.is_file() and not fragment.is_symlink() and
            fragment.stat().st_uid == 0, 'STOP_SERVICE_FRAGMENT')
    if fragment == TRANSIENT_UNIT:
        require(not RUNTIME_UNIT.exists() and not RUNTIME_UNIT.is_symlink(), 'STOP_RUNTIME_UNIT_EXISTS')
    listeners = text(['ss', '-H', '-lnt', 'sport = :8000']).splitlines()
    require(len(listeners) == 1 and listeners[0].split()[3] == '127.0.0.1:8000', 'STOP_LISTENER')
    homepage()
    db = BASE / '.runtime/dev.sqlite3'
    require(db.is_file() and not db.is_symlink() and db.resolve().is_relative_to(BASE.resolve()), 'STOP_DATABASE_PATH')
    database_state(db)
    return account, service_environment(pid), fragment, db, pid


def atomic_write(path, content, uid, gid, mode):
    fd, name = tempfile.mkstemp(prefix='.altavien-issue-', dir=path.parent)
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


def transient_canary():
    name = f'altavien-issue-canary-{os.getpid()}.service'
    runtime = Path('/run/systemd/system') / name
    started = False
    try:
        command(['systemd-run', '--unit=' + name, '--property=Type=exec', '/bin/sleep', '120'])
        started = True
        raw = text(['systemctl', 'show', name, '-p', 'FragmentPath', '-p', 'DropInPaths'])
        props = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
        fragment = Path(props.get('FragmentPath', ''))
        require(not props.get('DropInPaths') and fragment == Path('/run/systemd/transient') / name and
                fragment.is_file() and not runtime.exists(), 'STOP_CANARY_FRAGMENT')
        payload = fragment.read_bytes()
        command(['systemctl', 'stop', name])
        atomic_write(runtime, payload, 0, 0, 0o600)
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'start', name])
        command(['systemctl', 'is-active', '--quiet', name])
    finally:
        if started:
            command(['systemctl', 'stop', name], check=False)
        if runtime.exists():
            runtime.unlink()
            command(['systemctl', 'daemon-reload'], check=False)
    print('TRANSIENT_STOP_START_CANARY_PASSED', flush=True)


def candidates():
    files = {}
    for rel, expected in NEW.items():
        data = git('show', TARGET_COMMIT + ':backend/' + rel).stdout
        require(digest(data) == expected, 'STOP_CANDIDATE_HASH:' + rel)
        files[rel] = data
    return files


def app_command(backend, env, *args):
    result = command(['runuser', '-u', 'altavien-test', '--', PYTHON, str(backend / 'manage.py'), *args],
                     env=env, timeout=120)
    return result.stdout.decode('utf-8', 'replace')


def migration_plan(backend, env, applied):
    output = app_command(backend, env, 'showmigrations', '--plan')
    pending = re.findall(r'^\[ \]\s+(\S+)', output, flags=re.MULTILINE)
    require(pending == ([] if applied else ['publishing.' + MIGRATION]), 'STOP_UNEXPECTED_PENDING_MIGRATIONS')


SMOKE = '''
import json
from django.test import Client
from django.urls import reverse
from django.utils import translation
from publishing.models import PublicationDraft, PublishedIssueProposal
with translation.override('zh-hans'):
    assert translation.gettext('Confirm issue placement') == '确认编入期次'
    assert translation.gettext('Recheck historical reviewer copy') == '重新核查历史审稿副本'
assert reverse('propose_published_issue', args=['00000000-0000-0000-0000-000000000000'])
c = Client()
assert c.get('/').status_code == 200
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


def smoke(backend, env):
    output = app_command(backend, env, 'shell', '-c', SMOKE)
    prefix = 'ALTAVIEN_SMOKE_PATHS='
    found = [line[len(prefix):] for line in output.splitlines() if line.startswith(prefix)]
    require(len(found) == 1, 'STOP_SMOKE_OUTPUT')
    return json.loads(found[0])


def owner_tree(root, uid, gid):
    for directory, dirs, files in os.walk(root, followlinks=False):
        os.chown(directory, uid, gid)
        for name in dirs + files:
            path = Path(directory) / name
            if not path.is_symlink():
                os.chown(path, uid, gid)


def stage_check(files, env, account, db_copy):
    root = Path(tempfile.mkdtemp(prefix='altavien-issue-stage-', dir='/srv'))
    try:
        stage = root / 'backend'
        shutil.copytree(BASE, stage, symlinks=True, ignore=shutil.ignore_patterns('.runtime', '__pycache__', '*.pyc'))
        runtime = stage / '.runtime'
        runtime.mkdir(mode=0o700)
        shutil.copy2(db_copy, runtime / 'dev.sqlite3')
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
        state = database_state(runtime / 'dev.sqlite3')
        migration_plan(stage, env, state['applied'])
        app_command(stage, env, 'migrate', '--noinput')
        after = database_state(runtime / 'dev.sqlite3')
        require(after['applied'] and after['counts'] == state['counts'], 'STOP_STAGE_DATABASE_CHANGE')
        app_command(stage, env, 'check')
        paths = smoke(stage, env)
        print('STAGED_PUBLISHED_ARTICLES_OK=' + str(len(paths)), flush=True)
        print('STAGED_MIGRATION_AND_CHECKS_PASSED', flush=True)
        return paths
    finally:
        shutil.rmtree(root)


def ensure_runtime_unit(fragment_backup, original_fragment):
    if original_fragment == TRANSIENT_UNIT:
        if not RUNTIME_UNIT.exists() and not RUNTIME_UNIT.is_symlink():
            atomic_write(RUNTIME_UNIT, fragment_backup.read_bytes(), 0, 0, 0o600)
        require(RUNTIME_UNIT.is_file() and not RUNTIME_UNIT.is_symlink() and
                digest(RUNTIME_UNIT.read_bytes()) == digest(fragment_backup.read_bytes()), 'STOP_RUNTIME_UNIT_CHANGED')
        command(['systemctl', 'daemon-reload'])
    else:
        require(RUNTIME_UNIT.is_file() and not RUNTIME_UNIT.is_symlink() and
                digest(RUNTIME_UNIT.read_bytes()) == digest(fragment_backup.read_bytes()), 'STOP_RUNTIME_UNIT_CHANGED')


def stop_site():
    command(['systemctl', 'stop', UNIT], timeout=45)
    state = service()
    require(state.get('ActiveState') not in ('active', 'activating', 'deactivating'), 'STOP_SERVICE_STILL_RUNNING')
    require(not text(['ss', '-H', '-lnt', 'sport = :8000']), 'STOP_LISTENER_STILL_RUNNING')


def start_site(fragment_backup, original_fragment):
    ensure_runtime_unit(fragment_backup, original_fragment)
    command(['systemctl', 'start', UNIT], timeout=45)
    for _ in range(30):
        state = service()
        if state.get('ActiveState') == 'active' and state.get('SubState') == 'running':
            try:
                homepage()
                return
            except Exception:
                pass
        time.sleep(0.5)
    raise Stop('STOP_RESTART_HEALTH')


def live_check(paths):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for article, pdf in paths:
        require(article.startswith('/journals/') and pdf.startswith('/journals/'), 'STOP_SMOKE_PATH')
        with opener.open('http://127.0.0.1:8000' + article, timeout=10) as response:
            require(response.status == 200, 'STOP_LIVE_ARTICLE')
        with opener.open('http://127.0.0.1:8000' + pdf, timeout=10) as response:
            require(response.status == 200 and response.read(5) == b'%PDF-', 'STOP_LIVE_PDF')


def apply_schema_and_check(env, before):
    migration_plan(BASE, env, before['applied'])
    app_command(BASE, env, 'migrate', '--noinput')
    after = database_state(BASE / '.runtime/dev.sqlite3')
    require(after['applied'] and after['counts'] == before['counts'], 'STOP_LIVE_DATABASE_CHANGE')
    app_command(BASE, env, 'check')
    return smoke(BASE, env)


def main():
    backup = None
    maintenance = False
    modified = []
    metadata = {}
    try:
        account, env, fragment, db, pid = preflight()
        identity = (db.stat().st_dev, db.stat().st_ino)
        files = candidates()
        transient_canary()
        backup = Path(tempfile.mkdtemp(prefix='altavien-online-first-backup-', dir='/srv'))
        os.chmod(backup, 0o700)
        fragment_backup = backup / 'service.unit'
        fragment_backup.write_bytes(fragment.read_bytes())
        os.chmod(fragment_backup, 0o600)
        for rel in OLD:
            path = BASE / rel
            stat = path.stat()
            metadata[rel] = (stat.st_uid, stat.st_gid, stat.st_mode & 0o777)
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        (backup / 'file-metadata.json').write_text(json.dumps(metadata, sort_keys=True), encoding='utf-8')
        os.chmod(backup / 'file-metadata.json', 0o600)
        snapshot(db, backup / 'db-stage.sqlite3')
        print('BACKUP_DIRECTORY=' + str(backup), flush=True)
        stage_check(files, env, account, backup / 'db-stage.sqlite3')
        _, _, current_fragment, current_db, current_pid = preflight()
        require(current_pid == pid and current_fragment == fragment and
                (current_db.stat().st_dev, current_db.stat().st_ino) == identity and
                digest(fragment.read_bytes()) == digest(fragment_backup.read_bytes()), 'STOP_SITE_CHANGED_DURING_STAGE')
        maintenance = True
        stop_site()
        require((db.stat().st_dev, db.stat().st_ino) == identity, 'STOP_DATABASE_REPLACED')
        before = snapshot(db, backup / 'db-before-maintenance.sqlite3')
        for rel, data in files.items():
            path = BASE / rel
            require(path.parent.is_dir() and not path.parent.is_symlink(), 'STOP_LIVE_PARENT:' + rel)
            uid, gid, mode = metadata.get(rel, (account.pw_uid, account.pw_gid, 0o644))
            atomic_write(path, data, uid, gid, mode)
            modified.append(rel)
        paths = apply_schema_and_check(env, before)
        start_site(fragment_backup, fragment)
        live_check(paths)
        for rel, expected in NEW.items():
            require(digest((BASE / rel).read_bytes()) == expected, 'STOP_POST_INSTALL_HASH:' + rel)
        require(database_state(db)['applied'], 'STOP_POST_INSTALL_SCHEMA')
        print('ALTAVIEN_ONLINE_FIRST_UPGRADE_READY', flush=True)
        print('BACKUP_DIRECTORY=' + str(backup), flush=True)
        return 0
    except Exception as exc:
        print('ALTAVIEN_ONLINE_FIRST_UPGRADE_FAILED=' + str(exc), flush=True)
        if maintenance:
            try:
                if modified:
                    state = service()
                    if state.get('ActiveState') in ('active', 'activating', 'deactivating'):
                        stop_site()
                    for rel in reversed(modified):
                        path = BASE / rel
                        require(path.is_file() and not path.is_symlink() and digest(path.read_bytes()) == NEW[rel],
                                'STOP_ROLLBACK_FILE_CHANGED:' + rel)
                        if rel in OLD:
                            uid, gid, mode = metadata[rel]
                            atomic_write(path, (backup / rel).read_bytes(), uid, gid, mode)
                        else:
                            path.unlink()
                state = service()
                if state.get('ActiveState') != 'active' or state.get('SubState') != 'running':
                    start_site(fragment_backup, fragment)
                else:
                    homepage()
                print('ALTAVIEN_ORIGINAL_CODE_RUNNING_DATABASE_PRESERVED', flush=True)
                print('NOTE: additive migration 0053 may remain; no database was restored over new writes.', flush=True)
            except Exception as recovery_exc:
                print('ALTAVIEN_RECOVERY_NEEDS_ATTENTION=' + str(recovery_exc), flush=True)
        if backup:
            print('BACKUP_DIRECTORY=' + str(backup), flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
