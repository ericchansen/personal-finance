# Supported local release

Scheduled finance jobs run from a content-addressed release outside disposable
Git worktrees. A release is installed only from a clean commit:

```powershell
deploy\release\install-release.ps1 `
  -ReleaseRoot D:\documents\finance-runtime\personal-finance
```

The installer creates:

```text
<release-root>\
  current.json
  run-source-collector.ps1
  releases\<commit>\
    release-manifest.json
    ...
```

`current.json` binds the commit, Git tree, release directory, manifest hash, and
Python executable. The stable launcher verifies that binding before every run,
changes into the immutable release, removes every Wealthfolio mutation opt-in,
and invokes only `importers.simplefin.cli pull-snapshot`.

Install or update the source-only task only after the release exists:

```powershell
<release>\importers\simplefin\install-task.ps1 `
  -ReleaseRoot D:\documents\finance-runtime\personal-finance `
  -DataDir D:\documents\finance-data `
  -Days 90 `
  -At '06:00'
```

The task action points to `<release-root>\run-source-collector.ps1`, never to a
worktree. Installing a later clean commit atomically advances `current.json`;
the next task run uses that release.

The collection's latest receipt and per-scope outcomes are stored privately under
`automation/source-collection/`. A successful collection is not a statement
that the app ledger has been reconciled.

App delivery has a separate [incremental release](incremental-release.md) and
explicit scope ownership. Use that launcher's `status` command and the
[read-only event and operation views](incremental-agent-queries.md) to distinguish
source freshness, proposed work, and actual applied effects. Do not infer
projection lag by subtracting a count of one activity-key prefix from a
canonical row count: adopted legacy rows and unresolved history make that
comparison misleading.
