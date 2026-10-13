# Package Release Boundary

## Standing publication authorization

On 2026-10-12 the owner authorized publishing the current STRATHMARK version to
PyPI immediately and keeping PyPI automatically updated with future versions.
`3.0.0rc8` packages that automatic publisher and the current maintained source;
its tags and publication are created by the successful-main-CI release sequence.
`3.0.0rc9` adds automatic matching GitHub Releases and owner-assigned failure alerts,
authorized by the owner on 2026-10-13.
`3.0.0rc10` fixes draft discovery/readback using the authenticated paginated release
collection and immutable release ID. GitHub's by-tag endpoint exposes published
releases only; rc9's first GitHub publication stopped at draft readback and raised
an owner-assigned alert after its PyPI upload and indexed installation passed.
`3.0.0rc7` is published from immutable source tag `v3.0.0rc7`, commit
`ca12329a3c7fefccb54d6a432711cdb2da2c00db`, authorized by annotated tag
`pypi-v3.0.0rc7`. V2 portable library releases `2.0.0` and `2.0.1` remain available.

Publication implements this standing order; it does not enable V3 production
authority, migrate competition data, promote a model or select a competition engine.
Release candidates retain their Python prerelease version. Install the latest
candidate with `python -m pip install --upgrade --pre strathmark`; ordinary upgrades
continue selecting the latest stable version until a final release is published.

## Automatic release sequence

1. Bump `[project].version` for changed package behavior or contents, update the
   changelog, and merge through the existing required CI checks.
2. **Publish to PyPI** runs automatically after successful push CI on `main`.
   Failed CI, pull-request CI and fork CI cannot authorize an upload. If `main`
   advances while CI runs, publication waits for that newer commit's own CI.
3. The publisher checks the exact version on PyPI. Complete, non-yanked versions
   are skipped; missing versions proceed. An incomplete or yanked release fails
   for review rather than being silently treated as complete.
4. The publisher creates annotated `v<version>` and `pypi-v<version>` tags at the
   verified commit. Existing tags are preserved. An existing source tag must match
   the version, be an ancestor of maintained `main`, and have its own successful
   main CI. The annotated publication tag must authorize that exact source commit.
5. The builder checks out the immutable authorized source, verifies both tags,
   version and checkout cleanliness, builds wheel and sdist, runs `twine check`,
   and installs both outside the checkout. Every extra declared by the exact
   wheel is also installed and checked.
6. The `pypi` environment publishes those exact artifacts through OIDC trusted
   publishing. A final job checks PyPI's filenames and SHA-256 hashes against the
   built files and verifies an isolated indexed installation and synthetic demo.
7. Only after public verification succeeds, a separate job creates a GitHub Release
   draft on the existing source tag. It reads this version's changelog from that
   immutable source, uploads the exact wheel/sdist and `SHA256SUMS`, verifies GitHub
   asset hashes, then publishes the release. Candidates remain prereleases and do
   not replace the latest stable release. Interrupted drafts resume without replacing
   files; published releases are accepted on retry only when all assets match.
   Existing draft/published titles and notes must also match the immutable source;
   stale manual text fails for review rather than being silently published.

The workflow runs on `main` and retains the `pypi` environment's main-only deployment
rule. The environment no longer requires a separate manual reviewer for each version:
the owner's standing order supplies publication authorization. Tag protection permits
creation while retaining update, force-update and deletion restrictions. This allows
the workflow's scoped `GITHUB_TOKEN` to create release tags without storing a personal
token. Only the publication job receives `id-token: write`; it executes no source code.
Concurrent publications are serialized and no uploaded version is overwritten.

The GitHub Release job has `contents: write` and executes maintained release tooling,
without an OIDC publication token. The separate **Release failure alerts** workflow
opens one issue per failed main CI/publication run, assigned to the repository
owner, with a direct run link. Only issues authored by GitHub Actions' verified bot
identity are trusted. Failed retry attempts update that issue and notify its assignee.
A successful retry of that same run closes its alert; another version, an older
success, or skipped publication cannot hide an unrecovered release. Closed recovery
markers prevent delayed old failure events from reopening resolved alerts. Alerts
ignore forks, PR CI, and deliberately canceled runs. They include public run metadata,
not workflow logs. Historical CI code failures may be reviewed and closed manually
after their fix ships, without rewriting the failed source commit.
Account-level web/email delivery preferences remain unchanged.

## Manual recovery and TestPyPI

The manual **Publish to PyPI** dispatch remains available on `main`, with an existing
annotated `pypi-v<version>` authorization tag. It runs the same immutable-source and
artifact checks. Use it to retry a failed publication after correcting its cause
only when no PyPI upload has completed. After an upload, use GitHub's **Re-run failed
jobs** on that run to recover verification or GitHub Release publication using the
original exact artifacts; a fresh dispatch must not attempt to upload the same version.
Do not retag an already published version; ship a new version for changed contents.

**Rehearse package on TestPyPI** remains a separate optional manual workflow using
the `testpypi` environment, its reviewer protection, and its own trusted publisher.
TestPyPI is not a dependency of automatic production-index publication.

The PyPI trusted publisher identity is owner `SquirmyWormy275`, repository
`STRATHMARK`, workflow `publish.yml`, environment `pypi`. Retain that identity when
maintaining automation. A successful GitHub release upload alone does not prove a
PyPI upload succeeded: verify the public files and indexed installation.

## Platform truth

V2 is a portable Python library. STRATHMARK 3.0.0rc7 also supplies the separate
[Linux local competition profile](V3_LINUX_COMPETITION.md), using real Formula/ML,
persistent local P-256 keys, explicit approval/issue, settlement and later rounds.
Its authority is the declared local operator policy; it does not claim Windows
CNG qualification. V3 race-day authority under the V7 production contract still
requires the designated Windows installation, non-exportable CNG identities and
qualified three-assessor factory. The wheel retains the Windows optimizer DLL,
so the project makes no blanket OS-independent metadata claim.

Public wheel and source archives exclude operator workbooks and signing keys. The
source archive retains the synthetic Formula golden workbook. Reproducing workbook-
backed V2 release checks requires the separate exact workbook pinned by the existing
acceptance manifest; the released V2 algorithm, model and acceptance hashes are
unchanged. The installed-distribution smoke refuses an archive containing either
historical root workbook.
