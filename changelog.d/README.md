# Changelog fragments

A pull request with a notable change adds one file here instead of editing
`CHANGELOG.md`'s `[Unreleased]` section, so parallel pull requests do not
conflict on it. `scripts/changelog_fragments.py` validates the files (the test
suite runs it) and assembles them into `CHANGELOG.md`. This README is the one
file it ignores.

**Name:** `<slug>.<category>.md`. The slug is lowercase letters, digits and
hyphens: use your branch's topic, and add `-2`, `-3` for a second or third
entry. The category is a [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
one: `added`, `changed`, `deprecated`, `removed`, `fixed` or `security`.

**Text:** the entry exactly as it will read in `CHANGELOG.md`, one bullet in the
house style. Every line after the first, and every further paragraph, is
indented by two spaces:

```markdown
- **A bold sentence that says what changed.** Then the detail: why it matters,
  what a reader or operator does differently, and what it does not do.

  A second paragraph, if the entry needs one.
```

**Assembly:** `scripts/changelog_fragments.py assemble --dry-run` prints the
change; without `--dry-run` it writes each fragment at the top of its category
under `[Unreleased]` (a new heading when there is none yet) and deletes the
assembled files. It never rewrites or reorders the existing text.
