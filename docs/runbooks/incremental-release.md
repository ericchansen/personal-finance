# Stable incremental releases and routine commands

This packaging is separate from the source collector and its task.
The [installer](../../deploy/incremental/install-release.ps1) only archives a
clean committed checkout, verifies it, and selects an incremental release.
It never collects sources, installs dependencies, migrates a database,
registers/enables a task, activates a writer marker, or calls the app.
The [routine launcher](../../deploy/incremental/run-incremental.ps1) invokes the
existing incremental worker, not a second resolver or financial writer.

## Operator prerequisites

- Keep the legacy overlapping writer disabled. This package does not change it.
- Use a **dedicated external incremental runtime directory**, disjoint from the
  source checkout and the private data root. Do not reuse the collector release
  root. The installer refuses roots containing the collector's `current.json`,
  `releases/`, launcher, or any unrelated root entries.
- Provision the selected Python interpreter and existing repository
  dependencies separately. Python 3.11+ and actual IANA timezone data are
  required by the worker. Packaging does not install packages.
- Protect the runtime/pointer, reviewed configuration, credential files, and
  per-data-root ownership marker with operator-managed filesystem permissions.
  Hash bindings detect drift; they are not a signature or a defense against an
  administrator replacing both trusted launcher and trust records.
- Complete the worker's [account/evidence prerequisites](incremental-finance.md)
  separately. A passing archive import check is not source admission, app
  authentication, database migration, or financial reconciliation.

## Install from a reviewed clean commit

Use a PowerShell session with these paths set to operator-reviewed locations:

```powershell
.\deploy\incremental\install-release.ps1 `
    -RepositoryRoot $repository `
    -ReleaseRoot $incrementalRoot `
    -Python $pythonExecutable `
    -WhatIf

.\deploy\incremental\install-release.ps1 `
    -RepositoryRoot $repository `
    -ReleaseRoot $incrementalRoot `
    -Python $pythonExecutable `
    -Confirm:$false
```

The wrapper uses standard PowerShell
[ShouldProcess](https://learn.microsoft.com/powershell/scripting/learn/deep-dives/everything-about-shouldprocess).
No installation occurs under `-WhatIf`. An untracked or modified file refuses
installation; there is no dirty/force override.

The dedicated layout is:

```text
<incrementalRoot>/
  run-incremental.ps1
  incremental-current.json
  incremental-releases/<commit>/
    incremental-release-manifest.json
    <git archive contents, without .git>
```

The installer returns only safe release bindings: commit, code hash, and
manifest SHA-256. The operator must use this **installed release's** commit and
code hash when separately reviewing/activating the schema-2 ownership marker.
The installer does not invent whole-rebuild execution IDs or install a marker.

The [release helper](../../finance_store/incremental_release.py) records the
commit, tree, archive hash, and every archived file's SHA-256. Both the
PowerShell preflight and Python verifier reject missing, altered, or extra
files, including extra Python sources, migrations, and bytecode caches.
Symlinks/junctions/reparse points are rejected. The stable pointer binds the
exact manifest, release location, and launcher bytes. An existing release is
reverified rather than silently reused.

`current_incremental_release()` now supports this manifest-bound archive
without invoking Git or accidentally discovering an enclosing repository.
It rechecks archive contents on each call. Live development checkouts still
hash dirty/untracked runtime sources and use their actual Git commit. Runtime
hashing now also includes the dedicated packaging code and requirements files.
This changes the code hash: prior markers need separate operator review.

The stable launcher uses an isolated Python interpreter (`-I -B`), avoiding
inherited Python import-path overrides and new bytecode files inside the
archive. Do not use a normal bytecode-writing invocation inside an installed
archive. Imported dependency binaries and the interpreter are not vendored or
hashed by this manifest; their provisioning remains an operator prerequisite.

## Reviewed external launch configuration

Create the following **outside the repository**, inside the selected data root.
Values below are structural placeholders, not deployable account facts:

```json
{
  "schemaVersion": 1,
  "kind": "incremental-launch-configuration",
  "dataRoot": "<absolute external data root>",
  "scopes": [
    {
      "path": "incremental/scopes/cash.json",
      "scopeId": "<actual stable scope UUID>",
      "configurationHash": "<actual scope content hash>",
      "dsnFile": "secrets/incremental-dsn.txt",
      "passwordFile": "secrets/app-password.txt",
      "evidenceKeyFile": "secrets/repair-verification-key.txt"
    }
  ]
}
```

Scope IDs and scope configuration hashes come from the existing verified scope
loader. Do not derive them from account names or add an `approved` flag.
Every referenced path must remain within this data root; absolute external
paths, traversal, and reparse points are refused. Each scope may select its own
credentials. Scope IDs must be unique.

Credential files contain a single nonempty UTF-8 value, with optional trailing
newline:

- DSN: explicit `host`, `dbname`, `user`, and `password`. Libpq service/passfile
  indirection and inherited `PG*` authentication are not accepted.
- App password: the actual separately provisioned authenticated app credential.
- Evidence key: hexadecimal verification key, at least 32 bytes, matching the
  immutable completed-repair history.

The launcher clears inherited finance, Wealthfolio, SimpleFIN, libpq, and
Python environment overrides during each scope invocation. It reads the DSN
and password **only from the configured files**. Inherited HTTP proxy overrides
are removed and loopback app origins explicitly bypass proxies. For repair evidence it selects
the immutable contract's `repairHistory.verificationKeyEnv` (default
`WEALTHFOLIO_PROMOTION_EVIDENCE_KEY`) and overwrites that variable solely with
the configured key file. Custom names must be uppercase `*_KEY`, not Git,
libpq, Python, or the promotion operator key. An inherited key cannot substitute
for a missing configured file. Secrets never appear in command arguments,
console output, or launcher status documents. They exist briefly in the worker
process environment; OS administrators remain a trust boundary.

Record the **file SHA-256** of the reviewed launch configuration once:

```powershell
$reviewedConfigurationSha256 = (
    Get-FileHash -LiteralPath $launchConfiguration -Algorithm SHA256
).Hash.ToLowerInvariant()
```

Persist this reviewed value in the scheduled action arguments. Do **not**
recompute it automatically at each scheduled invocation: doing so would silently
approve configuration/scope expansion.

## Supported installed commands

Read database status (no app password, evidence key, marker, or mutation opt-in
is needed; scope evidence and DSN remain required):

```powershell
& "$incrementalRoot\run-incremental.ps1" `
    -ReleaseRoot $incrementalRoot `
    -Configuration $launchConfiguration `
    -ConfigurationSha256 $reviewedConfigurationSha256 `
    -Command status
```

Build the existing durable plan, without app mutations or writer activation:

```powershell
& "$incrementalRoot\run-incremental.ps1" `
    -ReleaseRoot $incrementalRoot `
    -Configuration $launchConfiguration `
    -ConfigurationSha256 $reviewedConfigurationSha256 `
    -Command plan
```

Run/recover the routine worker for **only those exact configured scopes**:

```powershell
& "$incrementalRoot\run-incremental.ps1" `
    -ReleaseRoot $incrementalRoot `
    -Configuration $launchConfiguration `
    -ConfigurationSha256 $reviewedConfigurationSha256 `
    -Command run -EnableMutations
```

`run` requires both the explicit switch and an independently activated schema-2
marker under this data root. The marker must bind the exact scope/config hash,
installed release, origin, instance, and environment. No inherited opt-in or
marker path can authorize another scope. The existing worker checks the
authenticated app instance again before writing. Wrong/missing markers hold
before any financial mutation.

For an operator-created Windows task, the stable action is the last command
above invoked with `powershell.exe -NoProfile -NonInteractive -File` (or the
provisioned `pwsh`), with the reviewed arguments stored literally. Use a distinct
incremental task name and `IgnoreNew` overlap behavior. **This package does not
register the task.** The actual cross-process mutation exclusion remains the
worker's existing PostgreSQL global writer lock and shared migration/backup
gate, not a launcher PID file or expiring lease. Overlap/gate failures are holds.

## Status and failure semantics

Each configured scope emits one safe JSON status line: scope ID, state, fixed
reason code, run/checkpoint hashes, and supported counts. Other app fields,
source values, credential strings, and exception messages are suppressed.
One bad scope does not stop subsequent independently configured scopes.

The wrapper persists sanitized per-invocation results under:

```text
<dataRoot>/automation/incremental-launcher/runs/<run UUID>.json
<dataRoot>/automation/incremental-launcher/current.json
```

Per-run records are exclusive-create; `current.json` is atomically replaced.
Archive/pointer/configuration failures before a trustworthy data root is
established produce a console hold only. The worker retains its separate exact
source/plan/binding/attempt receipts. Launcher status does not replace them.

Exit code `0` means invocation completed without a scope-level held/uncertain
result; `1` means at least one hold/failure/uncertainty. A launcher `completed`
status is **not** financial certification: a plan may be pending, and an applied
eligible batch may still report local holds or historical backfill counts.
Consume the per-scope state and the worker's evidence, not just the process exit.

The launcher rechecks the selected release and configuration between scopes.
Changing a pointer/configuration midway holds later scopes. It cannot revoke an
already issued HTTP request or atomically couple a pointer change to app writes.
Use operator-controlled quiescence, the migration gate where appropriate, and
separate marker review for release changes. A crashed install may leave a
staging/archive artifact; inspect and clean it explicitly rather than forcing a
new release over unexplained files. Previous immutable releases remain retained.

No existing activity update capability is added. Known lossy amount/date
corrections, unsupported interpretations, stale sources, missing bindings, and
whole-batch balance discrepancies keep their existing holds. No schedule,
collector, canonical policy, ledger, HTTP correction workaround, or UI changes
are part of this package.

## Verification

[Packaging tests](../../tests/test_incremental_release.py) execute the actual
PowerShell scripts against owned synthetic Git repositories and git archives.
The opt-in native cases reuse disposable PostgreSQL and authenticated synthetic
REST fixtures with the **real** resolver, scope verifier, bootstrap proof,
client, and worker. They cover archive drift, separate collector roots, `WhatIf`,
idempotent installation, inherited-secret rejection, exact marker/scope binding,
global/migration gates, create then fresh-process no-op, source-anchor evidence,
and independent scope continuation.

```powershell
$env:FINANCE_INCREMENTAL_POSTGRES_TEST = '1'
python -m pytest tests\test_incremental_release.py -q
python -m ruff check finance_store\incremental_release.py `
    finance_store\incremental_launcher.py deploy\incremental\entrypoint.py `
    tests\test_incremental_release.py importers\monarch\mutation_guard.py
```

The tests accept no deployed DSN and clean only their uniquely named fixtures
and containers. They do not establish that a private installation has been
deployed, scheduled, reconciled, or authorized.
