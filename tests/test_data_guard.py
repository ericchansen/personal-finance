import subprocess
import sys
from pathlib import Path

import pytest


GUARD = Path(__file__).resolve().parents[1] / ".githooks" / "scan_staged.py"


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.mark.parametrize(("filename", "staged"), [
    ("wallet-diagnostic.txt", False),
    ("\u03bb wallet-diagnostic.txt", False),
    ("\u03bb wallet-diagnostic.txt", True),
])
def test_guard_detects_diagnostics_in_untracked_and_unicode_paths(repository, filename, staged):
    path = repository / ".github" / filename
    path.parent.mkdir()
    fake_key = "x" + "pub" + "A" * 64
    path.write_text('{"providerKey": "' + fake_key + '"}\n', encoding="utf-8")
    if staged:
        subprocess.run(["git", "add", "--", str(path)], cwd=repository, check=True)
    else:
        unchanged = subprocess.run(
            [sys.executable, str(GUARD)], cwd=repository, capture_output=True,
        )
        assert unchanged.returncode == 0
    result = subprocess.run(
        [sys.executable, str(GUARD), *([] if staged else ["--all"])],
        cwd=repository, capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 1
    assert "Extended public key" in result.stderr
    assert fake_key not in result.stderr


def test_all_scans_tracked_and_untracked_files_but_respects_ignored_paths(repository):
    (repository / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repository / "public.md").write_text("Public documentation.\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "--", ".gitignore", "public.md"], cwd=repository, check=True,
    )
    (repository / "notes.md").write_text("Public notes.\n", encoding="utf-8")
    ignored = repository / "ignored"
    ignored.mkdir()
    (ignored / "diagnostic.txt").write_text("x" + "pub" + "A" * 64, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(GUARD), "--all"], cwd=repository,
        capture_output=True, encoding="utf-8",
    )
    assert result.returncode == 0
    assert "3 file(s) clean" in result.stdout
