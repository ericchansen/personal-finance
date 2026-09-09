"""Manifest-verified incremental archives, independent of collector releases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile

MANIFEST = "incremental-release-manifest.json"
POINTER = "incremental-current.json"
RELEASES = "incremental-releases"
LAUNCHER = "run-incremental.ps1"
MANIFEST_KIND = "personal-finance-incremental-release"
POINTER_KIND = "personal-finance-incremental-pointer"
RUNTIME_PATTERNS = (
    "finance_store/**/*.py", "importers/**/*.py", "deploy/postgres/migrations/*.sql",
    "deploy/incremental/*.py", "deploy/incremental/*.ps1", "requirements*.txt",
)


class ReleaseError(ValueError):
    """Only fixed, non-sensitive reason codes may cross the launcher boundary."""


def require(condition, reason):
    if not condition:
        raise ReleaseError(reason)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_hash(value, sizes=(64,)):
    return isinstance(value, str) and len(value) in sizes and all(c in "0123456789abcdef" for c in value)


def regular_path(path: Path):
    """Reject junctions/reparse points as well as symlinks, including ancestors."""
    path = path.absolute()
    for item in (path, *path.parents):
        if not item.exists() and not item.is_symlink():
            continue
        info = item.lstat()
        require(not stat.S_ISLNK(info.st_mode)
                and not getattr(info, "st_file_attributes", 0) & 0x400,
                "incremental-reparse-path-refused")
    return path


def relative_name(name):
    require(isinstance(name, str) and name and "\\" not in name and ":" not in name,
            "incremental-manifest-path-invalid")
    path = PurePosixPath(name)
    require(path.parts and not path.is_absolute() and name == path.as_posix()
            and not any(ord(character) < 32 for character in name)
            and not PureWindowsPath(name).is_reserved()
            and all(part not in {"", ".", "..", ".git"} and not part.endswith((".", " "))
                    for part in path.parts), "incremental-manifest-path-invalid")
    return path


def read_document(path):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "incremental-document-duplicate-key")
            result[key] = value
        return result
    value = json.loads(regular_path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs)
    require(isinstance(value, dict), "incremental-document-invalid")
    return value


def runtime_files(root):
    paths = sorted({path for pattern in RUNTIME_PATTERNS for path in root.glob(pattern)})
    require(paths and (root / "finance_store" / "incremental.py") in paths
            and (root / "importers" / "monarch" / "mutation_guard.py") in paths,
            "incremental-runtime-incomplete")
    return {path.relative_to(root).as_posix(): digest(regular_path(path)) for path in paths}


def archive_files(root):
    result = {}
    seen = set()
    for path in sorted(root.rglob("*")):
        regular_path(path)
        if not path.is_file() or path == root / MANIFEST:
            continue
        name = path.relative_to(root).as_posix()
        relative_name(name)
        require(name.casefold() not in seen,
                "incremental-archive-case-collision")
        seen.add(name.casefold())
        result[name] = digest(path)
    return result


def verify_archive(root, *, expected_manifest_hash=None):
    root = regular_path(root).resolve()
    require(not (root / ".git").exists(), "incremental-archive-must-not-contain-git")
    path = root / MANIFEST
    if expected_manifest_hash is not None:
        require(is_hash(expected_manifest_hash) and digest(path) == expected_manifest_hash,
                "incremental-release-manifest-drift")
    document = read_document(path)
    require(set(document) == {"schemaVersion", "kind", "commit", "tree", "archiveSha256", "files", "codeHash"}
            and type(document["schemaVersion"]) is int and document["schemaVersion"] == 1
            and document["kind"] == MANIFEST_KIND
            and is_hash(document["commit"], (40, 64)) and is_hash(document["tree"], (40, 64))
            and is_hash(document["archiveSha256"]) and is_hash(document["codeHash"])
            and isinstance(document["files"], dict) and document["files"],
            "incremental-release-manifest-invalid")
    for name, value in document["files"].items():
        relative_name(name)
        require(name != MANIFEST and is_hash(value), "incremental-release-file-binding-invalid")
    require(archive_files(root) == document["files"], "incremental-release-files-drift")
    require(fingerprint(runtime_files(root)) == document["codeHash"], "incremental-runtime-hash-drift")
    return document


def current_release(root):
    root = regular_path(root).resolve()
    if (root / MANIFEST).exists() or not (root / ".git").exists():
        document = verify_archive(root)
        return {key: document[key] for key in ("commit", "codeHash")}
    commit = _git(root, "rev-parse", "HEAD")
    require(Path(_git(root, "rev-parse", "--show-toplevel")).resolve() == root
            and is_hash(commit, (40, 64)), "incremental-git-root-invalid")
    return {"commit": commit, "codeHash": fingerprint(runtime_files(root))}


def verify_pointer(release_root):
    release_root = regular_path(release_root).resolve()
    document = read_document(release_root / POINTER)
    require(set(document) == {"schemaVersion", "kind", "commit", "tree", "releasePath",
                             "manifestSha256", "pythonExecutable", "launcherSha256"}
            and type(document["schemaVersion"]) is int and document["schemaVersion"] == 1
            and document["kind"] == POINTER_KIND and is_hash(document["commit"], (40, 64))
            and is_hash(document["tree"], (40, 64)) and is_hash(document["manifestSha256"])
            and is_hash(document["launcherSha256"]), "incremental-release-pointer-invalid")
    expected = release_root / RELEASES / document["commit"]
    require(Path(document["releasePath"]).is_absolute()
            and regular_path(Path(document["releasePath"])).resolve() == expected,
            "incremental-release-pointer-path-invalid")
    manifest = verify_archive(expected, expected_manifest_hash=document["manifestSha256"])
    require(all(document[key] == manifest[key] for key in ("commit", "tree")),
            "incremental-release-pointer-disagrees")
    require(digest(release_root / LAUNCHER) == document["launcherSha256"]
            == manifest["files"][f"deploy/incremental/{LAUNCHER}"],
            "incremental-launcher-drift")
    require(Path(document["pythonExecutable"]).is_absolute()
            and regular_path(Path(document["pythonExecutable"])).is_file(),
            "incremental-python-unavailable")
    return expected, document


def _git(root, *arguments):
    completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True)
    return completed.stdout.decode("utf-8").strip()


def _write_atomic(path, value):
    staging = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    try:
        staging.write_bytes(value)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


def install(repository, release_root, python):
    repository = regular_path(repository).resolve()
    release_root = regular_path(release_root).resolve()
    python = regular_path(python).resolve()
    require(not release_root.is_relative_to(repository) and not repository.is_relative_to(release_root),
            "incremental-release-root-must-be-separate")
    require(python.is_file(), "incremental-python-unavailable")
    require(not _git(repository, "status", "--porcelain", "--untracked-files=all"),
            "incremental-release-requires-clean-commit")
    commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    require(is_hash(commit, (40, 64)) and is_hash(tree, (40, 64)), "incremental-git-revision-invalid")
    if release_root.exists():
        allowed = {POINTER, RELEASES, LAUNCHER}
        require(all(path.name in allowed for path in release_root.iterdir()),
                "incremental-release-root-not-dedicated")
    release_root.mkdir(parents=True, exist_ok=True)
    releases = release_root / RELEASES
    releases.mkdir(exist_ok=True)
    target = releases / commit
    staging = releases / f".staging-{uuid.uuid4().hex}"
    archive = release_root / f".incremental-archive-{uuid.uuid4().hex}.zip"
    try:
        if not target.exists():
            subprocess.run(["git", "-C", str(repository), "archive", "--format=zip",
                            f"--output={archive}", commit], capture_output=True, check=True)
            staging.mkdir()
            with zipfile.ZipFile(archive) as zipped:
                names = set()
                for entry in zipped.infolist():
                    name = entry.filename.rstrip("/")
                    relative_name(name)
                    require(name.casefold() not in names and not stat.S_ISLNK(entry.external_attr >> 16)
                            and name != MANIFEST, "incremental-archive-entry-invalid")
                    names.add(name.casefold())
                    path = staging.joinpath(*PurePosixPath(name).parts)
                    if entry.is_dir():
                        path.mkdir(parents=True, exist_ok=True)
                    else:
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(zipped.read(entry))
            manifest = {
                "schemaVersion": 1, "kind": MANIFEST_KIND, "commit": commit, "tree": tree,
                "archiveSha256": digest(archive), "files": archive_files(staging),
                "codeHash": fingerprint(runtime_files(staging)),
            }
            (staging / MANIFEST).write_bytes(encoded(manifest))
            verify_archive(staging)
            # No financial calls, secrets, package installation, marker or task activation.
            smoke = subprocess.run(
                [str(python), "-I", "-B", str(staging / "deploy" / "incremental" / "entrypoint.py"), "verify"],
                cwd=staging, capture_output=True,
            )
            require(smoke.returncode == 0, "incremental-release-import-check-failed")
            staging.rename(target)
        manifest = verify_archive(target)
        require(manifest["commit"] == commit and manifest["tree"] == tree,
                "incremental-existing-release-disagrees")
        launcher = target / "deploy" / "incremental" / LAUNCHER
        _write_atomic(release_root / LAUNCHER, launcher.read_bytes())
        pointer = {
            "schemaVersion": 1, "kind": POINTER_KIND, "commit": commit, "tree": tree,
            "releasePath": str(target), "manifestSha256": digest(target / MANIFEST),
            "pythonExecutable": str(python), "launcherSha256": digest(launcher),
        }
        _write_atomic(release_root / POINTER, encoded(pointer))
        verify_pointer(release_root)
        return {"state": "installed", "commit": commit, "codeHash": manifest["codeHash"],
                "manifestSha256": pointer["manifestSha256"]}
    finally:
        archive.unlink(missing_ok=True)
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(install(args.repository, args.release_root, args.python), sort_keys=True))
        return 0
    except Exception as error:
        reason = str(error) if isinstance(error, ReleaseError) else "incremental-release-install-failed"
        print(json.dumps({"state": "held", "reason": reason}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
