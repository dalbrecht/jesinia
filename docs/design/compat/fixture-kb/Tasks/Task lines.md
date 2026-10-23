---
id: 01a16990-1ff4-71a5-b681-d31cf764376e
title: Task lines
---
This page tests how the Pigeonpile Wiki and Dopo Tasks handle task lines (ADR 0013). #compat

## Basic

- [ ] Ship feed management #jesinia priority:a scheduled:2026-10-24 due:2026-10-31
- [x] Bulk mark-read closed:2026-10-22
- [ ] Call the plumber due:2026-10-27T09:30

## States

- [ ] Draft the agenda state:next
- [ ] Renew passport state:waiting due:2026-12-01
- [x] Old idea state:cancelled closed:2026-10-20

## Repeaters

- [ ] Water the garden every:1w scheduled:2026-10-25
- [ ] Change the filter after:3m due:2026-11-01

## Nesting and other markers

- [ ] Plan the trip
  - [ ] Book flights due:2026-11-15
  - [x] Pick dates closed:2026-10-21
* [ ] Star bullet task
+ [ ] Plus bullet task
1. [ ] Numbered task
- [X] Uppercase done mark closed:2026-10-19

## Not fields (title text)

- [ ] Ask about due:tomorrow and priority:z
- [ ] Explain the `due:2026-01-01` syntax
- [ ] Duplicate due due:2026-10-24 due:2026-10-31
- [ ] Both repeaters every:1w after:2w scheduled:2026-10-26

```markdown
- [ ] Inside a code block, not a task due:2026-10-31
```

Toggle the first unchecked task, then edit this word to force a re-save: CHANGEME
