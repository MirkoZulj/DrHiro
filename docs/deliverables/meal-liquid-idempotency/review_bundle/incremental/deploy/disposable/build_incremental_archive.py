#!/usr/bin/env python3
"""Build the incremental review archive for the R2 checkpoint.

Payload = the new work developed on top of the FROZEN candidate (`7e2cf69`). The
frozen release archive (`rebuilt.tar.gz`) is deliberately NOT touched.

The archive carries:
  * the incremental patch (frozen candidate -> head), so the delta is reviewable
  * source, migrations, Compose configuration, tests and evidence as plain files
  * MANIFEST.sha256 with a full 64-char SHA-256 for every payload file
  * BASE_AND_HEAD.md recording the exact base/head commits
  * REVIEW_ANSWERS.md (the five review answers, copied from INCREMENTAL_REVIEW_R2.md)

Run from the repository root:  python deploy/disposable/build_incremental_archive.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BUNDLE = REPO / "docs/deliverables/meal-liquid-idempotency/review_bundle"
BUILD = BUNDLE / "incremental"
DIST = BUNDLE / "incremental_review_bundle_R2.tar.gz"
ARCHIVE_NAME = "incremental_review_bundle_R2.tar.gz"

# The frozen candidate: the review pin. Everything below is the delta on top of it.
FROZEN = "7e2cf6915dbd7478e8a558817d4d51aa63879e60"
PACKAGING_COMMIT = "8870a4e"          # packaging commit for the frozen archive
FROZEN_ARCHIVE = BUNDLE / "rebuilt.tar.gz"
FROZEN_ARCHIVE_SHA = "4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d"

# Files added/changed by this checkpoint. Grouped for the reviewer.
PAYLOAD = [
    # --- new source -----------------------------------------------------------
    "apps/api/src/drhiro_api/services/ingress_keys.py",
    # --- migration ------------------------------------------------------------
    "apps/api/src/drhiro_api/schema_activities.py",
    "apps/api/alembic/versions/b7c8d9e0f1a2_activities_table.py",
    # --- compose configuration + disposable stack ----------------------------
    "deploy/disposable/docker-compose.isolated.yml",
    "deploy/disposable/Dockerfile",
    "deploy/disposable/README.md",
    "deploy/disposable/stack_access.py",
    "deploy/disposable/capture_evidence.sh",
    "deploy/disposable/build_incremental_archive.py",
    "deploy/disposable/app/ingress.py",
    "deploy/disposable/app/seed_catalog.py",
    "deploy/disposable/app/concurrent_probe.py",
    "deploy/disposable/app/fake_telegram.py",
    "deploy/disposable/app/openclaw_stub.py",
    "deploy/disposable/app/mcp_stub.py",
    "deploy/disposable/app/probe_isolation.py",
    "deploy/disposable/app/verify_rotation.py",
    "deploy/disposable/app/stack_ctl.py",
    "deploy/disposable/sql/001_trusted.sql",
    # --- tests -----------------------------------------------------------------
    "tests/test_r1_stack_isolation.py",
    "tests/test_r2_trusted_key_set.py",
    "tests/test_r4_activities_migration.py",
    "tests/test_t1_isolated_ingress_stack.py",
    # --- diagnostics (explicitly NOT tests) ------------------------------------
    "scripts/diagnose_deployment_isolation.py",
    # --- docs ------------------------------------------------------------------
    "docs/deliverables/meal-liquid-idempotency/review_bundle/T1_implementation_plan.md",
    "docs/deliverables/meal-liquid-idempotency/review_bundle/INCREMENTAL_REVIEW_R2.md",
    # --- evidence ---------------------------------------------------------------
    "docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/incremental_evidence.txt",
    "docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/isolated_stack_tests_output.txt",
    "docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/isolated_stack_boundary_evidence.txt",
    "docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/activities_migration_e2e_output.txt",
    "docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/verify_activities_migration_e2e.py",
    "docs/deliverables/meal-liquid-idempotency/review_bundle/evidence/current_deployment_access_diagnostic.txt",
]


def run(*cmd: str) -> str:
    return subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                          check=True).stdout.strip()


def run_in(cwd: Path, *cmd: str) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          check=True).stdout.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    head = run("git", "rev-parse", "HEAD")

    # Refuse to touch the frozen archive.
    if FROZEN_ARCHIVE.exists():
        actual = sha256_file(FROZEN_ARCHIVE)
        if actual != FROZEN_ARCHIVE_SHA:
            print(f"ABORT: frozen archive changed. expected {FROZEN_ARCHIVE_SHA}")
            print(f"       actual   {actual}")
            return 1

    if BUILD.exists():
        shutil.rmtree(BUILD)
    BUILD.mkdir(parents=True)

    # 1. Incremental patch: frozen candidate -> head, excluding the stale binary.
    # Excluded from the textual patch: binaries git cannot represent as a patch, and
    # generated artifacts. The frozen release archive is deliberately excluded - it is
    # delivered separately and must stay byte-identical.
    EXCLUDE = [
        ":(exclude)drhiro_meal_liquid_review_bundle_FINAL.tar.gz",
        ":(exclude)docs/deliverables/meal-liquid-idempotency/review_bundle/rebuilt.tar.gz",
        ":(exclude)docs/deliverables/meal-liquid-idempotency/review_bundle/incremental/*",
        ":(exclude)docs/deliverables/meal-liquid-idempotency/review_bundle"
        "/incremental_review_bundle_R2.tar.gz",
    ]
    patch = subprocess.run(
        ["git", "diff", FROZEN, head, "--", ".", *EXCLUDE],
        cwd=REPO, capture_output=True, text=True, check=True).stdout
    patch_dir = BUILD / "patch"
    patch_dir.mkdir()
    (patch_dir / "incremental.patch").write_text(patch)

    # Verify the patch applies to a CLEAN CHECKOUT OF THE BASE, not to the current
    # tree (which already contains the changes, so --check would fail by definition).
    verify_dir = Path(tempfile.mkdtemp(prefix="incr-verify-"))
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(verify_dir), FROZEN],
                       cwd=REPO, capture_output=True, text=True, check=True)
        subprocess.run(["git", "apply", "--check",
                        str(patch_dir / "incremental.patch")],
                       cwd=verify_dir, capture_output=True, text=True, check=True)
        # Apply for real, and confirm the resulting tree equals HEAD for the payload.
        subprocess.run(["git", "apply", str(patch_dir / "incremental.patch")],
                       cwd=verify_dir, capture_output=True, text=True, check=True)
        applied = run_in(verify_dir, "git", "diff", "--stat")
        print(f"patch verified against clean base {FROZEN[:12]}: applied cleanly")
        print(f"  {applied.splitlines()[-1] if applied.splitlines() else 'no diff'}")
        for rel in PAYLOAD:
            base_file = verify_dir / rel
            head_file = REPO / rel
            if head_file.exists() and (not base_file.exists()
                                       or sha256_file(base_file) != sha256_file(head_file)):
                print(f"  NOTE: differs after patch application: {rel}")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(verify_dir)],
                       cwd=REPO, capture_output=True, text=True)
        shutil.rmtree(verify_dir, ignore_errors=True)

    # 2. Payload files, preserving their repository-relative paths.
    copied = []
    for rel in PAYLOAD:
        src = REPO / rel
        if not src.exists():
            print(f"ABORT: payload file missing: {rel}")
            return 1
        dst = BUILD / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)

    # 3. Base/head provenance.
    (BUILD / "BASE_AND_HEAD.md").write_text(f"""# Base and head

- **Base (frozen candidate):** `{FROZEN}`
- **Packaging commit of the frozen archive:** `{PACKAGING_COMMIT}`
- **Head of this incremental artifact:** `{head}`

Base/head as *decided* by the reviewer: the base is the frozen candidate
`7e2cf69`, the version pinned for review. `{PACKAGING_COMMIT}` is the (later)
packaging commit that produced the frozen archive; no source changed between them.

Reproduce the delta (from the repository root):

```
git apply --check patch/incremental.patch
```

## Frozen release archive is unchanged

`rebuilt.tar.gz` (the frozen release candidate) is **not** repackaged or modified by
this artifact. Its SHA-256 remains:

```
{FROZEN_ARCHIVE_SHA}
```

This artifact is *incremental*: review it before preparing a new release candidate.
""")

    # 4. Review answers alongside the manifest.
    answers = BUNDLE / "INCREMENTAL_REVIEW_R2.md"
    if answers.exists():
        shutil.copy2(answers, BUILD / "REVIEW_ANSWERS.md")
        copied.append("REVIEW_ANSWERS.md")

    # 5. MANIFEST.sha256 - full 64-char digests, self-excluded.
    lines = []
    for path in sorted(BUILD.rglob("*")):
        if path.is_dir() or path.name == "MANIFEST.sha256":
            continue
        rel = path.relative_to(BUILD).as_posix()
        lines.append(f"{sha256_file(path)}  {rel}  ({path.stat().st_size} bytes)")
    manifest = "\n".join(lines) + "\n"
    (BUILD / "MANIFEST.sha256").write_text(manifest)

    # 6. Verify the manifest, excluding itself.
    failures = 0
    for line in manifest.strip().splitlines():
        digest, rel = line.split("  ", 1)
        rel = rel.rsplit("  (", 1)[0]
        if sha256_file(BUILD / rel) != digest:
            failures += 1
            print(f"MANIFEST MISMATCH: {rel}")
    if failures:
        return 1

    # 7. Deterministic archive.
    if DIST.exists():
        DIST.unlink()
    subprocess.run(
        ["tar", "--sort=name", "--owner=0", "--group=0", "--numeric-owner",
         "-czf", str(DIST), "-C", str(BUILD), "."],
        check=True,
    )

    entries = subprocess.run(["tar", "-tzf", str(DIST)], capture_output=True,
                             text=True, check=True).stdout.strip().splitlines()
    files = [e for e in entries if not e.endswith("/")]

    print(f"base:    {FROZEN}")
    print(f"head:    {head}")
    print(f"payload: {len(copied)} files listed, manifest verified, 0 failures")
    print(f"archive: {DIST.name}")
    print(f"sha256:  {sha256_file(DIST)}")
    print(f"bytes:   {DIST.stat().st_size}")
    print(f"entries: {len(entries)} ({len(files)} files)")
    print(f"frozen archive untouched: {sha256_file(FROZEN_ARCHIVE) == FROZEN_ARCHIVE_SHA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
