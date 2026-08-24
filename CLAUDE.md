# casino-skeleton — Project Instructions

## Git permissions (overrides global read-only git rule)

Brian has explicitly authorized git **write** operations in this repository:
`commit`, `add`, `push`, `branch`, `checkout`, `tag`, `stash`, `merge`, `rebase`.

- Applies to `/Users/brian/Development/casino-skeleton` only. The global
  read-only-git rule in `~/.claude/CLAUDE.md` still governs every other project.
- Still confirm before destructive/history-rewriting operations:
  `push --force`, `reset --hard`, `branch -D`, `filter-branch`, rewriting pushed history.
- Remote: `git@github.com:paradigmbrian/casino-skeleton.git`

## Context

This repo is the Agent Engineer take-home (see `docs/Agent Engineer Take-Home.docx.pdf`).
The deliverable is an autonomous agent layer that maintains this repo — the agents
themselves commit to this repository, so their commits must appear in the history
alongside the template's original `Initial commit`.
