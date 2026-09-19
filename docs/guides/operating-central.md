# Operate Pursers Central

This guide starts after you have installed Pursers and run:

```sh
pursers-central init /PATH/TO/INSTANCE
```

`/PATH/TO/INSTANCE` is private. It contains the database configuration and
credentials for your Central service. Keep it readable only by the account that
runs Central.

## Run Central as a service

Use the executable from the same virtual environment in which you installed
Pursers. Do not put a token, signing key, or door value in a service file or
environment variable. `pursers-central run` reads its non-secret runtime
settings from `profile.env` and reads credentials from files.

### macOS LaunchAgent

This LaunchAgent configuration was loaded and exercised on macOS for this
release. Create the log and cache directories first:

```sh
install -d -m 700 /PATH/TO/LOGS /PATH/TO/CACHE
```

Save this as
`~/Library/LaunchAgents/io.github.pursers.central.plist`, replacing every
`/PATH/TO/...` value with an absolute path. XML does not expand `~` or shell
variables.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>io.github.pursers.central</string>

  <key>ProgramArguments</key>
  <array>
    <string>/PATH/TO/VENV/bin/pursers-central</string>
    <string>run</string>
    <string>/PATH/TO/INSTANCE</string>
    <string>--log-level</string>
    <string>info</string>
  </array>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/PATH/TO/LOGS/central.stdout.log</string>
  <key>StandardErrorPath</key>
  <string>/PATH/TO/LOGS/central.stderr.log</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>TMPDIR</key>
    <string>/PATH/TO/CACHE</string>
  </dict>
</dict>
</plist>
```

Check the file, load it, and inspect the running job:

```sh
plutil -lint ~/Library/LaunchAgents/io.github.pursers.central.plist
launchctl bootstrap gui/$(id -u) \
  ~/Library/LaunchAgents/io.github.pursers.central.plist
launchctl print gui/$(id -u)/io.github.pursers.central
curl --fail --silent http://127.0.0.1:8766/healthz
```

Restart a loaded service after an upgrade:

```sh
launchctl kickstart -k gui/$(id -u)/io.github.pursers.central
```

Unload it before a restore or uninstall, and wait for both the LaunchAgent and
its former process to disappear. Run this block in the same shell as the
restore or uninstall commands that follow:

```sh
central_service="gui/$(id -u)/io.github.pursers.central"
central_pid="$({ launchctl print "$central_service" 2>/dev/null || true; } |
  awk '/^[[:space:]]*pid = [0-9]+$/ { print $3; exit }')"
launchctl bootout "$central_service"

remaining=30
while launchctl print "$central_service" >/dev/null 2>&1 ||
  { [ -n "$central_pid" ] &&
    /bin/ps -p "$central_pid" -o pid= >/dev/null 2>&1; }
do
  remaining=$((remaining - 1))
  if [ "$remaining" -le 0 ]; then
    echo "Central did not stop within 30 seconds; do not change its files" >&2
    exit 1
  fi
  sleep 1
done
central_stopped=yes
echo "Central is stopped"
```

`KeepAlive` restarts an exited process while the job remains loaded. `bootout`
unloads the job, so it stays stopped. `bootout` can return before the process
has exited; the bounded loop therefore checks both sources before setting
`central_stopped`. If it times out, inspect the service and logs instead of
moving or deleting files. If you edit the plist, use the same stop-completion
check before running `bootstrap` again.

### Linux systemd user service

**Not verified on this release:** the release verification host was macOS and
did not provide `systemctl` or `loginctl`. The unit below follows the shipped
Central command-line interface, but was not started on Linux.

Create `~/.config/systemd/user/pursers-central.service`:

```ini
[Unit]
Description=Pursers Central
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart=/PATH/TO/VENV/bin/pursers-central run /PATH/TO/INSTANCE --log-level info
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
Environment=TMPDIR=/PATH/TO/CACHE
StandardOutput=journal
StandardError=journal
NoNewPrivileges=true

[Install]
WantedBy=default.target
```

Create `/PATH/TO/CACHE` with mode `0700`, then load and start the unit:

```sh
systemctl --user daemon-reload
systemctl --user enable --now pursers-central.service
systemctl --user status pursers-central.service
journalctl --user -u pursers-central.service -n 100 --no-pager
```

To keep the user service running after you log out, an administrator may need
to enable lingering once:

```sh
loginctl enable-linger USERNAME
```

Restart or stop the service with:

```sh
systemctl --user restart pursers-central.service
systemctl --user disable --now pursers-central.service
```

## Health and logs

Central listens on loopback by default. Check it locally:

```sh
curl --fail --silent http://127.0.0.1:8766/healthz
```

Important fields are:

| Field | Meaning |
| --- | --- |
| `status` | `ok` means Central produced the health response. `error` returns HTTP 500. |
| `store_backend` | The quickstart runtime should report `sqlite`. |
| `board_count` | Number of board documents in the store. |
| `journal_head` | Highest current journal sequence among the boards. It does not count tickets. |
| `last_error_class` | Class name of the last recorded runtime or tool error, or `null`. It remains useful after the request that failed. |
| `open_file_descriptors` | File descriptors currently open by Central, when the operating system exposes the count. |
| `soft_file_descriptor_limit` | Process soft limit, or `null` if unavailable. |
| `file_descriptor_pressure` | Open descriptors divided by the soft limit. Watch the trend; values approaching `1` leave little room for new files or connections. |

The response also includes uptime, active subscription streams, the per-
principal stream cap, and a `boards` object. Each board row reports its hot
document size, archived-ticket count, cumulative archived items and bytes
freed, and document loads and saves over the last 60 seconds.

The default log level is `info`. The command accepts `critical`, `error`,
`warning`, `info`, `debug`, or `trace`. Use `debug` or `trace` only while
investigating a problem because they produce more output. HTTP access logging
is disabled; startup messages, runtime errors, and machine-readable events go
to standard error. The LaunchAgent example writes those streams to the two log
files. The systemd example sends them to the user journal.

Watch for repeated restarts, traceback or JSON error records, a non-null
`last_error_class` that coincides with failed operations, increasing descriptor
pressure, and unexpected growth in the database or log files. The LaunchAgent
does not rotate its log files; use your normal host log-rotation policy.

## Back up a running instance

The durable store is `/PATH/TO/INSTANCE/data/board.sqlite3`. SQLite uses WAL
mode, so copying that file alone while Central is running can miss committed
data still in the WAL. Use SQLite's online backup operation instead:

```sh
umask 077
install -d -m 700 /PATH/TO/BACKUP/data
sqlite3 /PATH/TO/INSTANCE/data/board.sqlite3 \
  ".backup '/PATH/TO/BACKUP/data/board.sqlite3'"
sqlite3 /PATH/TO/BACKUP/data/board.sqlite3 'PRAGMA integrity_check;'
```

The last command must print `ok`.

Back up the runtime profile, issuer material, tokens, and request-state keyring
alongside the database:

```sh
cp -p /PATH/TO/INSTANCE/profile.env /PATH/TO/BACKUP/
cp -p /PATH/TO/INSTANCE/signing-key.pem /PATH/TO/BACKUP/
cp -p /PATH/TO/INSTANCE/jwks.json /PATH/TO/BACKUP/
cp -p /PATH/TO/INSTANCE/admin.jwt /PATH/TO/BACKUP/
cp -p /PATH/TO/INSTANCE/worker.jwt /PATH/TO/BACKUP/
cp -p /PATH/TO/INSTANCE/data/request-state.keys /PATH/TO/BACKUP/data/
```

Include any additional token files that you issued. Keep the backup directory
at mode `0700` and every credential or database file at mode `0600`. Do not
copy `central.live.lock`, `board.sqlite3-shm`, or `board.sqlite3-wal` from the
live instance into an online backup; the `.backup` database is the consistent
copy. If an issuer-key rotation is still in its overlap window, also preserve
the `signing-key.*.retired.pem` file described by the rotation manual.

An alternative is stop-copy-start: unload or stop the service, confirm the
process has exited, copy the entire instance directory with permissions
preserved, and start it again. A raw directory copy is safe only while Central
is stopped.

## Restore and prove the data is present

Restore to the same absolute instance path because `profile.env` contains
absolute paths. On macOS, first run the stop-completion block above in this
same shell. The following guard rechecks its result before the first filesystem
operation. Keep the old instance until the restore is verified:

```sh
test "${central_stopped:-}" = yes || {
  echo "Run the macOS stop-completion block before restoring" >&2
  exit 1
}
if launchctl print "$central_service" >/dev/null 2>&1 ||
  { [ -n "$central_pid" ] &&
    /bin/ps -p "$central_pid" -o pid= >/dev/null 2>&1; }
then
  echo "Central is still running; restore aborted" >&2
  exit 1
fi

mv /PATH/TO/INSTANCE /PATH/TO/INSTANCE.before-restore
install -d -m 700 /PATH/TO/INSTANCE /PATH/TO/INSTANCE/data
cp -p /PATH/TO/BACKUP/profile.env /PATH/TO/INSTANCE/
cp -p /PATH/TO/BACKUP/signing-key.pem /PATH/TO/INSTANCE/
cp -p /PATH/TO/BACKUP/jwks.json /PATH/TO/INSTANCE/
cp -p /PATH/TO/BACKUP/admin.jwt /PATH/TO/INSTANCE/
cp -p /PATH/TO/BACKUP/worker.jwt /PATH/TO/INSTANCE/
cp -p /PATH/TO/BACKUP/data/board.sqlite3 /PATH/TO/INSTANCE/data/
cp -p /PATH/TO/BACKUP/data/request-state.keys /PATH/TO/INSTANCE/data/
```

Start the service, wait for `status: ok`, then use your MCP client to call
`board_status` and `ticket_get` for a ticket that existed before the backup.
Do not delete `INSTANCE.before-restore` until those checks pass.

The release test took an online backup while Central was running. The bounded
stop check needed one wait iteration, its guard passed before the first `mv`,
and the LaunchAgent then bootstrapped without error. Health returned `ok` with
the `sqlite` backend, and the original ticket was read successfully from the
restored database.

## Upgrade and roll back

Take a backup first. Upgrade in the same virtual environment named in the
service configuration:

```sh
/PATH/TO/VENV/bin/python -m pip install --upgrade pursers
```

For a reproducible final release, pin the version explicitly:

```sh
/PATH/TO/VENV/bin/python -m pip install --upgrade 'pursers==5.0.0'
```

Restart the service, then check all three surfaces:

```sh
curl --fail --silent http://127.0.0.1:8766/healthz
/PATH/TO/VENV/bin/python -m pip show pursers pursers-central pursers-client
```

Also call `board_status` through an authenticated MCP client. Confirm the board
and ticket counts are plausible before allowing agents to resume writes.

**Not verified on this release:** PyPI listed only final version `5.0.0`, so a
pre-release install and a rollback to an older published version could not be
exercised. When such versions exist, use an exact pre-release pin such as
`pursers==X.Y.ZrcN`; do not use an unbounded `--pre` install for a service.
To roll back, stop Central, restore the pre-upgrade data backup if the newer
version changed it, reinstall the exact previously tested Pursers version, and
start Central again:

```sh
/PATH/TO/VENV/bin/python -m pip install --force-reinstall \
  'pursers==PREVIOUS_VERSION'
```

Repeat the health, version, `board_status`, and known-ticket checks after a
rollback.

Issuer-key rotation is a separate operation. Follow the
[issuer key rotation manual](../operations/issuer-key-rotation.md) for either
the normal overlapping-key procedure or an intentional hard cutover. An
upgrade does not rotate issuer keys.

## Retention and archive maintenance

The defaults are conservative:

- closed or canceled tickets stay in the hot board document for 2 days, then
  move to the per-ticket archive tier;
- the newest 50 dispatch, submission, and review history entries stay inline;
- retired members become tombstones after 3 inactive days;
- expired invites are pruned after 7 days;
- journal rows have a 7-day retention window and a 50,000-row cap, with a
  500-row minimum tail and live consumer cursors respected.

The automatic reaper runs the archive sweep. An administrator can request the
same work immediately with this MCP tool call:

```json
{"name":"board_archive_run","arguments":{"board_id":"YOUR_BOARD"}}
```

Archived tickets remain durable and readable through `ticket_get` and
`ticket_list`; archive maintenance is not deletion. `board_archive_run` also
performs the safe journal sweep and reports `durable_records_untouched: true`.

`board_reap` is different: it releases expired pre-submission work and review
leases while preserving submitted work. It does not replace archive
maintenance:

```json
{"name":"board_reap","arguments":{"board_id":"YOUR_BOARD"}}
```

Manual `journal_compact` removes only derivable journal telemetry and requires
an administrator. `retain_last` must be at least 500:

```json
{"name":"journal_compact","arguments":{"board_id":"YOUR_BOARD","retain_last":500}}
```

Leave the defaults in place unless you have measured growth and have a written
retention requirement. Before changing retention, take a backup and check that
active consumers have advanced their cursors.

## Disk and file-descriptor limits

Measure both the database directory and the filesystem that contains it:

```sh
du -sh /PATH/TO/INSTANCE/data
df -h /PATH/TO/INSTANCE/data
ulimit -n
```

The SQLite database, its WAL during writes, logical archive records, and the
service logs are the main growing data. `board_archive_run` reduces hot board
documents but retains archived records in SQLite. Plan disk capacity for the
durable archive and your backup retention, not only the hot document size.

Use the health fields rather than the interactive shell's `ulimit` to observe
the running Central process. If descriptor pressure climbs steadily, first
look for unusually many subscription streams or another process-level leak;
raising the limit alone does not fix the cause.

## Uninstall without losing the board

1. Stop and unload or disable the service.
2. Take a final backup and verify it with `PRAGMA integrity_check`.
3. Remove the service file.
4. Remove the virtual environment only after confirming that its path is not
   your instance or backup path.

On macOS, run the stop-completion block above in the same shell. Require its
verified stopped state before moving the environment aside:

```sh
test "${central_stopped:-}" = yes || {
  echo "Run the macOS stop-completion block before uninstalling" >&2
  exit 1
}
if launchctl print "$central_service" >/dev/null 2>&1 ||
  { [ -n "$central_pid" ] &&
    /bin/ps -p "$central_pid" -o pid= >/dev/null 2>&1; }
then
  echo "Central is still running; uninstall aborted" >&2
  exit 1
fi

VENV=/PATH/TO/VENV
INSTANCE=/PATH/TO/INSTANCE
BACKUP=/PATH/TO/BACKUP
QUARANTINE=/PATH/TO/QUARANTINE

[ -f "$VENV/pyvenv.cfg" ] || {
  echo "$VENV is not a Python virtual environment; uninstall aborted" >&2
  exit 1
}
venv_path=$(cd "$VENV" && pwd -P) || exit 1
instance_path=$(cd "$INSTANCE" && pwd -P) || exit 1
backup_path=$(cd "$BACKUP" && pwd -P) || exit 1
case "$venv_path" in
  "$instance_path"|"$backup_path")
    echo "The virtual environment must not be the instance or backup; uninstall aborted" >&2
    exit 1
    ;;
esac

install -d -m 700 "$QUARANTINE"
mv "$VENV" "$QUARANTINE/pursers-venv.old"
```

On Linux, remove the explicit virtual-environment directory only after the
same check:

```sh
VENV=/PATH/TO/VENV
INSTANCE=/PATH/TO/INSTANCE
BACKUP=/PATH/TO/BACKUP

[ -f "$VENV/pyvenv.cfg" ] || {
  echo "$VENV is not a Python virtual environment; uninstall aborted" >&2
  exit 1
}
venv_path=$(cd "$VENV" && pwd -P) || exit 1
instance_path=$(cd "$INSTANCE" && pwd -P) || exit 1
backup_path=$(cd "$BACKUP" && pwd -P) || exit 1
case "$venv_path" in
  "$instance_path"|"$backup_path")
    echo "The virtual environment must not be the instance or backup; uninstall aborted" >&2
    exit 1
    ;;
esac

rm -r -- "$VENV"
```

Removing the virtual environment does not remove the instance data.

Keep `/PATH/TO/INSTANCE` and at least one verified backup if you may reinstall
or need the audit history. Delete the instance and backups only when you intend
to destroy the board, tokens, issuer key, request-state keys, and archived
records together.
