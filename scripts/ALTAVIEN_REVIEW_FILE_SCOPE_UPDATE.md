# Updating reviewer file access on the isolated test site

This update is limited to the fictional ALTAVIEN test service. It installs the
reviewer file-scope fix from application commit
`42749acdeefa706a8ada8d0710db97706915b722` on the exact previous
issue-review recovery baseline. Unexpected file hashes, a different service,
an unsafe database state or changed installer bytes stop the update.

From a Windows PowerShell session, run the verified repository launcher:

```powershell
.\scripts\resume_altavien_review_file_scope.ps1 -SshTarget root@your-test-host
```

The launcher uses an already trusted SSH host key and does not store a
password. It fetches the development branch, verifies the application target
is in its history, and checks the installer SHA-256. The installer verifies
both previously reviewed installer layers before reusing their preflight,
staging, private backup, shared upgrade lock, data checks and rollback.

The process rehearses on a database copy, briefly stops the test service,
applies the reviewed files and restarts it. Existing database records, saved
article order and media bytes are checked. It never releases an issue or sends
email. Rerunning after a completed update verifies the existing version
without another restart; rollback never restores older database or media
snapshots over new writes.

Keep `UPDATE_UNIT` from the output. If the connection ends, inspect that unit's
journal before retrying. `ALTAVIEN_REMOTE_UPDATE_VERIFIED` indicates a
successful final check. `UPDATE_STILL_RUNNING_UNIT` indicates the detached job
is still running. `ALTAVIEN_RECOVERY_NEEDS_ATTENTION` requires investigation.
CI rehearsals use disposable fictional records and do not access the VPS.
