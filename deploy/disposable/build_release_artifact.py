#!/usr/bin/env python3
"""Build the RELEASE artifact for the meal+liquid idempotency checkpoint.

Distinct from every review bundle: new name, new checksum. The frozen release archive
(`review_bundle/rebuilt.tar.gz`) is verified unchanged and is NOT repackaged by this
script.

Contents:
  RELEASE.md              - the deployment sheet (features, migrations, backup state,
                            smoke tests, rollback, limitations, verdict)
  BASE_AND_HEAD.md        - exact commit ids for the release and its base
  DIFF.patch              - functional diff base -> release commit
  files/                  - the changed functional files, byte-identical to the tree
  evidence/               - captured release verification output
  MANIFEST.sha256         - sha256 + byte size of every payload file (self-excluded)
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
BUNDLE = REPO / "docs/deliverables/meal-liquid-idempotency"
BUILD = BUNDLE / ".release_build"
DIST = BUNDLE / "drhiro_release_2026-09-11_review12.tar.gz"

BASE = "7e2cf6915dbd7478e8a558817d4d51aa63879e60"          # FROZEN CANDIDATE (release baseline)
REVIEW_BASE = "74ca71f8dec9368c3274b2166a5d9b19d3b94a2b"   # base of the last reviewed increment
RELEASE = "50bec02db48358ae9ef32f1640e23e347b8e9057"       # functional release commit
FROZEN_ARCHIVE = BUNDLE / "review_bundle/rebuilt.tar.gz"
FROZEN_SHA = "4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d"

DOCS = [
    "docs/deliverables/meal-liquid-idempotency/DEPLOYMENT_SHEET.md",
    "docs/deliverables/meal-liquid-idempotency/release_evidence.txt",
]


def run(*cmd: str, cwd: Path | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd or REPO, capture_output=True, text=True,
                          check=True).stdout.strip()


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    if FROZEN_ARCHIVE.exists() and sha256(FROZEN_ARCHIVE) != FROZEN_SHA:
        print("ABORT: frozen archive changed - do not repackage")
        return 1
    if run("git", "rev-parse", f"{RELEASE}^{{commit}}") != RELEASE:
        print("ABORT: release commit not resolvable")
        return 1

    # SHIPPABLE code only: the paths that build into images. Everything else in the
    # range is review-bundle/documentation churn and must not be presented as release
    # code.
    func_files = sorted(set(run("git", "diff", "--diff-filter=d", "--name-only",
                                BASE, RELEASE, "--",
                                "apps", "packages", "services", "infra",
                                "docker-compose.yml").splitlines()))
    if not func_files:
        print("ABORT: functional diff is empty")
        return 1

    if BUILD.exists():
        shutil.rmtree(BUILD)
    (BUILD / "files").mkdir(parents=True)
    (BUILD / "evidence").mkdir()

    (BUILD / "DIFF.patch").write_text(
        subprocess.run(["git", "diff", BASE, RELEASE, "--",
                        "apps", "packages", "services", "infra", "docker-compose.yml"],
                       cwd=REPO, capture_output=True, text=True, check=True).stdout)
    (BUILD / "DIFF_reviewed_increment.patch").write_text(
        subprocess.run(["git", "diff", REVIEW_BASE, RELEASE], cwd=REPO,
                       capture_output=True, text=True, check=True).stdout)
    payload_extra = ["DIFF_reviewed_increment.patch"]

    payload = list(payload_extra)
    for rel in func_files:
        dst = BUILD / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dst)
        payload.append(f"files/{rel}")

    for rel in DOCS:
        src = REPO / rel
        if not src.exists():
            print(f"ABORT: missing doc {rel}")
            return 1
        shutil.copy2(src, BUILD / "evidence" / src.name)
        payload.append(f"evidence/{src.name}")
    # the deployment sheet also sits at the root as RELEASE.md
    shutil.copy2(REPO / DOCS[0], BUILD / "RELEASE.md")
    payload.append("RELEASE.md")

    packaging = run("git", "rev-parse", "HEAD")
    (BUILD / "BASE_AND_HEAD.md").write_text(f"""# Release identity

- **release commit (functional):** `{RELEASE}`
- **release baseline (frozen candidate):** `{BASE}`
- **base of the last reviewed increment:** `{REVIEW_BASE}`
- **frozen candidate == release baseline (UNCHANGED):** `7e2cf6915dbd7478e8a558817d4d51aa63879e60`
- **frozen archive sha256 (UNCHANGED):** `{FROZEN_SHA}`

This artifact is NEW (`{DIST.name}`). It does not replace, and must not be confused
with, the frozen release archive or any review bundle.

- **packaging commit (carries the docs/evidence in this artifact):** `{packaging}`
  *Recorded here rather than inside the sheet itself: a file cannot cite the hash of the
  commit that contains it, so the sheet names the functional commit and this file names
  the packaging commit.*
""")
    payload.append("BASE_AND_HEAD.md")

    # ---- verify payload byte-identity and patch applicability ----
    verify = Path(tempfile.mkdtemp(prefix="release-verify-"))
    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(verify), BASE],
                       cwd=REPO, capture_output=True, text=True, check=True)
        subprocess.run(["git", "apply", str(BUILD / "DIFF.patch")],
                       cwd=verify, capture_output=True, text=True, check=True)
        bad = [f for f in func_files
               if sha256(verify / f) != sha256(REPO / f)]
        if bad:
            print(f"ABORT: patch did not reproduce {bad}")
            return 1
        print(f"patch applies to clean base {BASE[:7]} and reproduces "
              f"{len(func_files)} files byte-identically")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(verify)],
                       cwd=REPO, capture_output=True, text=True)

    # ---- manifest (self-excluded) ----
    lines = []
    for rel in sorted(payload):
        p = BUILD / rel
        lines.append(f"{sha256(p)}  {rel}  ({p.stat().st_size} bytes)")
    (BUILD / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")

    # ---- archive ----
    with tarfile.open(DIST, "w:gz") as tf:
        for path in sorted(BUILD.rglob("*")):
            tf.add(path, arcname=str(path.relative_to(BUILD)))

    print(f"release artifact: {DIST.name}")
    print(f"sha256:           {sha256(DIST)}")
    print(f"bytes:            {DIST.stat().st_size}")
    print(f"functional files: {len(func_files)}")
    for f in func_files:
        print(f"  + {f}")
    print(f"payload entries:  {len(payload)}")
    print(f"frozen archive unchanged: "
          f"{sha256(FROZEN_ARCHIVE) == FROZEN_SHA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
