"""Run the delivered Bash payload against a disposable simulated host."""
from pathlib import Path
import hashlib
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
source = (root / "resume_altavien_issue_release.ps1").read_text()
payload = source.split("$remoteScript = @'\n", 1)[1].split("\n'@\n", 1)[0] + "\n"
subprocess.run(["bash", "-n"], input=payload, text=True, check=True)
repository = root.parent
installer = subprocess.check_output(
    ["git", "show", "6639c7ffb18b3bcaed64270b0db2c83a50cdb92e:scripts/install_altavien_issue_release.py"],
    cwd=repository,
)
assert hashlib.sha256(installer).hexdigest() == "fd184f1815727b0c2bcbfd00ce8446a8ee0c2cd1e6f38ee202085cc3937f9f79"

mock = r'''
id() {
  if [ "$CASE" = nonroot ]; then echo 1000; else echo 0; fi
}
runuser() {
  case "$*" in
    *"remote get-url origin")
      if [ "$CASE" = repository ]; then echo https://example.invalid/wrong.git
      else echo https://github.com/ziiyuu-lgtm/intelligent-publishing-studio.git; fi ;;
    *"fetch origin "*)
      if [ "$CASE" = fetch ]; then return 1; fi ;;
    *"merge-base --is-ancestor "*)
      if [ "$CASE" = ancestry ]; then return 1; fi ;;
    *"show "*)
      cat "$CASE_ROOT/source-installer"
      if [ "$CASE" = hash ]; then echo '# tampered'; fi ;;
    *) echo "Unexpected runuser call" >&2; return 90 ;;
  esac
}
mktemp() { /usr/bin/mktemp "$CASE_ROOT/installer.XXXXXXXX.py"; }
python3() {
  echo check >> "$CASE_ROOT/checks"
  if [ "$CASE" = preflight ]; then return 1; fi
  if [ "$CASE" = postflight ] && [ "$(wc -l < "$CASE_ROOT/checks")" -eq 2 ]; then return 1; fi
}
systemd-run() {
  printf '%s\n' "$*" > "$CASE_ROOT/launch"
  if [ "$CASE" = exec_failure ]; then return 1; fi
  case "$*" in
    *--no-block*) echo queued > "$CASE_ROOT/state" ;;
    *--property=Type=exec*) echo running > "$CASE_ROOT/state" ;;
    *) echo "Unexpected service type" >&2; return 91 ;;
  esac
}
systemctl() {
  case "$*" in
    "show --value -p SubState "*)
      if [ "$CASE" = installer_failure ]; then echo failed; return; fi
      if [ "$CASE" = pending ]; then echo running; return; fi
      value="$(cat "$CASE_ROOT/state")"
      if [ "$value" = queued ]; then echo dead
      elif [ "$value" = running ]; then echo running; echo exited > "$CASE_ROOT/state"
      else echo exited; fi ;;
    "show --value -p Result "*)
      if [ "$CASE" = result_failure ]; then echo exit-code; else echo success; fi ;;
    show*) echo 'Result=success' ;;
    *) return 92 ;;
  esac
}
sleep() { if [ "$CASE" = pending ]; then SECONDS=1000; fi; }
journalctl() { echo SIMULATED_SERVICE_JOURNAL; }
'''
cases = [
    ("success", 0, True),
    ("result_failure", 1, True),
    ("nonroot", 1, False),
    ("repository", 1, False),
    ("fetch", 1, False),
    ("ancestry", 1, False),
    ("hash", 1, False),
    ("preflight", 1, False),
    ("exec_failure", 1, True),
    ("installer_failure", 1, True),
    ("postflight", 1, True),
    ("pending", 3, True),
]
for case, expected_code, launched in cases:
    with tempfile.TemporaryDirectory(prefix="altavien-launcher-") as temporary:
        work = Path(temporary)
        (work / "probe/.git").mkdir(parents=True)
        (work / "source-installer").write_bytes(installer)
        script = payload.replace("probe=/srv/altavien-pg-probe/source", f"probe='{work}/probe'")
        result = subprocess.run(
            ["bash"], input=mock + "\n" + script, text=True, capture_output=True,
            env={**os.environ, "CASE": case, "CASE_ROOT": str(work)}, timeout=10,
        )
        assert result.returncode == expected_code, (case, result.returncode, result.stdout, result.stderr)
        assert (work / "launch").exists() == launched, case
        assert ("ALTAVIEN_REMOTE_UPDATE_VERIFIED" in result.stdout) == (case == "success"), case
        if case == "pending":
            assert "UPDATE_STILL_RUNNING_UNIT=" in result.stdout
            assert "UPDATE_FAILED_SEE_JOURNAL" not in result.stdout
        print("PASS:", case)
print("All 12 simulated launcher scenarios passed. No server connection or deployment was made.")
