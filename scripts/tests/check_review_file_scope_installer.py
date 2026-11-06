"""Check both trusted installer pins and the exact old/new runtime files."""
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('review_scope_installer', root / 'scripts/install_altavien_review_file_scope.py')
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
previous = (root / 'scripts/install_altavien_issue_review_recovery.py').read_bytes()
original = (root / 'scripts/install_altavien_issue_release.py').read_bytes()
assert sha256(previous).hexdigest() == wrapper.BASE_SHA256


def git_result(args, **kwargs):
    if str(args[-1]).endswith(':scripts/install_altavien_issue_review_recovery.py'):
        return subprocess.CompletedProcess(args, 0, previous, b'')
    if str(args[-1]).endswith(':scripts/install_altavien_issue_release.py'):
        return subprocess.CompletedProcess(args, 0, original, b'')
    raise AssertionError('Unexpected command')


with patch.object(wrapper.subprocess, 'run', side_effect=git_result):
    app = wrapper.build_installer()
assert app.TARGET_COMMIT == wrapper.TARGET_COMMIT
assert app.PROFILES['previous-review-recovery']['publishing/views.py'] == wrapper.OLD_VIEWS
assert app.PROFILES['previous-review-recovery']['publishing/supporting_views.py'] == wrapper.OLD_SUPPORTING
assert app.PROFILES['current']['publishing/views.py'] == wrapper.NEW_VIEWS
assert app.PROFILES['current']['publishing/supporting_views.py'] == wrapper.NEW_SUPPORTING
assert 'publishing/views.py' not in app.GUARDS
for rel, expected in [('publishing/views.py', wrapper.NEW_VIEWS),
                      ('publishing/supporting_views.py', wrapper.NEW_SUPPORTING)]:
    assert sha256((root / 'backend' / rel).read_bytes()).hexdigest() == expected
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        candidate = subprocess.check_output(['git', 'show', wrapper.TARGET_COMMIT + ':backend/' + rel], cwd=root)
        assert sha256(candidate).hexdigest() == expected
if os.environ.get('GITHUB_ACTIONS') == 'true':
    base = subprocess.check_output(['git', 'show', wrapper.BASE_COMMIT + ':scripts/install_altavien_issue_review_recovery.py'], cwd=root)
    assert sha256(base).hexdigest() == wrapper.BASE_SHA256

for which in ('previous', 'original'):
    def tampered(args, **kwargs):
        result = git_result(args, **kwargs)
        is_previous = str(args[-1]).endswith(':scripts/install_altavien_issue_review_recovery.py')
        return subprocess.CompletedProcess(args, 0, result.stdout + b'\n# tampered' if
            ((which == 'previous') == is_previous) else result.stdout, b'')
    with patch.object(wrapper.subprocess, 'run', side_effect=tampered):
        try:
            wrapper.build_installer()
        except RuntimeError:
            pass
        else:
            raise AssertionError('Untrusted installer bytes were accepted')
print('PASS: two installer pins, old/new profiles, candidates and tampering checks')
