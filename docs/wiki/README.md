# Wiki source

The [Linux numeric candidate](https://github.com/SquirmyWormy275/STRATHMARK/blob/main/docs/V3_LINUX_CANDIDATE.md) separately runs real Formula/ML numeric previews through STRATHEX 7.2. It uses a subprocess contract, without V7 approval, issue, settlement, next-round learning, or production authority. The full authenticated V7 lifecycle requires operational composition and installation qualification.

This directory is the canonical source for the STRATHMARK GitHub wiki. Publish it only
after the matching repository documentation is merged and separately authorized.

[Handicap foundations](Handicap-Mark-Math.md) is mandatory timeless domain reading.
[Prediction Engine V3](Prediction-Engine-V3.md) routes to the successor release candidate.
[Prediction Engine V2](Prediction-Engine-V2.md) preserves the globally trusted production
contract. V3.0.0rc3 is a release candidate that tracks all
232 in-repository requirements. Core implementation and contract tests exist; installed lifecycle composition and qualification remain unfinished. Its development-key rehearsal is source-bound; no production authority
has changed and V3 is not production-eligible.

Pages that retain V2 formulas or compatibility behavior must label them as V2-specific.
Pages that describe V3 must state the rehearsal/production boundary. Wiki publication
does not authorize a code release, deployment, model promotion, database migration, or
consumer switch.

Use `python scripts/publish_wiki.py --mode preview` to review changes, then `--mode publish` from the exact clean merged main commit. Publication fetches and verifies the remote pages; `--mode check` verifies synchronization without writing.
