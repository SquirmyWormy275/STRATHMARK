# Package Release Boundary

STRATHMARK publication is a separate authorization boundary from repository merge,
passing CI, and V3 rehearsal evidence. None of those states publishes a package or
enables V3 production authority.

## First public package

The first public package candidate is the immutable V2 portable library at
`strathmark==2.0.0`, source tag `v2.0.0`, commit
`a231ad65fe82317516cc82a282761d73adb0c0e3`. It does not promise V3 production
eligibility. The current `3.0.0rc1` branch is not a substitute for that V2 release.

The historical `v2.0.0` tag is lightweight and must not be moved or replaced. A
separately authorized, protected, annotated `pypi-v2.0.0` tag must point to that
same commit. The publisher verifies both tags, their peeled commits, the checked-out
commit, and `[project].version` before it builds anything.

## Platform truth

V2 is a portable Python library. Current V3 source and contracts can be inspected and
tested on supported Python platforms, but V3 race-day authority is a designated
Windows installation contract and the release-candidate wheel carries a Windows DLL.
The current project therefore makes no blanket OS-independent metadata claim. Package
installation, V3 rehearsal readiness, and V3 production eligibility remain distinct.

## Guarded publication sequence

1. Merge the guarded workflows and this policy to `main`.
2. Configure active GitHub tag rulesets protecting `v*` and `pypi-v*`.
3. Configure the `testpypi` GitHub environment with required reviewers and a
   `main`-only deployment-branch rule, then configure its TestPyPI trusted publisher.
4. Create the annotated `pypi-v2.0.0` authorization tag at the existing `v2.0.0`
   commit only after explicit release authorization.
5. Run **Rehearse package on TestPyPI** from `main` with `pypi-v2.0.0` and review
   the exact artifact/install evidence.
6. Configure the `pypi` environment with required reviewers and a `main`-only
   deployment-branch rule, then configure the PyPI pending trusted publisher for
   owner `SquirmyWormy275`, repository `STRATHMARK`, workflow `publish.yml`,
   environment `pypi`.
7. After a separate explicit production-publication approval, run **Publish to PyPI**
   from `main` with the same annotated tag.

Both workflows use OIDC trusted publishing, require environment approval, build an
sdist and wheel, run `twine check`, and install the exact artifacts outside the source
checkout. Declared extras are installed from the exact wheel in isolated environments.
The workflows fail closed if dispatched from any branch other than `main` or if the
authorization tag, source tag, version, checkout, or cleanliness checks disagree.

Creating tags, changing GitHub/PyPI settings, uploading to TestPyPI/PyPI, and enabling
V3 production selection are deliberately outside ordinary code delivery.
