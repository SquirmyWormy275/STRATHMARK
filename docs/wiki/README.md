# Maintaining the wiki

This directory is the versioned source for the separate STRATHMARK GitHub wiki.
Edit pages here, check links, and merge the documentation before publishing.

```bash
python scripts/check_docs.py
python scripts/publish_wiki.py --mode preview
python scripts/publish_wiki.py --mode publish
python scripts/publish_wiki.py --mode check
```

Publish from the exact clean merged `main` commit. The publisher copies maintained
pages, pushes the wiki, fetches it again and verifies every remote page. It preserves
unrelated wiki files. A repository merge alone does not update the wiki.

Keep V2, Linux V3, Windows V7 rehearsal and separate preview behavior clear. Label
historical behavior by version. Publishing documentation does not deploy code or
promote a model. This file is a maintainer guide and is not published as a wiki page.
