# Use one host profile for CLI and MCP reviews

Several MCP servers do not create extra foreground slots. All worktrees of one
clone share `review-fg`; the default legacy lock still serializes them even if
`SKODUN_REVIEW_FG_CAPACITY` is increased. Shell exports also do not configure an
already running MCP process or a GUI client that did not inherit that shell.

The optional [host launcher](../examples/bin/skodun-host) reads one trusted,
operator-owned shell file before starting the installed executable. It does not
install Skodun, restart clients, change product defaults, load credentials, or
select providers. It clears inherited executable and foreground/legacy/provider
capacity settings before sourcing the profile, so omitted capacity settings use
product defaults. Use it for both CLI and MCP startup.

## Prepare and activate

1. Run `skodun doctor`, compare its commit with the intended installed build,
   and inspect `skodun queue --scope host --json` plus `skodun providers`.
   Compare each MCP handshake's `serverInfo.commit` and `schemaVersion` with
   the CLI. The package version alone is insufficient. Process start time after
   a verified immutable wheel installation is useful supporting evidence;
   it does not replace a handshake when executable identity is uncertain.
2. Inventory actual legacy review processes, wrappers and lock owners. Only
   turn off the legacy lock after confirming the participant set uses Skodun's
   shared store admission. Historical scripts in an old worktree are potential
   participants; check what is actually running and the active client policy.
3. Save the existing entry points/configuration. Copy the launcher to a stable
   absolute host path. Create `~/.config/skodun/host-profile.sh` (or the same
   path under `XDG_CONFIG_HOME`) with owner-only write access. Set
   `SKODUN_REAL_BIN` to the underlying executable, never the wrapper or its
   alias. For example, on an audited pipx installation:

   ```sh
   SKODUN_REAL_BIN="$HOME/.local/pipx/venvs/skodun/bin/skodun"
   export SKODUN_REVIEW_FG_CAPACITY=2
   export SKODUN_LEGACY_FG_LOCK=0
   # This example assumes an existing, explicitly approved provider limit of 2:
   export SKODUN_PROVIDER_MAX_IN_FLIGHT=2
   ```

   This is an explicit host-wide opt-in affecting every repository reached
   through the launcher. Do not use it for a mixed legacy participant set.
   Two foreground slots are a starting profile, not a promise of twice the
   throughput. Keep the provider limit at the host's approved value; do not
   multiply it by the number of clients. This does not enable within-review
   parallel batches, which have their own explicit request option.
4. Point the CLI entry point and each client's MCP `command` to the same
   absolute launcher; preserve `args = ["mcp"]` and existing credential/provider
   configuration. A credential wrapper may invoke this launcher as its final
   exec target. Do not make the two wrappers call each other. Pin a shared
   absolute `SKODUN_HOST_PROFILE` if clients use different config directories.
5. Start a fresh CLI/MCP connection and verify `doctor`, handshake and real
   queue receipts. Retire idle old connections through their host's supported
   reconnect flow. Finish or explicitly cancel a busy review before retiring
   its connection. Killing an MCP child does not prove the host reconnected it;
   a disconnected transport is not a successful restart. A fresh CLI is the
   fallback while a host refresh is pending.

Profile changes apply to newly launched processes. Existing MCP processes and
queued/running reviews retain their original environment. All participating
clients should use the same limits; mixed configurations can yield different
admission behavior. Do not restart an agent's entire working session merely to
refresh the review tool.

## Verify and roll back

Use `skodun queue <request-id> --json` to inspect `capacity_layers`: foreground
configured/effective capacity should both be 2 and `legacy_dual_hold` false.
Provider layers retain their actual approved limits. Check trustworthy coverage
and current identity with the ordinary gate; queue admission is not review
success. Record actual waits/completion/failure rates before raising limits
again. Inspect the [bounded pilot](foreground-concurrency-pilot.md) for workload
and measurement limitations.

To restore serialization, put capacity 1 and legacy lock 1 in the profile and
refresh participants after draining or explicitly cancelling old requests.
Restoring configuration does not retroactively change active admissions.
Restore saved launchers if desired. Check the entry point after every package
upgrade: a package manager can replace a managed symlink. Verify the immutable
build and MCP handshakes again; this launcher does not auto-update loaded code.

The profile is trusted executable shell configuration, like a shell rc file.
The profile path must be absolute. Never point `SKODUN_HOST_PROFILE` at an untrusted repository file. Do not store
secrets in it or print a process's full environment during an audit.
