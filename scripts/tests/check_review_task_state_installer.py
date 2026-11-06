"""Verify every inherited installer pin and both exact test-site profiles."""
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

root = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('task_state_installer', root / 'scripts/install_altavien_review_task_state.py')
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
files = {
    'scripts/install_altavien_review_file_scope.py': (root / 'scripts/install_altavien_review_file_scope.py').read_bytes(),
    'scripts/install_altavien_issue_review_recovery.py': (root / 'scripts/install_altavien_issue_review_recovery.py').read_bytes(),
    'scripts/install_altavien_issue_release.py': (root / 'scripts/install_altavien_issue_release.py').read_bytes(),
}
assert sha256(files['scripts/install_altavien_review_file_scope.py']).hexdigest() == wrapper.BASE_SHA256


def git_result(args, **kwargs):
    ref = str(args[-1])
    for path, data in files.items():
        if ref.endswith(':' + path):
            return subprocess.CompletedProcess(args, 0, data, b'')
    raise AssertionError('Unexpected command')


with patch.object(wrapper.subprocess, 'run', side_effect=git_result):
    installer = wrapper.build_installer()
old = installer.PROFILES['previous-file-scope']
new = installer.PROFILES['current']
assert installer.TARGET_COMMIT == wrapper.TARGET_COMMIT
assert old[wrapper.VIEWS] == wrapper.OLD_VIEWS and old[wrapper.TEMPLATE] == wrapper.OLD_TEMPLATE
assert new[wrapper.VIEWS] == wrapper.NEW_VIEWS and new[wrapper.TEMPLATE] == wrapper.NEW_TEMPLATE
assert set(old) == set(new)
assert {path for path in old if old[path] != new[path]} == {wrapper.VIEWS, wrapper.TEMPLATE}
assert wrapper.VIEWS not in installer.GUARDS and wrapper.TEMPLATE not in installer.GUARDS
if os.environ.get('GITHUB_ACTIONS') == 'true':
    for path, expected in ((wrapper.VIEWS, wrapper.NEW_VIEWS), (wrapper.TEMPLATE, wrapper.NEW_TEMPLATE)):
        data = subprocess.check_output(['git', 'show', wrapper.TARGET_COMMIT + ':backend/' + path], cwd=root)
        assert sha256(data).hexdigest() == expected
    data = subprocess.check_output(['git', 'show', wrapper.BASE_COMMIT + ':scripts/install_altavien_review_file_scope.py'], cwd=root)
    assert sha256(data).hexdigest() == wrapper.BASE_SHA256

for bad_path in files:
    def tampered(args, **kwargs):
        result = git_result(args, **kwargs)
        return subprocess.CompletedProcess(args, 0,
            result.stdout + b'\n# tampered' if str(args[-1]).endswith(':' + bad_path) else result.stdout, b'')
    with patch.object(wrapper.subprocess, 'run', side_effect=tampered):
        try:
            wrapper.build_installer()
        except RuntimeError:
            pass
        else:
            raise AssertionError('Untrusted installer bytes were accepted: ' + bad_path)
print('PASS: three installer pins, exact old/new profiles and rejected tampering')
