"""Verify the launch command against real systemd, on a disposable CI runner."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import time
import uuid

if os.environ.get("GITHUB_ACTIONS") != "true" or os.geteuid() != 0:
    raise SystemExit("This test requires a disposable GitHub Actions runner and root.")
source = (Path(__file__).resolve().parents[1] / "resume_altavien_review_file_scope.ps1").read_text()
command_line = next(line for line in source.splitlines() if line.startswith("systemd-run "))
for expected in (0, 7):
    unit = "altavien-launcher-ci-" + uuid.uuid4().hex + ".service"
    with tempfile.TemporaryDirectory(prefix="altavien-launcher-ci-") as work:
        fixture = Path(work) / "fictional.py"
        fixture.write_text(f"import time\nimport sys\ntime.sleep(0.5)\nsys.exit({expected})\n")
        args = [arg.replace("$unit", unit).replace("$installer", str(fixture))
                for arg in shlex.split(command_line)]
        try:
            subprocess.run(args, check=True, capture_output=True, timeout=10)
            result = subprocess.run(
                ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "ExecMainPID"],
                text=True, capture_output=True, check=True,
            )
            properties = dict(line.split("=", 1) for line in result.stdout.splitlines())
            assert int(properties["ExecMainPID"]) > 0, properties
            assert properties["SubState"] != "dead", properties
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                result = subprocess.run(
                    ["systemctl", "show", unit, "-p", "SubState", "-p", "Result", "-p", "ExecMainStatus"],
                    text=True, capture_output=True, check=True,
                )
                properties = dict(line.split("=", 1) for line in result.stdout.splitlines())
                if properties["SubState"] in ("exited", "failed"):
                    break
                time.sleep(0.05)
            assert int(properties["ExecMainStatus"]) == expected, properties
            assert properties["SubState"] == ("exited" if expected == 0 else "failed"), properties
            assert properties["Result"] == ("success" if expected == 0 else "exit-code"), properties
            print(f"PASS: real systemd started the process and preserved exit status {expected}")
        finally:
            subprocess.run(["systemctl", "stop", unit], check=False, capture_output=True)
            subprocess.run(["systemctl", "reset-failed", unit], check=False, capture_output=True)
