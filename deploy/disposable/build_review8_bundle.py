#!/usr/bin/env python3
"""Build the FOCUSED incremental review bundle for the review-round-8 corrections.

NOT a release candidate. Contents:

  * the functional diff for the round-8 fix commit (base = the reviewed head), with
    exact full base/head commit ids;
  * the changed source files and regression tests;
  * documentation/evidence commits, identified separately;
  * evidence/review8_test_results.txt;
  * a full 64-char SHA-256 manifest over the payload.

Deliberately EXCLUDED: previous archives, exported review trees, embedded historical
patches. The builder asserts the payload inventory equals the real git diff, asserts
no forbidden artifact is present, and re-verifies the manifest by re-reading it.

Run from the repository root:
    python deploy/disposable/build_review8_bundle.py
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "docs/deliverables/meal-liquid-idempotency/review_bundle"
BUILD = BUNDLE / "review8"
DIST = BUNDLE / "review8_focused_bundle.tar.gz"
ARCHIVE_NAME = "review8_focused_bundle.tar.gz"

FUNC_BASE = "d46ed10bde10bd415a06f943c6eff5e6ce526ae0"   # the reviewed head
FUNC_HEAD = "1b7f7523bffaa7386b5cdcd1e7ecb698388d6662"   # round-8 functional fix

FROZEN_CANDIDATE = "7e2cf6915dbd7478e8a558817d4d51aa63879e60"
FROZEN_ARCHIVE = BUNDLE / "rebuilt.tar.gz"
FROZEN_ARCHIVE_SHA = "4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d"

FORBIDDEN_SUFFIXES = (".tar.gz", ".tgz", ".zip")
FORBIDDEN_PATH_PARTS = (
    "review_bundle/export",
    "review_bundle/incremental",
    "review_bundle/review7",
    "review_bundle/review8",
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


def names_changed(rev_range: list[str]) -> list[str]:
    return [n for n in run("git", "diff", "--name-only", *rev_range).splitlines() if n.strip()]


def main() -> int:
    if FROZEN_ARCHIVE.exists():
        actual = sha256_file(FROZEN_ARCHIVE)
        if actual != FROZEN_ARCHIVE_SHA:
            print(f"ABORT: frozen archive changed.\n  expected {FROZEN_ARCHIVE_SHA}\n  actual   {actual}")
            return 1

    docs_head = run("git", "rev-parse", "HEAD")
    if docs_head == FUNC_HEAD:
        print("ABORT: no documentation commit above the functional head")
        return 1
    if run("git", "rev-parse", FUNC_BASE) != FUNC_BASE:
        print("ABORT: functional base not present")
        return 1

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    func_files = sorted(set(names_changed([FUNC_BASE, FUNC_HEAD])))
    assert func_files, "functional diff is empty"

    # ---- patches -------------------------------------------------------------
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

    # Verify against a CLEAN WORKTREE OF THE BASE (checking in the working tree always
    # fails: the changes are already applied).
    import tempfile
    verify = Path(tempfile.mkdtemp(prefix="review8-verify-"))
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(verify), FUNC_BASE],
                       cwd=REPO, capture_output=True, text=True, check=True)
        subprocess.run(["git", "apply", str(patch_dir / f"functional_{FUNC_BASE[:7]}_to_{FUNC_HEAD[:7]}.patch")],
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

    # ---- functional payload --------------------------------------------------
    copied_func = []
    for rel in func_files:
        src = REPO / rel
        if not src.exists():
            print(f"ABORT: functional file missing at HEAD: {rel}")
            return 1
        dst = BUILD / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied_func.append(rel)

    # ---- documentation/evidence (separate tree) -----------------------------
    docs_files = sorted(set(names_changed([FUNC_HEAD, docs_head])))
    copied_docs = []
    for rel in docs_files:
        src = REPO / rel
        if not src.exists():
            print(f"ABORT: docs file missing: {rel}")
            return 1
        dst = BUILD / "documentation" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied_docs.append(rel)

    (BUILD / "BASE_AND_HEAD.md").write_text(f"""# Base and head

## Functional diff (review round 8)

- **base:** `{FUNC_BASE}` (the head that was reviewed in review7_focused_bundle)
- **head:** `{FUNC_HEAD}` — `fix(review8): duplicate receipts, retry budget, attempt
  fencing, strict confirm, conservative schema`

Combined patch: `patch/functional_{FUNC_BASE[:7]}_to_{FUNC_HEAD[:7]}.patch`; the
per-commit patch is alongside it. Reproduce from the repository root:

```
git apply --check patch/functional_{FUNC_BASE[:7]}_to_{FUNC_HEAD[:7]}.patch
```

Verified during the build: the patch applies to a clean worktree of `{FUNC_BASE[:7]}`
and reproduces all {len(func_files)} functional files byte-identically.

**The functional inventory is asserted programmatically against
`git diff --name-only {FUNC_BASE[:7]} {FUNC_HEAD[:7]}`**, so it cannot drift from the
real diff (the review of the previous bundle caught a hand-written inventory that
omitted a functional file).

## Documentation/evidence (NOT functional)

- **base:** `{FUNC_HEAD}`
- **head:** `{docs_head}`

Patch: `patch/docs_{FUNC_HEAD[:7]}_to_{docs_head[:7]}.patch`. These files are present
under `documentation/`, listed separately so packaging is not mistaken for functional
code.

## Frozen state (unchanged by this bundle)

- Candidate `{FROZEN_CANDIDATE}` — frozen, untouched.
- Frozen release archive `rebuilt.tar.gz` — SHA-256 `{FROZEN_ARCHIVE_SHA}` (verified
  unchanged by this build).
- This is a **focused incremental review bundle, not a release candidate**.

## Excluded

Previous archives, exported review trees, embedded historical patches:
`rebuilt.tar.gz`, `incremental_review_bundle_R2.tar.gz`,
`review7_focused_bundle.tar.gz`, `review_bundle/export/`, `review_bundle/incremental/`,
`review_bundle/review7/`, `drhiro_meal_liquid_review_bundle_FINAL.tar.gz`. The builder
asserts their absence from the payload.

## Evidence scope

`documentation/docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/review8_test_results.txt`

Suite results captured from a disposable stack are **evidence, not production
acceptance**. Container uptimes unchanged support "no observed restart" only; they do
not independently prove that configuration, credentials, or data were unchanged.
""")

    # ---- guard ---------------------------------------------------------------
    for path in BUILD.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(BUILD).as_posix()
        if rel.endswith(FORBIDDEN_SUFFIXES) or any(p in rel for p in FORBIDDEN_PATH_PARTS):
            print(f"ABORT: forbidden artifact in payload: {rel}")
            return 1

    # ---- manifest ------------------------------------------------------------
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

    # ---- archive -------------------------------------------------------------
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
    print(f"functional files: {len(copied_func)} (== git diff, asserted)")
    for f in copied_func:
        print(f"  + {f}")
    print(f"docs/evidence:    {len(copied_docs)} files (separate tree)")
    for f in copied_docs:
        print(f"  * {f}")
    print(f"archive:          {ARCHIVE_NAME}")
    print(f"sha256:           {sha256_file(DIST)}")
    print(f"bytes:            {DIST.stat().st_size}")
    print(f"entries:          {len(entries)} ({len(files)} files)")
    print(f"frozen archive unchanged: {sha256_file(FROZEN_ARCHIVE) == FROZEN_ARCHIVE_SHA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
