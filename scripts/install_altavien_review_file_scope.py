#!/usr/bin/env python3
"""Install the fictional test site's reviewer file-scope fix on a known baseline.

Reuse the exact previously verified recovery installer, including its original
guarded staging, backup, shared lock, preservation and rollback logic.
"""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import types

PROBE = Path('/srv/altavien-pg-probe/source')
BASE_COMMIT = '3f1176edcf6bb7564cfa9b1acb37e1f70efb1a64'
BASE_SHA256 = 'b4732d7d4263e92b94050db33789c2bcbfa7e7bea359c95ccd667eb68ccbfa83'
TARGET_COMMIT = '42749acdeefa706a8ada8d0710db97706915b722'
OLD_VIEWS = '60f45294b0b4e2c0c0d17262c0e2a0f3051799b9f62d9901c426c1338873283b'
OLD_SUPPORTING = 'f7d025fcb0557314c905fba99523871371a03f883fe0761021605f6756ddacd3'
NEW_VIEWS = '86b4cc3e10fe778d2dc80c10c94d880d0f5aace75308a2a207a3063705ac73f2'
NEW_SUPPORTING = '292254c1bc34a0c36f2d3bea96910aa18b5c6af2fe38a3b6e7971ca82bebf708'


def build_installer():
    result = subprocess.run(
        ['runuser', '-u', 'postgres', '--', 'git', '-C', str(PROBE), 'show',
         BASE_COMMIT + ':scripts/install_altavien_issue_review_recovery.py'],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False,
    )
    if result.returncode or hashlib.sha256(result.stdout).hexdigest() != BASE_SHA256:
        raise RuntimeError('STOP_RECOVERY_INSTALLER_INTEGRITY')
    previous = types.ModuleType('altavien_pinned_review_recovery_installer')
    exec(compile(result.stdout, '<verified-review-recovery-installer>', 'exec'), previous.__dict__)
    installer = previous.build_installer()
    old = dict(installer.PROFILES['current'])
    if installer.GUARDS.get('publishing/views.py') != OLD_VIEWS:
        raise RuntimeError('STOP_UNEXPECTED_BASE_GUARD')
    installer.GUARDS = dict(installer.GUARDS)
    del installer.GUARDS['publishing/views.py']
    old.update({'publishing/views.py': OLD_VIEWS, 'publishing/supporting_views.py': OLD_SUPPORTING})
    new = dict(old)
    new.update({'publishing/views.py': NEW_VIEWS, 'publishing/supporting_views.py': NEW_SUPPORTING})
    installer.TARGET_COMMIT = TARGET_COMMIT
    installer.PROFILES = {'previous-review-recovery': old, 'current': new}
    installer.NEW = new
    installer.SMOKE += '''\nfrom publishing.views import _downloadable_review_round\nclass FictionalClosedSubmission:\n    status = 'accepted'\nassert _downloadable_review_round(FictionalClosedSubmission()) is None\n'''
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
        allowed = {'STOP_RECOVERY_INSTALLER_INTEGRITY', 'STOP_BASE_INSTALLER_INTEGRITY', 'STOP_UNEXPECTED_BASE_GUARD'}
        code = str(exc) if str(exc) in allowed else type(exc).__name__
        print('ALTAVIEN_REVIEW_FILE_SCOPE_PREPARATION_FAILED=' + code, flush=True)
        return 1
    return installer.main(check_only=sys.argv[1:] == ['--check'])


if __name__ == '__main__':
    sys.exit(main())
