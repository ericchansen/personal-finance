"""Isolated interpreter bootstrap; the PowerShell launcher verifies files first."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    from finance_store.incremental_release import verify_archive
    if len(sys.argv) > 1 and sys.argv[1] == "install":
        from finance_store.incremental_release import main as install
        return install(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "verify":
        verify_archive(ROOT)
        from finance_store import incremental_cli  # noqa: F401
        import psycopg  # noqa: F401
        return 0
    from finance_store.incremental_launcher import main as launch
    return launch(sys.argv[1:])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print('{"state":"held","reason":"incremental-entrypoint-unavailable"}')
        raise SystemExit(1)
