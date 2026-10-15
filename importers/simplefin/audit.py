"""Read-only source reconciliation; Wealthfolio owns the reports and health checks."""

from __future__ import annotations

import sys

from importers.simplefin.local_sync import main


if __name__ == "__main__":
    raise SystemExit(main([*sys.argv[1:], "--check"]))
