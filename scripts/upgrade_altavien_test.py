#!/usr/bin/env python3
"""Upgrade only the isolated, fictional Altavien 8000 test service.

This is not a production installer. It snapshots the current SQLite database,
copies the private runtime, rehearses migrations, and preserves the old backend.
"""
import hashlib
from contextlib import closing
import os
import pwd
import select
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = '9d075b62e518e32ad82f6023a85a603dfc807743'
CANDIDATE = 'a9fdf7ed32a9aad9435166f974c8bdd8bcc79875'
PROOF = 'backend/publishing/production_views.py'
EXCLUDED_DB = {'dev.sqlite3', 'dev.sqlite3-wal', 'dev.sqlite3-shm', 'dev.sqlite3-journal'}
MIGRATIONS = (
    '0048_local_login_throttle', '0049_local_review_mail', '0050_local_test_registration',
    '0051_review_copy_checked_sha256', '0052_review_metadata_checked_sha256',
)


def run(*args, capture=False):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def db_open(path):
    return sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def database_check(path, upgraded):
    with closing(db_open(path)) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity check failed')
        applied = {r[0] for r in db.execute('SELECT name FROM django_migrations WHERE app=?', ('publishing',))}
        if '0047_journalaipolicy_keyword_suggestions_enabled_and_more' not in applied:
            raise RuntimeError('The expected starting migration is missing')
        if any((name in applied) != upgraded for name in MIGRATIONS):
            raise RuntimeError('The migration state does not match the expected stage')
        cols = {r[1] for r in db.execute('PRAGMA table_info(publishing_submissionversion)')}
        for col in ('review_copy_checked_sha256', 'review_metadata_checked_sha256'):
            if (col in cols) != upgraded:
                raise RuntimeError('The review database columns are not in the expected state')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise RuntimeError('SQLite foreign key check failed')


def compare_existing_rows(before, after):
    """Compare every old row using old columns, excluding migration-created permissions."""
    ignore = {'django_migrations', 'django_content_type', 'auth_permission', 'sqlite_sequence'}
    with closing(db_open(before)) as left, closing(db_open(after)) as right:
        tables = [r[0] for r in left.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if r[0] not in ignore and not r[0].startswith('sqlite_')]
        for table in tables:
            qtable = quote_identifier(table)
            old_cols = [r[1] for r in left.execute(f'PRAGMA table_info({qtable})')]
            new_cols = {r[1] for r in right.execute(f'PRAGMA table_info({qtable})')}
            if not set(old_cols) <= new_cols:
                raise RuntimeError('An existing database column was not preserved')
            cols = ', '.join(map(quote_identifier, old_cols))
            query = f'SELECT {cols} FROM {qtable}'
            old_rows = sorted(repr(row) for row in left.execute(query))
            new_rows = sorted(repr(row) for row in right.execute(query))
            if old_rows != new_rows:
                raise RuntimeError('Existing database records changed during rehearsal')
    print(f'EXISTING_TABLES_PRESERVED={len(tables)}', flush=True)


def compare_original_to_snapshot(original, snapshot):
    """Refuse cutover if another writer changed the old database after backup."""
    with closing(db_open(original)) as left, closing(db_open(snapshot)) as right:
        left.execute('BEGIN')
        right.execute('BEGIN')
        schema_query = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
        left_schema = list(left.execute(schema_query))
        if left_schema != list(right.execute(schema_query)):
            raise RuntimeError('The original database schema changed after the snapshot')
        for kind, name, _, _ in left_schema:
            if kind != 'table':
                continue
            query = 'SELECT * FROM ' + quote_identifier(name)
            if sorted(repr(row) for row in left.execute(query)) != sorted(repr(row) for row in right.execute(query)):
                raise RuntimeError('The original database changed after the snapshot')


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def runtime_manifest(root):
    result = {}
    for current, dirs, files in os.walk(root, followlinks=False):
        directory = Path(current)
        for name in dirs:
            path = directory / name
            if path.is_symlink():
                raise RuntimeError('Runtime contains a symbolic-link directory')
        for name in files:
            path = directory / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise RuntimeError('Runtime contains a non-regular file')
            if directory == root and name in EXCLUDED_DB:
                continue
            result[str(path.relative_to(root))] = file_digest(path)
    return result


def compare_runtime(source, target):
    if runtime_manifest(source) != runtime_manifest(target):
        raise RuntimeError('Copied runtime files do not match the original')


class SystemdService:
    def __init__(self, site, username):
        self.site = site
        self.username = username
        self.name = 'altavien-test-web.service'

    def value(self, name):
        return run('systemctl', 'show', '--value', '-p', name, self.name, capture=True).stdout.decode().strip()

    def active(self):
        return subprocess.run(['systemctl', 'is-active', '--quiet', self.name]).returncode == 0

    def validate(self):
        if not self.active() or self.value('CanStart') != 'yes' or self.value('CanStop') != 'yes':
            raise RuntimeError('The test service is not in the expected active state')
        pid = int(self.value('MainPID'))
        proc = Path('/proc') / str(pid)
        if pid <= 0 or proc.stat().st_uid != pwd.getpwnam(self.username).pw_uid:
            raise RuntimeError('Unexpected test service process owner')
        cmdline = (proc / 'cmdline').read_bytes().split(b'\0')
        if str(self.site / 'backend/manage.py').encode() not in cmdline or b'127.0.0.1:8000' not in cmdline:
            raise RuntimeError('Unexpected test service process command')
        listeners = run('ss', '-H', '-lntp', capture=True).stdout.decode().splitlines()
        matches = [line for line in listeners if '127.0.0.1:8000 ' in line]
        if len(matches) != 1 or f'pid={pid},' not in matches[0]:
            raise RuntimeError('Unexpected test service port 8000 listener')
        return pid

    def stop(self):
        run('systemctl', 'stop', self.name)
        if self.active():
            raise RuntimeError('The original test service did not stop')

    def start(self):
        run('systemctl', 'start', self.name)

    def healthy(self):
        for _ in range(10):
            if self.active():
                try:
                    with urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3) as response:
                        if response.status == 200:
                            return True
                except (OSError, urllib.error.URLError):
                    pass
            time.sleep(1)
        return False


class Upgrade:
    def __init__(self, site, repo, stage, backup, python, service, *, user='altavien-test',
                 git_user='postgres', base=BASE, candidate=CANDIDATE, expected_head=CANDIDATE,
                 set_owner=True):
        self.site, self.repo, self.stage, self.backup = map(Path, (site, repo, stage, backup))
        self.python, self.service = Path(python), service
        self.user, self.git_user = user, git_user
        self.base, self.candidate, self.expected_head = base, candidate, expected_head
        self.set_owner = set_owner
        self.old_moved = self.new_moved = self.stop_attempted = self.start_attempted = False
        self.guard = None
        self.guard_pid = None

    def git(self, *args):
        return run('runuser', '-u', self.git_user, '--', 'git', '-C', str(self.repo), *args, capture=True).stdout

    def tree(self, rev):
        result = {}
        for raw in self.git('ls-tree', '-r', '-z', rev, '--', 'backend').split(b'\0'):
            if not raw:
                continue
            info, path = raw.split(b'\t', 1)
            mode, kind, digest = info.decode().split()
            if kind != 'blob' or mode == '120000':
                raise RuntimeError('The source tree contains an unexpected file type')
            result[path.decode()] = digest
        if not result:
            raise RuntimeError('The expected source tree is empty')
        return result

    def verify_sources(self):
        if self.git('rev-parse', 'HEAD').decode().strip() != self.expected_head or self.git('status', '--porcelain'):
            raise RuntimeError('Probe checkout is not clean at the expected revision')
        base, candidate = self.tree(self.base), self.tree(self.candidate)
        if PROOF not in base or PROOF not in candidate:
            raise RuntimeError('Publication source is missing')
        found = set()
        backend = self.site / 'backend'
        for current, dirs, files in os.walk(backend, followlinks=False):
            parent = Path(current)
            dirs[:] = [name for name in dirs if name not in ('.runtime', '__pycache__')]
            for name in dirs:
                if (parent / name).is_symlink():
                    raise RuntimeError('Source tree contains a symbolic-link directory')
            for name in files:
                path = parent / name
                if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                    raise RuntimeError('Source tree contains a non-regular file')
                relative = str(path.relative_to(self.site))
                found.add(relative)
                if relative not in base:
                    raise RuntimeError('Unexpected local source file: ' + relative)
                data = path.read_bytes()
                digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
                expected = candidate[relative] if relative == PROOF else base[relative]
                if digest != expected:
                    raise RuntimeError('Local source differs from the expected version: ' + relative)
        if found != set(base):
            raise RuntimeError('One or more expected source files are missing')

    def prepare(self):
        if os.geteuid() != 0:
            raise RuntimeError('This test-site upgrade must run as root')
        if self.stage.exists() or self.stage.is_symlink() or self.backup.exists() or self.backup.is_symlink():
            raise RuntimeError('The upgrade staging or backup path already exists')
        backend = self.site / 'backend'
        runtime = backend / '.runtime'
        database = runtime / 'dev.sqlite3'
        for path in (self.site, backend, runtime, database):
            if path.is_symlink() or not path.exists():
                raise RuntimeError('An expected test-site path is missing or symbolic')
        if not self.python.is_file() or not os.access(self.python, os.X_OK):
            raise RuntimeError('The test Python interpreter is unavailable')
        if not shutil.which('fuser'):
            raise RuntimeError('The fuser command is required to check for other database users')
        if self.stage.parent.stat().st_dev != backend.stat().st_dev or self.backup.parent.stat().st_dev != backend.stat().st_dev:
            raise RuntimeError('The backend and upgrade directories are on different filesystems')
        self.verify_sources()
        database_check(database, upgraded=False)
        runtime_manifest(runtime)
        self.pid = self.service.validate()
        runtime_size = sum(p.stat().st_size for p in runtime.rglob('*') if p.is_file())
        if shutil.disk_usage(self.stage.parent).free < 2 * runtime_size + 64 * 1024 * 1024:
            raise RuntimeError('There is not enough free space for the isolated copy')
        print('PRECHECKS_PASSED', flush=True)

    def chown(self, root):
        if not self.set_owner:
            return
        identity = pwd.getpwnam(self.user)
        for current, dirs, files in os.walk(root, followlinks=False):
            for name in ['.', *dirs, *files]:
                path = Path(current) / name
                if path.is_symlink():
                    raise RuntimeError('A staging path unexpectedly became symbolic')
                os.chown(path, identity.pw_uid, identity.pw_gid)

    def archive_candidate(self):
        self.backup.mkdir(mode=0o700)
        self.stage.mkdir(mode=0o700)
        archive = subprocess.Popen(['runuser', '-u', self.git_user, '--', 'git', '-C', str(self.repo),
                                    'archive', self.candidate, 'backend'], stdout=subprocess.PIPE)
        try:
            result = subprocess.run(['tar', '-xf', '-', '-C', str(self.stage)], stdin=archive.stdout, check=False)
        finally:
            archive.stdout.close()
            archive_status = archive.wait()
        if result.returncode or archive_status:
            raise RuntimeError('Unable to extract the verified source')
        self.chown(self.stage)
        print('CANDIDATE_PREPARED', flush=True)

    def check_database_handles(self, allowed=()):
        database = self.site / 'backend/.runtime/dev.sqlite3'
        allowed = set(allowed)
        for suffix in ('', '-wal', '-shm', '-journal'):
            path = Path(str(database) + suffix)
            if not path.exists():
                continue
            result = subprocess.run(['fuser', str(path)], capture_output=True, text=True)
            if result.returncode not in (0, 1):
                raise RuntimeError('Unable to check database users')
            if 'permission denied' in result.stderr.lower() or 'cannot stat' in result.stderr.lower():
                raise RuntimeError('Unable to check database users')
            if result.returncode == 1 and result.stderr.strip():
                raise RuntimeError('Unable to check database users')
            try:
                pids = {int(value) for value in result.stdout.split()}
            except ValueError as exc:
                raise RuntimeError('Unable to interpret database users') from exc
            if pids - allowed:
                raise RuntimeError('Another process has the test database open')

    def acquire_guard(self):
        code = '''import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1], timeout=10, isolation_level=None)
db.execute("BEGIN IMMEDIATE")
print("READY", os.readlink("/proc/self"), flush=True)
sys.stdin.read(1)
db.rollback()
db.close()
'''
        database = self.site / 'backend/.runtime/dev.sqlite3'
        self.guard = subprocess.Popen(['runuser', '-u', self.user, '--', str(self.python), '-c', code, str(database)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        readable, _, _ = select.select([self.guard.stdout], [], [], 20)
        if not readable:
            raise RuntimeError('Timed out while protecting the original database')
        parts = self.guard.stdout.readline().split()
        if len(parts) != 2 or parts[0] != 'READY' or not parts[1].isdigit():
            raise RuntimeError('Unable to protect the original database')
        self.guard_pid = int(parts[1])
        self.check_database_handles({self.guard_pid})

    def release_guard(self):
        if self.guard is None:
            return
        guard = self.guard
        if guard.stdin and not guard.stdin.closed:
            guard.stdin.close()
        try:
            guard.wait(timeout=15)
        except subprocess.TimeoutExpired:
            guard.terminate()
            guard.wait(timeout=5)
            raise RuntimeError('Database guard did not exit cleanly')
        finally:
            if guard.stdout:
                guard.stdout.close()
            if guard.stderr:
                guard.stderr.close()
            self.guard = None
            self.guard_pid = None
        if guard.returncode != 0:
            raise RuntimeError('Database guard exited unexpectedly')

    def snapshot_and_rehearse(self):
        old_runtime = self.site / 'backend/.runtime'
        staged_runtime = self.stage / 'backend/.runtime'
        self.verify_sources()
        database_check(old_runtime / 'dev.sqlite3', upgraded=False)
        self.check_database_handles()
        before_files = runtime_manifest(old_runtime)
        def skip_database(src, names):
            return EXCLUDED_DB.intersection(names) if Path(src) == old_runtime else set()
        shutil.copytree(old_runtime, staged_runtime, ignore=skip_database)
        snapshot = self.backup / 'database-before.sqlite3'
        with closing(db_open(old_runtime / 'dev.sqlite3')) as source, closing(sqlite3.connect(snapshot)) as dest:
            source.backup(dest)
        os.chmod(snapshot, 0o600)
        database_check(snapshot, upgraded=False)
        self.acquire_guard()
        compare_original_to_snapshot(old_runtime / 'dev.sqlite3', snapshot)
        shutil.copy2(snapshot, staged_runtime / 'dev.sqlite3')
        os.chmod(staged_runtime / 'dev.sqlite3', (old_runtime / 'dev.sqlite3').stat().st_mode & 0o777)
        if before_files != runtime_manifest(old_runtime) or before_files != runtime_manifest(staged_runtime):
            raise RuntimeError('Runtime files changed while copying')
        self.chown(self.stage)
        env = ['env', 'PYTHONDONTWRITEBYTECODE=1', 'IPS_LOCAL_PUBLICATION_ENABLED=1']
        manage = str(self.stage / 'backend/manage.py')
        run('runuser', '-u', self.user, '--', *env, str(self.python), manage, 'migrate', '--noinput', '--verbosity', '1')
        run('runuser', '-u', self.user, '--', *env, str(self.python), manage, 'check')
        migrated = staged_runtime / 'dev.sqlite3'
        database_check(migrated, upgraded=True)
        compare_existing_rows(snapshot, migrated)
        compare_runtime(old_runtime, staged_runtime)
        self.smoke()
        if self.guard.poll() is not None:
            raise RuntimeError('Database guard ended before cutover')
        self.check_database_handles({self.guard_pid})
        compare_original_to_snapshot(old_runtime / 'dev.sqlite3', snapshot)
        print('STAGED_MIGRATION_AND_CHECKS_PASSED', flush=True)

    def smoke(self):
        code = '''import os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django; django.setup()
from django.test import Client
from django.urls import reverse
from publishing.models import PublicationDraft
c = Client()
assert c.get("/").status_code == 200
drafts = PublicationDraft.objects.filter(published_at__isnull=False, published_proof__isnull=False,
    submission__journal__active=True, submission__journal__organisation__active=True).select_related("submission__journal")
total = 0
for draft in drafts:
    page = c.get(reverse("test_article", args=[draft.submission.journal.slug, draft.pk]))
    pdf = c.get(reverse("test_article_pdf", args=[draft.submission.journal.slug, draft.pk]))
    if page.status_code == 200 and pdf.status_code == 200:
        assert b"".join(pdf.streaming_content).startswith(b"%PDF-")
        total += 1
if drafts.exists() and not total: raise RuntimeError("No existing published test PDF passed")
print("STAGED_PUBLISHED_ARTICLES_OPEN=" + str(total))
'''
        env = ['env', 'PYTHONDONTWRITEBYTECODE=1', 'IPS_LOCAL_PUBLICATION_ENABLED=1',
               'PYTHONPATH=' + str(self.stage / 'backend')]
        run('runuser', '-u', self.user, '--', *env, str(self.python), '-c', code)

    def rollback(self):
        if self.start_attempted:
            if self.service.active():
                self.service.stop()
        if self.new_moved:
            os.rename(self.site / 'backend', self.backup / 'failed-new-backend')
            self.new_moved = False
        if self.old_moved:
            os.rename(self.backup / 'previous-backend', self.site / 'backend')
            self.old_moved = False
        if self.stop_attempted and not self.service.active():
            self.service.start()
        if self.stop_attempted and not self.service.healthy():
            raise RuntimeError('The restored test service did not pass the health check')

    def execute(self):
        self.prepare()
        self.archive_candidate()
        try:
            if self.service.validate() != self.pid:
                raise RuntimeError('The test service process changed during preparation')
            self.stop_attempted = True
            self.service.stop()
            self.snapshot_and_rehearse()
            shutil.copystat(self.site / 'backend', self.stage / 'backend', follow_symlinks=False)
            self.chown(self.stage)
            os.rename(self.site / 'backend', self.backup / 'previous-backend')
            self.old_moved = True
            # Close the old SQLite connection while the original path is absent.
            # SQLite may clean up sidecars using its original pathname on close.
            self.release_guard()
            os.rename(self.stage / 'backend', self.site / 'backend')
            self.new_moved = True
            self.start_attempted = True
            self.service.start()
            if not self.service.healthy():
                raise RuntimeError('The updated test service did not pass the health check')
        except Exception as exc:
            print('ALTAVIEN_UPGRADE_FAILED:', type(exc).__name__, str(exc), flush=True)
            try:
                try:
                    self.release_guard()
                except Exception as guard_exc:
                    print('ALTAVIEN_GUARD_RELEASE_WARNING:', type(guard_exc).__name__, str(guard_exc), flush=True)
                self.rollback()
                print('ALTAVIEN_PREVIOUS_TEST_SITE_RESTORED', flush=True)
            except Exception as rollback_exc:
                print('ALTAVIEN_RECOVERY_NEEDS_ATTENTION:', type(rollback_exc).__name__, str(rollback_exc), flush=True)
            print('BACKUP_DIRECTORY=' + str(self.backup), flush=True)
            raise
        print('ALTAVIEN_REVIEW_PROTECTION_UPGRADE_READY', flush=True)
        print('BACKUP_DIRECTORY=' + str(self.backup), flush=True)
        print('LEGACY_REVIEW_COPIES_REQUIRE_EDITOR_RECHECK', flush=True)


def main():
    def interrupted(signum, frame):
        raise RuntimeError('Upgrade interrupted by signal ' + str(signum))
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    site = Path('/srv/altavien-test')
    upgrader = Upgrade(site, '/srv/altavien-pg-probe/source', '/srv/altavien-review-stage-a9fdf7',
        '/srv/altavien-review-backup-a9fdf7', site / '.venv/bin/python', SystemdService(site, 'altavien-test'))
    upgrader.execute()


if __name__ == '__main__':
    main()
