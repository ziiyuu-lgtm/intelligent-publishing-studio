"""Check the trusted installer pin and the narrowly scoped version profiles."""
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

root = Path(__file__).resolve().parents[2]
path = root / 'scripts/install_altavien_issue_review_recovery.py'
spec = importlib.util.spec_from_file_location('review_recovery_installer', path)
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
base = (root / 'scripts/install_altavien_issue_release.py').read_bytes()
assert sha256(base).hexdigest() == wrapper.BASE_INSTALLER_SHA256
if os.environ.get('GITHUB_ACTIONS') == 'true':
    from_git = subprocess.check_output(
        ['git', 'show', wrapper.BASE_INSTALLER_COMMIT + ':scripts/install_altavien_issue_release.py'], cwd=root,
    )
    assert sha256(from_git).hexdigest() == wrapper.BASE_INSTALLER_SHA256
    for rel, expected in [('publishing/issue_views.py', wrapper.VIEW_SHA256),
                          (wrapper.TEMPLATE, wrapper.TEMPLATE_SHA256)]:
        candidate = subprocess.check_output(['git', 'show', wrapper.TARGET_COMMIT + ':backend/' + rel], cwd=root)
        assert sha256(candidate).hexdigest() == expected

with patch.object(wrapper.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, base, b'')):
    app = wrapper.build_installer()
assert app.TARGET_COMMIT == wrapper.TARGET_COMMIT
assert app.PROFILES['previous-review'][wrapper.TEMPLATE] is None
assert app.PROFILES['current'][wrapper.TEMPLATE] == wrapper.TEMPLATE_SHA256
assert app.PROFILES['current']['publishing/issue_views.py'] == wrapper.VIEW_SHA256
for name in app.PROFILES['previous-review']:
    if name not in (wrapper.TEMPLATE, 'publishing/issue_views.py'):
        assert app.PROFILES['previous-review'][name] == app.PROFILES['current'][name]
for rel, expected in [('publishing/issue_views.py', wrapper.VIEW_SHA256),
                      (wrapper.TEMPLATE, wrapper.TEMPLATE_SHA256)]:
    assert sha256((root / 'backend' / rel).read_bytes()).hexdigest() == expected

for result in (subprocess.CompletedProcess([], 1, base, b''),
               subprocess.CompletedProcess([], 0, base + b'\n# modified', b'')):
    with patch.object(wrapper.subprocess, 'run', return_value=result):
        try:
            wrapper.build_installer()
        except RuntimeError as exc:
            assert str(exc) == 'STOP_BASE_INSTALLER_INTEGRITY'
        else:
            raise AssertionError('Untrusted installer bytes were accepted')
print('PASS: pinned installer integrity, exact target files and rejected tampering')
