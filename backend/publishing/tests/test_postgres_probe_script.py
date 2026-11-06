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
        self.stub("pg_dump", 'exit 0')
        self.stub("pg_restore", 'exit 0')
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

    def run_probe(self, *args, **changes):
        env = dict(os.environ, PATH=str(self.bin) + ":/usr/bin:/bin",
                   PROBE_FAKE_LOG=str(self.log), PROBE_FAKE_COUNT=str(self.root / "count"),
                   PROBE_FAKE_LISTENER="LISTEN 0 244 127.0.0.1:5432 0.0.0.0:*")
        env.update(changes)
        return subprocess.run(["/bin/bash", str(self.script), *args], env=env, capture_output=True, text=True, timeout=10)

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
        self.assertIn("publishing.tests.test_fictional_end_to_end", calls)
        self.assertIn("--keepdb", calls)
        self.assertNotIn("dropdb", calls)
        self.assertIn("ALTAVIEN_POSTGRES_PROBE_PASSED", result.stdout)

    def test_backup_mode_runs_only_the_dedicated_drill_and_preserves_databases(self):
        result = self.run_probe("backup-drill")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.log.read_text()
        self.assertEqual(calls.count("createdb"), 2)
        self.assertIn("ALTAVIEN_BACKUP_DRILL_ENABLED=1", calls)
        self.assertIn("publishing.tests.test_postgres_backup_drill", calls)
        self.assertNotIn("publishing.tests.test_workflow", calls)
        self.assertNotIn("dropdb", calls)
        self.assertIn("ALTAVIEN_BACKUP_DRILL_PASSED", result.stdout)

    def test_invalid_mode_fails_before_creating_databases(self):
        result = self.run_probe("unexpected-mode")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STOP_INVALID_PROBE_MODE", result.stdout)
        self.assertFalse(self.log.exists())

    def test_review_copy_mode_runs_only_its_suite_and_retains_test_databases(self):
        result = self.run_probe("review-copy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        calls = self.log.read_text()
        self.assertEqual(calls.count("createdb"), 2)
        self.assertIn("publishing.tests.test_review_copy_integrity", calls)
        self.assertNotIn("publishing.tests.test_workflow", calls)
        self.assertNotIn("dropdb", calls)
        self.assertIn("ALTAVIEN_REVIEW_COPY_PROBE_PASSED", result.stdout)

    def test_review_copy_failure_has_no_success_marker(self):
        result = self.run_probe("review-copy", PROBE_FAKE_FAIL_TESTS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ALTAVIEN_REVIEW_COPY_PROBE_PASSED", result.stdout)
        self.assertNotIn("ALTAVIEN_POSTGRES_PROBE_PASSED", result.stdout)

    def test_backup_failure_is_not_reported_as_success(self):
        result = self.run_probe("backup-drill", PROBE_FAKE_FAIL_TESTS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ALTAVIEN_BACKUP_DRILL_PASSED", result.stdout)
        self.assertNotIn("ALTAVIEN_POSTGRES_PROBE_PASSED", result.stdout)
