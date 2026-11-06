#requires -Version 5.1
# Exercise argument transport in the actual Windows PowerShell/PowerShell engine.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$StubDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$engine = (Get-Process -Id $PID).Path
$sourcePath = Join-Path $PSScriptRoot '..\resume_altavien_issue_release.ps1'
$source = Get-Content -LiteralPath $sourcePath -Raw
$tokens = $null
$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseInput(
    $source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -ne 0) { throw ($parseErrors | Out-String) }
$match = [regex]::Match($source, '(?s)\$remoteScript = @''\r?\n(.*?)\r?\n''@')
if (-not $match.Success) { throw 'Remote payload not found' }
$expectedPayload = $match.Groups[1].Value.Replace("`r", '')
$work = Join-Path ([IO.Path]::GetTempPath()) ('altavien launcher ' + [guid]::NewGuid().ToString('N'))
$null = New-Item -ItemType Directory -Path $work
$scriptPath = Join-Path $work 'resume test.ps1'
Copy-Item -LiteralPath $sourcePath -Destination $scriptPath
$oldPath = $env:PATH
try {
    $env:PATH = $StubDirectory + [IO.Path]::PathSeparator + $env:PATH
    $env:ALTAVIEN_FAKE_SSH = '1'
    $env:ALTAVIEN_FAKE_SSH_TRACE = Join-Path $work 'ssh-arguments.txt'
    $ssh = Get-Command ssh -CommandType Application
    if ($ssh.Source -ne (Join-Path $StubDirectory 'ssh.exe')) {
        throw 'Refusing to test with a real SSH executable'
    }
    foreach ($code in @(0, 1, 3, 255)) {
        $env:ALTAVIEN_FAKE_SSH_EXIT = [string]$code
        $output = (& $engine -NoProfile -NonInteractive -File $scriptPath `
            -SshTarget 'root@fictional.invalid' 2>&1 | Out-String)
        if ($LASTEXITCODE -ne $code) { throw "Exit code mismatch: $code / $LASTEXITCODE" }
        $captured = @(Get-Content -LiteralPath $env:ALTAVIEN_FAKE_SSH_TRACE | ForEach-Object {
            [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_))
        })
        if ($captured[-2] -cne 'root@fictional.invalid') { throw 'SSH destination changed' }
        $expectedOptions = @(
            '-T', '-o', 'ConnectTimeout=15', '-o', 'ConnectionAttempts=1',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlgorithms=ssh-ed25519'
        )
        if (($captured[0..($captured.Count-3)] -join '|') -cne ($expectedOptions -join '|')) {
            throw 'SSH validation options changed'
        }
        $transport = [regex]::Match($captured[-1],
            "^printf '%s' '([A-Za-z0-9+/=]+)' \| base64 --decode \| /bin/bash$")
        if (-not $transport.Success) { throw 'Remote command argument was split or changed' }
        $decoded = [Text.Encoding]::UTF8.GetString(
            [Convert]::FromBase64String($transport.Groups[1].Value))
        if ($decoded -cne $expectedPayload) { throw 'Remote script bytes changed in transport' }
        $claimedSuccess = $output.Contains('Verified: the isolated test-site update completed.')
        if ($claimedSuccess -ne ($code -eq 0)) { throw 'Incorrect completion message' }
        if ($code -eq 3 -and -not $output.Contains('The update is still running.')) {
            throw 'Pending update was not distinguished from failure'
        }
        Write-Host "PASS: SSH transport and exit code $code"
    }
    # Invalid destinations must be rejected by parameter validation before SSH runs.
    foreach ($destination in @('-oProxyCommand=bad', 'root@host;bad', 'root@host -p 2222')) {
        Remove-Item -LiteralPath $env:ALTAVIEN_FAKE_SSH_TRACE
        $savedPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $null = & $engine -NoProfile -NonInteractive -File $scriptPath `
            -SshTarget $destination 2>&1
        $invalidExit = $LASTEXITCODE
        $ErrorActionPreference = $savedPreference
        if ($invalidExit -eq 0 -or (Test-Path -LiteralPath $env:ALTAVIEN_FAKE_SSH_TRACE)) {
            throw 'Invalid destination reached SSH'
        }
        # Recreate only the disposable trace so the next invalid-input check can remove it.
        [IO.File]::WriteAllText($env:ALTAVIEN_FAKE_SSH_TRACE, '')
        Write-Host 'PASS: invalid destination rejected before SSH'
    }
    Write-Host ("PASS: seven scenarios on " + $PSVersionTable.PSEdition + ' ' + $PSVersionTable.PSVersion)
} finally {
    $env:PATH = $oldPath
    Remove-Item Env:ALTAVIEN_FAKE_SSH, Env:ALTAVIEN_FAKE_SSH_TRACE, Env:ALTAVIEN_FAKE_SSH_EXIT -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $work -Recurse -Force
}
exit 0
