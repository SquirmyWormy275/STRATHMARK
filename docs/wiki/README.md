# Wiki source

This directory is the canonical source for the STRATHMARK GitHub wiki. Wiki changes
must be reviewed with the matching code release and copied one-way to
`STRATHMARK.wiki.git`.

`Prediction-Engine-V2.md` is the primary model page. `Prediction-Cascade.md` is retained
only because external links may still use that name; it now documents the compatibility
key mapping rather than the retired numeric cascade.

Publish after the main-tree documentation is merged:

```bash
git clone https://github.com/SquirmyWormy275/STRATHMARK.wiki.git
cp docs/wiki/*.md ../STRATHMARK.wiki/
cd ../STRATHMARK.wiki
git add .
git commit -m "docs(wiki): sync Prediction Engine V2"
git push
```

Keep the source one-way (main repository to wiki), use plain Markdown, link model claims
to code or checked-in evidence, and never publish a live-schema or benchmark claim that
has not been independently verified.
