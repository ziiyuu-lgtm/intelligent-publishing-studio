import os
from pathlib import Path
import subprocess
import tempfile

from django.test import SimpleTestCase


class PostgreSQLProbeScriptSafetyTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ips-probe-script-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "backend").mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        original = Path(__file__).resolve().parents[3] / "scripts/run_altavien_postgres_probe.sh"
        script = original.read_text().replace('/srv/altavien-pg-probe/source', str(self.source))
        self.script = self.source / "scripts/run_altavien_postgres_probe.sh"
        self.script.write_text(script)
        self.log = self.root / "calls.log"
        self.stub("id", 'echo 0')
        self.stub("stat", 'echo postgres')
        self.stub("pg_isready", 'exit 0')
        self.stub("psql", 'exit 0')
        self.stub("createdb", 'exit 0')
        self.stub("python3", 'echo 0123456789abcdef01234567')
        self.stub("ss", 'printf "%s\\n" "$PROBE_FAKE_LISTENER"')
        self.stub("runuser", '''
printf '%s\\n' "$*" >> "$PROBE_FAKE_LOG"
case "$*" in
    *createdb*)
        count=0
        if [ -e "$PROBE_FAKE_COUNT" ]; then count=$(cat "$PROBE_FAKE_COUNT"); fi
        count=$((count + 1))
        echo "$count" > "$PROBE_FAKE_COUNT"
        if [ "${PROBE_FAKE_FAIL_CREATEDB:-0}" = "$count" ]; then exit 1; fi
        ;;
    *manage.py*)
        if [ "${PROBE_FAKE_FAIL_TESTS:-0}" = 1 ]; then exit 1; fi
        ;;
esac
exit 0
''')

    def stub(self, name, content):
        path = self.bin / name
        path.write_text("#!/bin/sh\nset -eu\n" + content + "\n")
        path.chmod(0o755)

    def run_probe(self, **changes):
        env = dict(os.environ, PATH=str(self.bin) + ":/usr/bin:/bin",
                   PROBE_FAKE_LOG=str(self.log), PROBE_FAKE_COUNT=str(self.root / "count"),
                   PROBE_FAKE_LISTENER="LISTEN 0 244 127.0.0.1:5432 0.0.0.0:*")
        env.update(changes)
        return subprocess.run(["/bin/bash", str(self.script)], env=env, capture_output=True, text=True, timeout=10)

    def test_nonlocal_listener_stops_before_creating_databases(self):
        result = self.run_probe(PROBE_FAKE_LISTENER="LISTEN 0 244 0.0.0.0:5432 0.0.0.0:*")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STOP_NONLOCAL_LISTENER", result.stdout)
        self.assertFalse(self.log.exists())

    def test_database_creation_failure_stops_before_tests(self):
        result = self.run_probe(PROBE_FAKE_FAIL_CREATEDB="2")
        self.assertNotEqual(result.returncode, 0)
        calls = self.log.read_text()
        self.assertEqual(calls.count("createdb"), 2)
        self.assertNotIn("manage.py", calls)
        self.assertNotIn("ALTAVIEN_POSTGRES_PROBE_PASSED", result.stdout)

    def test_failed_workflow_tests_are_not_reported_as_success(self):
        result = self.run_probe(PROBE_FAKE_FAIL_TESTS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manage.py test", self.log.read_text())
        self.assertNotIn("ALTAVIEN_POSTGRES_PROBE_PASSED", result.stdout)

    def test_success_preserves_both_new_databases(self):
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.log.read_text()
        self.assertEqual(calls.count("createdb"), 2)
        self.assertIn("altavien_probe_0123456789abcdef01234567_test", calls)
        self.assertIn("--keepdb", calls)
        self.assertNotIn("dropdb", calls)
        self.assertIn("ALTAVIEN_POSTGRES_PROBE_PASSED", result.stdout)
