# Base and head

- **Base (frozen candidate):** `7e2cf6915dbd7478e8a558817d4d51aa63879e60`
- **Packaging commit of the frozen archive:** `8870a4e`
- **Head of this incremental artifact:** `ee3f24b54f80723dbbe913d3c77b42cd0e571e8c`

Base/head as *decided* by the reviewer: the base is the frozen candidate
`7e2cf69`, the version pinned for review. `8870a4e` is the (later)
packaging commit that produced the frozen archive; no source changed between them.

Reproduce the delta (from the repository root):

```
git apply --check patch/incremental.patch
```

## Frozen release archive is unchanged

`rebuilt.tar.gz` (the frozen release candidate) is **not** repackaged or modified by
this artifact. Its SHA-256 remains:

```
4d2bf6851c869670308ad28961d32626a12cf2b2a2af389fc3e1f6c8fe59668d
```

This artifact is *incremental*: review it before preparing a new release candidate.
