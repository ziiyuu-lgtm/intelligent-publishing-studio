#requires -Version 5.1
<#
Resume the already approved isolated ALTAVIEN test-site update.
Run from a PowerShell window in this file's directory:
  .\scripts\resume_altavien_issue_release.ps1 -SshTarget root@your-test-host

GitHub PR #8: https://github.com/ziiyuu-lgtm/intelligent-publishing-studio/pull/8
Installer commit: 6639c7ffb18b3bcaed64270b0db2c83a50cdb92e
Application target: 35c25bd53b7a4cfa97d531e377151c0a9791749d

Uses the existing SSH host key and prompts for the server password normally.
No password or key is embedded or saved. The installer checks the existing
test site, backs up code/database, rehearses the migration and preserves data.
If SSH disconnects after UPDATE_UNIT is printed, the systemd job continues.
Read that unit's journal before retrying; the installer excludes concurrent
updates and verifies an already completed installation without restarting it.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^root@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$')]
    [string]$SshTarget
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$sshExecutable = (Get-Command ssh -CommandType Application -ErrorAction Stop | Select-Object -First 1).Path

$remoteScript = @'
set -euo pipefail
umask 077
probe=/srv/altavien-pg-probe/source
commit=6639c7ffb18b3bcaed64270b0db2c83a50cdb92e
if [ "$(id -u)" -ne 0 ]; then
  echo STOP_REQUIRES_ROOT
  exit 1
fi
test -d "$probe/.git"
case "$(runuser -u postgres -- git -C "$probe" remote get-url origin)" in
  https://github.com/ziiyuu-lgtm/intelligent-publishing-studio|https://github.com/ziiyuu-lgtm/intelligent-publishing-studio.git|git@github.com:ziiyuu-lgtm/intelligent-publishing-studio.git) ;;
  *) echo STOP_UNEXPECTED_REPOSITORY; exit 1 ;;
esac
runuser -u postgres -- git -C "$probe" fetch origin feature/altavien-release-foundation-20261104
runuser -u postgres -- git -C "$probe" merge-base --is-ancestor "$commit" FETCH_HEAD
installer="$(mktemp /root/altavien-issue-release.XXXXXXXX.py)"
runuser -u postgres -- git -C "$probe" show "$commit:scripts/install_altavien_issue_release.py" > "$installer"
printf '%s  %s\n' fd184f1815727b0c2bcbfd00ce8446a8ee0c2cd1e6f38ee202085cc3937f9f79 "$installer" | sha256sum -c -
python3 "$installer" --check
unit="altavien-issue-release-$(date -u +%Y%m%d%H%M%S)-$$.service"
printf 'UPDATE_UNIT=%s\n' "$unit"
printf 'If this connection closes, reconnect and run: journalctl -u %s -n 100 --no-pager\n' "$unit"
# Type=exec waits for the installer process to start before status polling.
# The service remains independent of the SSH connection after it starts.
systemd-run --unit="$unit" --property=Type=exec --property=RemainAfterExit=yes /usr/bin/python3 -u "$installer"
deadline=$((SECONDS + 300))
state=unknown
while [ "$SECONDS" -lt "$deadline" ]; do
  state="$(systemctl show --value -p SubState "$unit")"
  case "$state" in exited|failed|dead) break ;; esac
  sleep 2
done
systemctl show "$unit" -p Result -p ActiveState -p SubState
journalctl -u "$unit" -n 100 --no-pager
if [ "$state" != exited ]; then
  case "$state" in
    failed|dead) echo UPDATE_FAILED_SEE_JOURNAL; exit 1 ;;
    *) printf 'UPDATE_STILL_RUNNING_UNIT=%s\n' "$unit"; exit 3 ;;
  esac
fi
if [ "$(systemctl show --value -p Result "$unit")" != success ]; then
  echo UPDATE_FAILED_SEE_JOURNAL
  exit 1
fi
python3 "$installer" --check
echo ALTAVIEN_REMOTE_UPDATE_VERIFIED
'@

# Base64 keeps PowerShell line endings and dollar signs out of the remote shell.
$normalizedScript = $remoteScript.Replace("`r", '')
$payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($normalizedScript))
$remoteCommand = "printf '%s' '$payload' | base64 --decode | /bin/bash"
$sshOptions = @(
    '-T',
    '-o', 'ConnectTimeout=15',
    '-o', 'ConnectionAttempts=1',
    '-o', 'ServerAliveInterval=15',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'StrictHostKeyChecking=yes',
    '-o', 'HostKeyAlgorithms=ssh-ed25519'
)
Write-Host 'Connecting to the existing ALTAVIEN test server...'
& $sshExecutable @sshOptions $SshTarget $remoteCommand
$sshExitCode = $LASTEXITCODE
if ($sshExitCode -eq 0) {
    Write-Host 'Verified: the isolated test-site update completed.'
} elseif ($sshExitCode -eq 3) {
    Write-Host 'The update is still running. Keep UPDATE_UNIT and inspect that service journal.'
} else {
    Write-Host 'Update completion is not confirmed. Keep the output above, including UPDATE_UNIT.'
    Write-Host 'If SSH disconnected, the server job may still be running; inspect its journal before retrying.'
}
exit $sshExitCode
