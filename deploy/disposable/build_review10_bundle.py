#!/usr/bin/env python3
"""Build the FOCUSED incremental review bundle for the review-round-9 corrections.

NOT a release candidate. Contents:
  * the functional diff (base = the reviewed round-8 functional head), exact commit ids;
  * the changed source files and regression tests;
  * documentation/evidence, identified separately;
  * evidence/review10_test_results.txt;
  * a full 64-char SHA-256 manifest over the payload.

EXCLUDED: previous archives, exported review trees, embedded historical patches. The
builder asserts the payload inventory equals the real git diff, verifies the patch
applies to a clean worktree of the base, asserts the manifest, and refuses to build if
the frozen archive changed.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "docs/deliverables/meal-liquid-idempotency/review_bundle"
BUILD = BUNDLE / "review10"
DIST = BUNDLE / "review10_focused_bundle.tar.gz"

FUNC_BASE = "afa410366c10da0c02b39a97d9a8738539832579"   # fully-reviewed round-9 state (docs head)
FUNC_HEAD = ""                                            # filled from git log

FROZEN_CANDIDATE = "7e2cf6915dbd7478e8a558817d4d51aa63879e60"
FROZEN_ARCHIVE = BUNDLE / "rebuilt.tar.gz"
FROZEN_ARCHIVE_SHA = "4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d"

FORBIDDEN_SUFFIXES = (".tar.gz", ".tgz", ".zip")
FORBIDDEN_PATH_PARTS = (
    "review_bundle/export",
    "review_bundle/incremental",
    "review_bundle/review7",
    "review_bundle/review8",
    "review_bundle/review9",
    "review_bundle/review10",
    "drhiro_meal_liquid_review_bundle_FINAL",
)


def run(*cmd: str, cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd or REPO, capture_output=True, text=True,
                          check=True).stdout.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if FROZEN_ARCHIVE.exists():
        if sha256_file(FROZEN_ARCHIVE) != FROZEN_ARCHIVE_SHA:
            print("ABORT: frozen archive changed")
            return 1

    # The functional head is the fix(review10) commit; the docs head is HEAD.
    heads = run("git", "log", "--format=%H", "-10").splitlines()
    FUNC_HEAD = ""
    for h in heads:
        if "fix(review10)" in run("git", "log", "-1", "--format=%s", h):
            FUNC_HEAD = h
            break
    if not FUNC_HEAD:
        print("ABORT: fix(review10) commit not found")
        return 1
    docs_head = run("git", "rev-parse", "HEAD")

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    func_files = sorted(set(run("git", "diff", "--name-only", FUNC_BASE, FUNC_HEAD).splitlines()))
    assert func_files, "functional diff is empty"

    patch_dir = BUILD / "patch"
    patch_dir.mkdir()
    (patch_dir / f"functional_{FUNC_BASE[:7]}_to_{FUNC_HEAD[:7]}.patch").write_text(
        subprocess.run(["git", "diff", FUNC_BASE, FUNC_HEAD], cwd=REPO,
                       capture_output=True, text=True, check=True).stdout)
    (patch_dir / f"{FUNC_HEAD[:7]}.patch").write_text(
        subprocess.run(["git", "show", FUNC_HEAD], cwd=REPO,
                       capture_output=True, text=True, check=True).stdout)
    (patch_dir / f"docs_{FUNC_HEAD[:7]}_to_{docs_head[:7]}.patch").write_text(
        subprocess.run(["git", "diff", FUNC_HEAD, docs_head], cwd=REPO,
                       capture_output=True, text=True, check=True).stdout)

    verify = Path(tempfile.mkdtemp(prefix="review10-verify-"))
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(verify), FUNC_BASE],
                       cwd=REPO, capture_output=True, text=True, check=True)
        subprocess.run(["git", "apply",
                        str(patch_dir / f"functional_{FUNC_BASE[:7]}_to_{FUNC_HEAD[:7]}.patch")],
                       cwd=verify, capture_output=True, text=True, check=True)
        run("git", "add", "-A", cwd=verify)
        stat = run("git", "diff", "--cached", "--stat", cwd=verify).splitlines()[-1]
        mismatched = [rel for rel in func_files
                      if not (verify / rel).exists()
                      or sha256_file(verify / rel) != sha256_file(REPO / rel)]
        if mismatched:
            print(f"ABORT: patch did not reproduce: {mismatched}")
            return 1
        print(f"patch applies to clean base {FUNC_BASE[:7]}: {stat}")
        print(f"all {len(func_files)} functional files reproduced byte-identically")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(verify)],
                       cwd=REPO, capture_output=True, text=True)
        shutil.rmtree(verify, ignore_errors=True)

    for rel in func_files:
        src = REPO / rel
        if not src.exists():
            print(f"ABORT: functional file missing at HEAD: {rel}")
            return 1
        dst = BUILD / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    docs_files = sorted(set(run("git", "diff", "--name-only", FUNC_HEAD, docs_head).splitlines()))
    for rel in docs_files:
        src = REPO / rel
        if not src.exists():
            print(f"ABORT: docs file missing: {rel}")
            return 1
        dst = BUILD / "documentation" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    (BUILD / "BASE_AND_HEAD.md").write_text(f"""# Base and head

## Functional diff (review round 9)

- **base:** `{FUNC_BASE}` (the fully-reviewed round-8 state, docs head)
- **head:** `{FUNC_HEAD}`

Combined patch: `patch/functional_{FUNC_BASE[:7]}_to_{FUNC_HEAD[:7]}.patch`; the
per-commit patch is alongside it. Reproduce from the repository root:

```
git apply --check patch/functional_{FUNC_BASE[:7]}_to_{FUNC_HEAD[:7]}.patch
```

Verified during the build: the patch applies to a clean worktree of `{FUNC_BASE[:7]}`
and reproduces all {len(func_files)} functional files byte-identically. The functional
inventory is asserted against `git diff --name-only` (not hand-written).

## Documentation/evidence (NOT functional)

- **base:** `{FUNC_HEAD}`
- **head:** `{docs_head}`

These files are under `documentation/`, listed separately so packaging is not mistaken
for functional code.

## Frozen state (unchanged)

- Candidate `{FROZEN_CANDIDATE}` - frozen, untouched.
- Frozen archive `rebuilt.tar.gz` - SHA-256 `{FROZEN_ARCHIVE_SHA}` (verified unchanged).
- Focused incremental review bundle, not a release candidate.

## Evidence scope

`documentation/docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/review10_test_results.txt`

The evidence records the source path+hash ACTUALLY LOADED BY THE RUNNING INGRESS
CONTAINER (in addition to host and pre-fix worktree hashes), the migration
failing-before/passing-after output, and the stack-suite result. Passing disposable-stack
tests are evidence, not production acceptance; unchanged uptimes support 'no observed
restart' only.
""")

    for path in BUILD.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(BUILD).as_posix()
        if rel.endswith(FORBIDDEN_SUFFIXES) or any(p in rel for p in FORBIDDEN_PATH_PARTS):
            print(f"ABORT: forbidden artifact in payload: {rel}")
            return 1

    lines = []
    for path in sorted(BUILD.rglob("*")):
        if path.is_dir() or path.name == "MANIFEST.sha256":
            continue
        rel = path.relative_to(BUILD).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}  ({path.stat().st_size} bytes)")
    manifest = "\n".join(lines) + "\n"
    (BUILD / "MANIFEST.sha256").write_text(manifest)
    for line in manifest.strip().splitlines():
        digest, rest = line.split("  ", 1)
        rel = rest.rsplit("  (", 1)[0]
        if sha256_file(BUILD / rel) != digest:
            print(f"MANIFEST MISMATCH: {rel}")
            return 1

    if DIST.exists():
        DIST.unlink()
    with tarfile.open(DIST, "w:gz") as tf:
        for path in sorted(BUILD.rglob("*")):
            tf.add(path, arcname=path.relative_to(BUILD).as_posix(),
                   recursive=False, filter=lambda ti: (
                       setattr(ti, "uid", 0), setattr(ti, "gid", 0),
                       setattr(ti, "uname", ""), setattr(ti, "gname", ""), ti)[-1])

    with tarfile.open(DIST) as tf:
        entries = tf.getnames()
    files = [e for e in entries if not e.endswith("/")]

    print(f"base:             {FUNC_BASE}")
    print(f"head:             {FUNC_HEAD}")
    print(f"docs head:        {docs_head}")
    print(f"functional files: {len(func_files)} (== git diff, asserted)")
    for f in func_files:
        print(f"  + {f}")
    print(f"docs/evidence:    {len(docs_files)} files (separate tree)")
    for f in docs_files:
        print(f"  * {f}")
    print(f"archive:          {DIST.name}")
    print(f"sha256:           {sha256_file(DIST)}")
    print(f"bytes:            {DIST.stat().st_size}")
    print(f"entries:          {len(entries)} ({len(files)} files)")
    print(f"frozen archive unchanged: {sha256_file(FROZEN_ARCHIVE) == FROZEN_ARCHIVE_SHA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
