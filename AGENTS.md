# Agent Instructions

The approved [modular agreement](docs/MODULAR_ECOSYSTEM.md) and
[implemented managed contract](docs/MANAGED_EVENTS.md) distinguish calculation-only
consumer authority from delegated operations. Preserve the existing numerical
engines and frozen contracts. Synthetic managed v1 is not production activation.

## Required domain reading

Before analyzing, changing, or reviewing handicap predictions, marks, start sheets,
simulations, results, or related documentation, read
[`docs/wiki/Handicap-Mark-Math.md`](docs/wiki/Handicap-Mark-Math.md) in full.

That page is the repository source of truth for what a woodchopping handicap is, what
it is intended to accomplish, and how staggered starts, rebasing, reference marks, and
multi-round fields must be interpreted.

Do not elevate one association's penalty table, conversion scale, draw procedure, or
committee practice into a universal project requirement. Governing rulebooks control
their competitions; the source-of-truth page controls domain meaning within this
repository.

After the domain reading, follow [`ONBOARDING.md`](ONBOARDING.md) for the current
engine, architecture, safety boundaries, and verification workflow.

## Standing order: keep PyPI current

The owner authorized automatic PyPI publication on 2026-10-12. Every new package
version merged to `main` must reach PyPI automatically after successful main CI and
the exact wheel/sdist installation checks. This standing authorization includes
release candidates, preserving their prerelease versions; it does not grant V3
production eligibility or change a competition's selected engine.

Keep `.github/workflows/publish.yml` automatic publishing enabled. Bump
`[project].version` when shipping changed package behavior or contents; never move
an existing `v*` or `pypi-v*` tag or attempt to replace uploaded files. Follow
[`docs/PACKAGE_RELEASE.md`](docs/PACKAGE_RELEASE.md), verify the published artifact
hashes and an isolated installation from PyPI, and repair/report any failed upload
before calling a version release complete. No additional per-version publication
confirmation is needed under this standing order.

On 2026-10-13 the owner also authorized automatic matching GitHub Releases and
release failure alerts. After PyPI verification, publish source-bound release notes,
the exact verified wheel/sdist and checksums using the existing immutable source tag.
Keep candidates marked as prereleases. Preserve existing published assets; an
interrupted draft may resume only with matching hashes. Keep owner-assigned main
CI/publication failure alerts enabled and verify recovery before closing a release.
