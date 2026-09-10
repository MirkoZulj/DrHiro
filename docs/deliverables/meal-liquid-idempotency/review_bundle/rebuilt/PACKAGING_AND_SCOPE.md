# Packaging + review-scope reconciliation (corrected)

## Release candidate
- **Candidate under review:** 7e2cf6915dbd7478e8a558817d4d51aa63879e60
  (contains the concurrency fix + concurrent-recovery regression test)
- **Superseded checkpoint (NOT the candidate):** ec6b538d64330a93f7d7667521aa95f8c73a6af0

## Bases (reproducible)
- **T1 slice base:** dfc976e760554837ff71bfa63756507c73956e8b
  (`git diff dfc976e..7e2cf69` = the T1-only slice, 58 files)
- **FULL remediation base (merge-base with main):**
  40ff524ee820ea5507e06fca36890ca917381608
  (`git diff 40ff524..7e2cf69` = ALL R1-R6 + T1 repairs, 27 commits, 135 files)

## Scope honesty
The T1-only patch (dfc976e..HEAD) is NOT the complete remediation: it covers the
T1 trusted-ingress work. The earlier R1-R6 repairs (consumption idempotency,
nutrition resolution, ORM<->Alembic parity, writer flags, coexistence, etc.) live
in commits BEFORE dfc976e on this branch. The reproducible baseline containing ALL
of them is 40ff524 (merge-base with main). Therefore:
- `patch/full_remediation_40ff524__7e2cf69.patch` = complete remediation bundle.
- `patch/t1_slice_dfc976e__7e2cf69.patch` = T1-only slice (partial; NOT the whole).

## Payload-file count reconciliation
Earlier index listed 23 checksums; the export dir held 33 files (11 source + 7
tests + 9 migrations + 5 docs + 1 patch). The gap: 9 migrations + 1 patch were
copied into the export but never checksummed. This rebuilt bundle checksums EVERY
delivered payload file with its full 64-char SHA-256 in MANIFEST.sha256 (which
lists itself nowhere), and provides the archive checksum separately.

## Verification performed (worktree)
The full-remediation patch was applied to a clean `git worktree` checkout of
40ff524 and the suites were run there:
- concurrent-recovery regression: 3/3 passed (incl. the uncertain-commit race)
- T1 vertical slice: 25/25 passed
- T1 endpoint: 10/10  |  T1 bridge wiring: 10/10  |  writer-gate routes: 27/27  |  MCP writer gate: 10/10
- gated R1/R4 (Alembic DB): 17/17 passed
- R3 reconciliation: 10/10 passed
- default suite (clean env): 331 passed / 46 skipped / 0 errors
  (46 skipped = the four env-gated acceptance suites: T1 vertical slice 24+1,
   T1 endpoint 10, R1 3, R4 8+... they run when their DRHIRO_*_ALEMBIC_DB=1 gate
   is set, reported separately as the enabled acceptance suites above.)

The stale drhiro_meal_liquid_review_bundle_FINAL.tar.gz (committed at fe9f6b8,
repo root) is EXCLUDED from both patches so they apply cleanly; it is a historical
artifact, not a review payload.

## Unimplemented designs (PROPOSALS)
- OpenClaw ingress patch (spool module) - PROPOSAL, not implemented.
- scope=model credential - PROPOSAL, not implemented (JWT removal + egress bound
  still required; see T1_design_addendum.md section 2a).
Production-compatible ingress remains an architectural blocker.
