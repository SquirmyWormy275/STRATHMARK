# Wiki source

This directory holds the canonical source for the STRATHMARK wiki.
GitHub wikis live in a separate git repository named
`<repo>.wiki.git`, but keeping the source here in the main tree has
three benefits:

1. Wiki edits flow through the same PR review as code edits.
2. The wiki is searchable from the same IDE session as the code.
3. Releases can pin the wiki version to the code version.

## Layout

```
docs/wiki/
    Home.md                        landing page
    Installation.md
    Quick-Start.md
    Architecture-Overview.md
    Prediction-Cascade.md
    Handicap-Mark-Math.md
    Rulebook-Comparison.md
    Wood-and-Diameter-Scaling.md
    Time-Decay-Weighting.md
    Variance-and-Monte-Carlo.md
    Fairness-Assessment.md
    Persistence-and-Database.md
    LLM-Integration.md
    REST-API.md
    Deployment.md
    Testing.md
    FAQ.md
    _Sidebar.md                    left-nav panel
    _Footer.md                     every-page footer
    README.md                      this file
```

## Publishing to the GitHub wiki

One-time setup (clone the wiki sibling repo outside the main tree):

```bash
git clone https://github.com/SquirmyWormy275/STRATHMARK.wiki.git
```

Each release:

```bash
cp docs/wiki/*.md ../STRATHMARK.wiki/
cd ../STRATHMARK.wiki
git add .
git commit -m "docs(wiki): sync from main repo at v0.4.0"
git push
```

An optional `.github/workflows/sync-wiki.yml` can automate the copy
on every push to `main`; keep the sync one-way (main → wiki) so the
source of truth stays in this directory.

## Editing conventions

- Plain Markdown, CommonMark-flavoured. No HTML, no wiki-specific
  extensions.
- Wrap prose at 72 columns where reasonable. Tables and fenced code
  blocks may exceed 72.
- Cite code paths as `strathmark/<module>.py` (no line numbers — the
  line numbers drift).
- Cite rulebook rules with the body and rule number: *"AAA Rule 18:
  ..."* so readers can look them up in the appropriate PDF.
- Every invariant claim must link back to the place in the code that
  enforces it, or to the wiki page that documents it.
- Preserve the 70-character plain-text output convention when
  rendering example start sheets.
