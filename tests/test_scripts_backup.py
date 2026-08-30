"""Regression tests for scripts/backup.sh (Qodo #6).

Stubs docker (compose exec pg_dump + exports-volume copy) so the archive
inclusion and failure-path behavior are exercised without a live stack.
"""
from __future__ import annotations

import os
import subprocess
import tarfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO, "scripts")


def _write_stub(bindir, name, content):
    p = bindir / name
    p.write_text(content)
    p.chmod(0o755)
    return p


def _write_env(repo):
    (repo / ".env").write_text("TELEGRAM_BOT_TOKEN=redacted\nAI_MODEL=test\n")


def _run_backup(repo, bindir):
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    # The script cd's to $SCRIPT_DIR/.. — place a copy inside the temp repo so
    # it operates on the temp repo (reads its .env, writes its backups/).
    local_scripts = repo / "scripts"
    local_scripts.mkdir(exist_ok=True)
    import shutil
    shutil.copy(os.path.join(SCRIPTS_DIR, "backup.sh"), local_scripts / "backup.sh")
    r = subprocess.run(
        ["bash", str(local_scripts / "backup.sh")],
        capture_output=True, text=True, env=env, cwd=str(repo),
    )
    return r


@pytest.fixture()
def backup_env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_env(repo)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    return repo, bindir


def test_backup_fails_and_writes_no_archive_on_dump_failure(backup_env):
    """Qodo #6: a failed pg_dump must NOT produce an archive with an empty SQL."""
    repo, bindir = backup_env
    _write_stub(bindir, "docker", '''#!/usr/bin/env bash
if [[ "$1" == "compose" ]]; then
  if [[ "$*" == *pg_dump* ]]; then
    exit 1
  fi
fi
# docker run exports copy — make it succeed so the archive would be attempted
exit 0
''')
    r = _run_backup(repo, bindir)
    assert r.returncode != 0
    # No tarball and no empty dump file left behind.
    assert not list(repo.glob("backups/*.tar.gz"))
    assert "pg_dump failed" in r.stdout + r.stderr


def test_backup_includes_exports_volume(backup_env):
    """Qodo #6: the exports_data volume must be copied into the tarball, not
    merely mentioned in a note."""
    repo, bindir = backup_env
    _write_stub(bindir, "docker", '''#!/usr/bin/env bash
if [[ "$1" == "compose" ]]; then
  if [[ "$*" == *pg_dump* ]]; then echo "FAKE-DUMP"; exit 0; fi
fi
# docker run -v ..._exports_data:/data:ro -v STAGE:/out alpine sh -c 'cp ...'
# Simulate the volume by writing a brief into the stage dir (/out mount).
for a in "$@"; do
  if [[ "$a" == *":/out"* ]]; then
    stage="${a%%:*}"
    mkdir -p "$stage/exports"
    echo '{"brief":"demo"}' > "$stage/exports/visit-brief.json"
  fi
done
exit 0
''')
    r = _run_backup(repo, bindir)
    assert r.returncode == 0, r.stdout + r.stderr
    archives = list(repo.glob("backups/*.tar.gz"))
    assert archives, "expected a backup archive"
    # The archive must contain the exported brief.
    with tarfile.open(archives[0]) as tf:
        names = tf.getnames()
        assert any(n.endswith("trueforge-db.sql") for n in names)
        assert any(n.endswith("exports/visit-brief.json") for n in names)
        assert any(n.endswith("env-keys.txt") for n in names)
