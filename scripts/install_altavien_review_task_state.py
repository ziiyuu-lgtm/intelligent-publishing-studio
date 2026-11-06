#!/usr/bin/env python3
"""Guarded reviewer task-page update for the existing fictional test site.

The preceding installer is loaded from the probe's Git object database only
after its bytes match the pin. Its inherited staging, backup, exclusive lock,
preservation, restart and rollback checks are retained unchanged.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import types

PROBE = Path('/srv/altavien-pg-probe/source')
BASE_COMMIT = 'c52d011953cdf15fdad2a667a39a341e91771308'
BASE_SHA256 = '2b4563cfd1402078962c3f41cbc5e44b28f367b7b7662cd6091f3d96fa45458f'
TARGET_COMMIT = '833b4ab6e98d4ad9abf1fdcc9029171f38a0f447'
VIEWS = 'publishing/views.py'
TEMPLATE = 'templates/publishing/review_detail.html'
OLD_VIEWS = '86b4cc3e10fe778d2dc80c10c94d880d0f5aace75308a2a207a3063705ac73f2'
OLD_TEMPLATE = '64d89e9f79a89c5f4a31fb7e70983678aab2cd84853ca64e64670489f4c81a63'
NEW_VIEWS = '3f1514e44a5d4e0481950bd0fc1fd18d08d712dfbe6ae5e71142d88e374eea2b'
NEW_TEMPLATE = '84ed5accdc86f285b1bde6f9b301dc6dbc4343f1d27b5a725fbf1985fef027d3'


def build_installer():
    result = subprocess.run(
        ['runuser', '-u', 'postgres', '--', 'git', '-C', str(PROBE), 'show',
         BASE_COMMIT + ':scripts/install_altavien_review_file_scope.py'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )
    if result.returncode or hashlib.sha256(result.stdout).hexdigest() != BASE_SHA256:
        raise RuntimeError('STOP_FILE_SCOPE_INSTALLER_INTEGRITY')
    previous = types.ModuleType('altavien_pinned_review_file_scope_installer')
    exec(compile(result.stdout, '<verified-review-file-scope-installer>', 'exec'), previous.__dict__)
    installer = previous.build_installer()
    old = dict(installer.PROFILES['current'])
    if old.get(VIEWS) != OLD_VIEWS or TEMPLATE in old or TEMPLATE in installer.GUARDS:
        raise RuntimeError('STOP_UNEXPECTED_BASE_GUARD')
    old[TEMPLATE] = OLD_TEMPLATE
    new = dict(old)
    new.update({VIEWS: NEW_VIEWS, TEMPLATE: NEW_TEMPLATE})
    installer.TARGET_COMMIT = TARGET_COMMIT
    installer.PROFILES = {'previous-file-scope': old, 'current': new}
    installer.NEW = new
    installer.SMOKE += '''\nfrom django.template.loader import get_template\nfrom publishing.views import _review_detail_context\nget_template('publishing/review_detail.html')\n'''
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
        allowed = {'STOP_FILE_SCOPE_INSTALLER_INTEGRITY', 'STOP_RECOVERY_INSTALLER_INTEGRITY',
                   'STOP_BASE_INSTALLER_INTEGRITY', 'STOP_UNEXPECTED_BASE_GUARD'}
        code = str(exc) if str(exc) in allowed else type(exc).__name__
        print('ALTAVIEN_REVIEW_TASK_STATE_PREPARATION_FAILED=' + code, flush=True)
        return 1
    return installer.main(check_only=sys.argv[1:] == ['--check'])


if __name__ == '__main__':
    sys.exit(main())
