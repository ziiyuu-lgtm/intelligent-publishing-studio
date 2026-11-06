#!/usr/bin/env python3
"""Install the test-only issue-review recovery on the known test-site baseline.

The guarded installer from the previous rollout owns the maintenance, backup,
staging, preservation and rollback logic. Load its exact pinned bytes from the
test probe's Git object database; never execute a modified checkout file.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import types

PROBE = Path('/srv/altavien-pg-probe/source')
BASE_INSTALLER_COMMIT = '6639c7ffb18b3bcaed64270b0db2c83a50cdb92e'
BASE_INSTALLER_SHA256 = 'fd184f1815727b0c2bcbfd00ce8446a8ee0c2cd1e6f38ee202085cc3937f9f79'
TARGET_COMMIT = '3ffd1e2a58a8540f333d35dcfa699da467a329ac'
VIEW_SHA256 = '353c7ce2e133b4e5604bf766c80b98c8cdd90b82ed1808bce70ff042b38b5276'
TEMPLATE = 'templates/publishing/issue_release_review_expired.html'
TEMPLATE_SHA256 = 'f12fd17f865f08d9a7c7d6d25c89d7cbbef9604fc88f974f39670931d5a64c8e'


def build_installer():
    result = subprocess.run(
        ['runuser', '-u', 'postgres', '--', 'git', '-C', str(PROBE), 'show',
         BASE_INSTALLER_COMMIT + ':scripts/install_altavien_issue_release.py'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )
    if result.returncode or hashlib.sha256(result.stdout).hexdigest() != BASE_INSTALLER_SHA256:
        raise RuntimeError('STOP_BASE_INSTALLER_INTEGRITY')
    installer = types.ModuleType('altavien_pinned_issue_installer')
    exec(compile(result.stdout, '<verified-issue-installer>', 'exec'), installer.__dict__)

    old = dict(installer.PROFILES['current'])
    old[TEMPLATE] = None
    new = dict(old)
    new['publishing/issue_views.py'] = VIEW_SHA256
    new[TEMPLATE] = TEMPLATE_SHA256
    installer.TARGET_COMMIT = TARGET_COMMIT
    installer.PROFILES = {'previous-review': old, 'current': new}
    installer.NEW = new
    # The previous installer already checks site ownership, loopback, service,
    # code hashes, no pending migrations, records, saved order and media bytes.
    installer.SMOKE += '''\nfrom django.template.loader import get_template\nget_template('publishing/issue_release_review_expired.html')\n'''
    return installer


def main():
    if sys.argv[1:] not in ([], ['--check']):
        raise SystemExit('Use no arguments to upgrade, or --check for read-only preflight.')
    if os.geteuid() != 0:
        print('STOP_REQUIRES_ROOT', flush=True)
        return 1
    try:
        installer = build_installer()
    except Exception as exc:
        code = str(exc) if str(exc) == 'STOP_BASE_INSTALLER_INTEGRITY' else type(exc).__name__
        print('ALTAVIEN_REVIEW_RECOVERY_PREPARATION_FAILED=' + code, flush=True)
        return 1
    return installer.main(check_only=sys.argv[1:] == ['--check'])


if __name__ == '__main__':
    sys.exit(main())
