#!/usr/bin/env python3
"""Block financial data and secrets from entering this public repository.

Runs as a pre-commit hook over staged files. Use --all to scan the whole tree.

Two layers of defence:
  1. Extension denylist  - data-shaped files never belong here at all.
  2. Content scan        - secrets and account-number patterns in text files.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Files that must never be committed regardless of content.
DENIED_SUFFIXES = {
    ".csv", ".tsv", ".ofx", ".qfx", ".qbo", ".qif",
    ".xls", ".xlsx", ".xlsm", ".pdf",
    ".har", ".db", ".sqlite", ".sqlite3", ".dump",
    ".pem", ".key", ".p12",
}

# Paths exempt from the extension denylist (synthetic fixtures only).
ALLOWED_PATTERNS = (
    re.compile(r"(^|/)tests/fixtures/"),
    re.compile(r"\.example\.(csv|json|ofx)$"),
)

# Extensions worth scanning the contents of.
TEXT_SUFFIXES = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".yml", ".yaml",
    ".md", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".txt", ".env", ".sql",
    ".example", "",
}

SECRET_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Stripe key", re.compile(r"\b[sr]k_(live|test)_[0-9A-Za-z]{16,}\b")),
    ("Plaid secret", re.compile(r"\bplaid[_-]?(secret|client[_-]?id)\s*[=:]\s*['\"][^'\"]{8,}", re.I)),
    ("Private key block", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("Bearer token", re.compile(r"\b[Bb]earer\s+[A-Za-z0-9._\-]{24,}")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("Hardcoded password", re.compile(r"\b(password|passwd|secret|api[_-]?key)\s*[=:]\s*['\"][^'\"{}$][^'\"]{7,}['\"]", re.I)),
    ("US SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("Card-like number", re.compile(r"\b(?:\d[ -]?){15,18}\d\b")),
    ("Account number label", re.compile(r"\b(account|acct|routing)\s*(number|no\.?|#)\s*[:=]\s*\d{6,}", re.I)),
    ("Personal email", re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)),
    ("Windows user path", re.compile(r"\b[A-Z]:\\Users\\[^\\\s]+\\", re.I)),
    ("Extended public key", re.compile(r"\b[xyzuvt]pub[1-9A-HJ-NP-Za-km-z]{40,}\b")),
    ("Bitcoin address", re.compile(r"\bbc1[a-z0-9]{20,}\b", re.I)),
]

# Placeholder values that look secret-ish but are safe.
PLACEHOLDER = re.compile(
    r"(example|placeholder|dummy|sample|fake|test|synthetic|username|xxxx|0000|1234567|changeme|your[_-])",
    re.I,
)


def staged_files() -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [Path(line) for line in out.splitlines() if line.strip()]


def tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [Path(line) for line in out.splitlines() if line.strip()]


def is_allowed(path: Path) -> bool:
    posix = path.as_posix()
    return any(p.search(posix) for p in ALLOWED_PATTERNS)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, OSError):
        return None


def scan(paths: list[Path]) -> list[str]:
    problems: list[str] = []

    for path in paths:
        if not path.exists() or path.is_dir():
            continue
        if is_allowed(path):
            continue

        if path.suffix.lower() in DENIED_SUFFIXES:
            problems.append(
                f"{path}: '{path.suffix}' is a data/secret file type. "
                f"Move it to the external data directory."
            )
            continue

        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue

        content = read_text(path)
        if content is None:
            continue

        for lineno, line in enumerate(content.splitlines(), 1):
            if len(line) > 2000:
                continue
            for label, rule in SECRET_RULES:
                if rule.search(line) and not PLACEHOLDER.search(line):
                    problems.append(f"{path}:{lineno}: possible {label}")
                    break

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="scan all tracked files")
    args = parser.parse_args()

    paths = tracked_files() if args.all else staged_files()
    if not paths:
        return 0

    problems = scan(paths)
    if not problems:
        print(f"data-guard: {len(paths)} file(s) clean")
        return 0

    print("\ndata-guard BLOCKED this commit:\n", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print(
        "\nThis repository is public and must contain no financial data or PII.\n"
        "If a match is a false positive, rename the placeholder or add the path to\n"
        "ALLOWED_PATTERNS in .githooks/scan_staged.py.\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
