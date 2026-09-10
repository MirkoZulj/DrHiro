#!/usr/bin/env python3
"""Build the FOCUSED incremental review bundle for the review #7 blocker fixes.

This is NOT a release candidate. It contains only the review #7 delta:

  * the functional diff for commits 6b0c39b and 8ea0bcb (together: base 8921c30 ->
    head 8ea0bcb), plus each commit's own patch;
  * the changed source files and regression tests;
  * the documentation/evidence commit 42fe5ba, identified separately;
  * evidence/review7_test_results.txt;
  * a full 64-char SHA-256 manifest over the payload.

Deliberately EXCLUDED: previous archives, exported review trees, and embedded
historical patches. The builder asserts that exclusion, asserts the payload inventory
matches the real git diff, and asserts every functional file in the diff is present.

Run from the repository root:
    python deploy/disposable/build_review7_bundle.py
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
BUILD = BUNDLE / "review7"
DIST = BUNDLE / "review7_focused_bundle.tar.gz"
ARCHIVE_NAME = "review7_focused_bundle.tar.gz"

# Exact full commit ids.
FUNC_BASE = "8921c305bce543f38653a5a948018a6ba33ba997"   # parent of 6b0c39b
B6 = "6b0c39b73cc33302c6367da69e5a2c8733911d75"
FUNC_HEAD = "8ea0bcbced78b0f2c1a210047873779cc9008577"   # parent of 42fe5ba
DOCS = "42fe5ba3255de74cc846a8a5aa4ac164cda6cb96"       # docs as first submitted
# Corrections head is resolved at build time (FUNC_HEAD..HEAD), so the range stays
# correct without pinning a hash that the build itself would invalidate.
DOCS_FIX = ""

FROZEN_CANDIDATE = "7e2cf6915dbd7478e8a558817d4d51aa63879e60"
FROZEN_ARCHIVE = BUNDLE / "rebuilt.tar.gz"
FROZEN_ARCHIVE_SHA = "4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d"

# Must never enter the payload: prior archives, exported review trees, historical
# patches, and the build output itself.
FORBIDDEN_SUFFIXES = (".tar.gz", ".tgz", ".zip")
FORBIDDEN_PATH_PARTS = (
    "review_bundle/export",
    "review_bundle/incremental",
    "review_bundle/review7",
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
    out = run("git", "diff", "--name-only", *rev_range)
    return [n for n in out.splitlines() if n.strip()]


def main() -> int:
    # ---- 0. frozen archive must be untouched ---------------------------------
    if FROZEN_ARCHIVE.exists():
        actual = sha256_file(FROZEN_ARCHIVE)
        if actual != FROZEN_ARCHIVE_SHA:
            print(f"ABORT: frozen archive changed.\n  expected {FROZEN_ARCHIVE_SHA}\n  actual   {actual}")
            return 1

    head_now = run("git", "rev-parse", "HEAD")
    docs_fix = head_now
    if docs_fix == FUNC_HEAD:
        print("ABORT: no documentation commit above the functional head")
        return 1
    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    func_files = sorted(set(names_changed([FUNC_BASE, FUNC_HEAD])))
    assert func_files, "functional diff is empty"

    # ---- 1. patches ----------------------------------------------------------
    patch_dir = BUILD / "patch"
    patch_dir.mkdir()

    combined = subprocess.run(
        ["git", "diff", FUNC_BASE, FUNC_HEAD], cwd=REPO,
        capture_output=True, text=True, check=True).stdout
    (patch_dir / "functional_8921c30_to_8ea0bcb.patch").write_text(combined)
    (patch_dir / f"{B6[:7]}.patch").write_text(
        subprocess.run(["git", "show", B6], cwd=REPO,
                       capture_output=True, text=True, check=True).stdout)
    (patch_dir / f"{FUNC_HEAD[:7]}.patch").write_text(
        subprocess.run(["git", "show", FUNC_HEAD], cwd=REPO,
                       capture_output=True, text=True, check=True).stdout)
    docs_patch = subprocess.run(
        ["git", "diff", FUNC_HEAD, DOCS], cwd=REPO,
        capture_output=True, text=True, check=True).stdout
    (patch_dir / "docs_8ea0bcb_to_42fe5ba.patch").write_text(docs_patch)

    # Accuracy corrections in response to review (NOT functional).
    docs_fix_patch = subprocess.run(
        ["git", "diff", DOCS, docs_fix], cwd=REPO,
        capture_output=True, text=True, check=True).stdout
    (patch_dir / f"docs_corrections_{DOCS[:7]}_to_{docs_fix[:7]}.patch").write_text(
        docs_fix_patch)

    # Verify the functional patch applies to a CLEAN WORKTREE OF THE BASE. Checking in
    # the current tree always fails (the changes are already applied). `git apply`
    # leaves new files untracked, which under-reports, so stage before diffing.
    import tempfile
    verify = Path(tempfile.mkdtemp(prefix="review7-verify-"))
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(verify), FUNC_BASE],
                       cwd=REPO, capture_output=True, text=True, check=True)
        subprocess.run(["git", "apply", str(patch_dir / "functional_8921c30_to_8ea0bcb.patch")],
                       cwd=verify, capture_output=True, text=True, check=True)
        run("git", "add", "-A", cwd=verify)
        stat = run("git", "diff", "--cached", "--stat", cwd=verify).splitlines()[-1]

        # Every functional file must be reproduced byte-identically by the patch.
        mismatched = []
        for rel in func_files:
            src, recon = REPO / rel, verify / rel
            if not recon.exists() or sha256_file(recon) != sha256_file(src):
                mismatched.append(rel)
        if mismatched:
            print(f"WARNING: patch did not reproduce: {mismatched}")
            return 1
        print(f"patch applies to clean base {FUNC_BASE[:7]}: {stat}")
        print(f"all {len(func_files)} functional files reproduced byte-identically")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(verify)],
                       cwd=REPO, capture_output=True, text=True)
        shutil.rmtree(verify, ignore_errors=True)

    # ---- 2. functional payload: changed source + regression tests ------------
    copied_func: list[str] = []
    for rel in func_files:
        src = REPO / rel
        if not src.exists():
            print(f"ABORT: functional file missing at HEAD: {rel}")
            return 1
        dst = BUILD / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied_func.append(rel)

    # ---- 3. documentation/evidence from 42fe5ba, identified separately -------
    # Documentation/evidence as it stands at the corrections commit, so the reviewer
    # reads the CORRECTED text (inventory, B7 status, uptime labelling, fencing
    # coverage). Both docs patches are shipped so the change is visible.
    docs_files = sorted(set(names_changed([FUNC_HEAD, docs_fix])))
    copied_docs: list[str] = []
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

## Functional diff (review #7 blockers)

- **base:** `{FUNC_BASE}`
- **commit 1:** `{B6}` — `fix(r4): adoption validator rejects invalid activities schemas`
- **head:**   `{FUNC_HEAD}` — `fix(t1): review #7 blockers 1-5+7`

The combined functional patch is `patch/functional_8921c30_to_8ea0bcb.patch`; the two
per-commit patches are alongside it. Reproduce from the repository root:

```
git apply --check patch/functional_8921c30_to_8ea0bcb.patch   # against {FUNC_BASE[:12]}
```

Verified during the build: the patch applies to a clean worktree of `{FUNC_BASE[:7]}`
and reproduces all {len(func_files)} functional files byte-identically.

## Documentation/evidence (NOT functional)

- **base:** `{FUNC_HEAD}`
- **submitted:** `{DOCS}` — `docs(t1): review #7 blocker fixes - plan REVISION 5, ...`
- **corrections:** `{docs_fix}` — `docs(t1): review #7 accuracy corrections + fuller evidence`

Patches: `patch/docs_8ea0bcb_to_{DOCS[:7]}.patch` (as submitted) and
`patch/docs_corrections_{DOCS[:7]}_to_{docs_fix[:7]}.patch` (accuracy corrections). These files
are present under `documentation/` **as corrected** (the corrections commit), listed separately
so packaging is not mistaken for new functional code.

The corrections commit addresses, from the review: the missing
`concurrent_probe.py` in the functional inventory; the misleading B7 scope claim
(now: NOT CLOSED, behaviour unimplemented); the uptime capability labelling; the
precise attempt-fencing coverage statement; and the fuller evidence (commands,
environment/versions, migration failing-before/passing-after).

## Frozen state (unchanged by this bundle)

- Candidate `{FROZEN_CANDIDATE}` — frozen, untouched.
- Frozen release archive `rebuilt.tar.gz` — SHA-256 `{FROZEN_ARCHIVE_SHA}` (verified
  unchanged by the build).
- This is a **focused incremental review bundle, not a release candidate**.

## Excluded

Previous archives, exported review trees, and embedded historical patches:
`rebuilt.tar.gz`, `incremental_review_bundle_R2.tar.gz`, `review_bundle/export/`,
`review_bundle/incremental/`, `drhiro_meal_liquid_review_bundle_FINAL.tar.gz`. The
builder asserts their absence from the payload.
""")

    # ---- 4. guard: no forbidden artifacts in the payload --------------------
    for path in BUILD.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(BUILD).as_posix()
        if rel.endswith(FORBIDDEN_SUFFIXES) or any(p in rel for p in FORBIDDEN_PATH_PARTS):
            print(f"ABORT: forbidden artifact in payload: {rel}")
            return 1

    # ---- 5. MANIFEST.sha256 - full 64-char, self-excluded -------------------
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

    # ---- 6. deterministic archive ------------------------------------------
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
    print(f"docs commit:      {DOCS} (submitted)")
    print(f"docs corrections: {docs_fix}")
    print(f"functional files: {len(copied_func)}")
    for f in copied_func:
        print(f"  + {f}")
    print(f"docs/evidence:    {len(copied_docs)} files (separate tree)")
    for f in copied_docs:
        print(f"  * {f}")
    print(f"archive:          {ARCHIVE_NAME}")
    print(f"sha256:           {sha256_file(DIST)}")
    print(f"bytes:            {DIST.stat().st_size}")
    print(f"entries:          {len(entries)} ({len(files)} files)")
    print(f"frozen archive unchanged: "
          f"{sha256_file(FROZEN_ARCHIVE) == FROZEN_ARCHIVE_SHA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
