---
id: 01a16990-1ff4-71a5-b681-d31cf764376e
title: Task lines
---
This page tests how the Pigeonpile Wiki and Dopo Tasks handle task lines in Dopo's dialect (ADR 0013). #compat

## Core tokens

- [ ] Inbox by default
- [ ] Ship feed management #jesinia @status:next due:2026-10-31 defer:2026-10-24
- [ ] Draft the agenda @status:in-progress @context:computer @energy:high
- [ ] Renew passport @status:waiting @waiting:"photo appointment" due:2026-12-01
- [ ] Learn the cello @status:someday
- [x] Bulk mark-read
- [x] Old idea @status:trash

## Proposed tokens

- [ ] Pick the feed reader @priority:1 @today:2026-10-23
- [x] Order seeds completed:2026-10-22
- [ ] Water the garden @every:1w due:2026-10-25
- [ ] Change the filter @after:3m due:2026-11-01
- [ ] Pay rent @every:1m due:2026-01-31
- [ ] Gym @every:1w @repeat-on:mon,thu @repeat-until:2026-12-31 due:2026-10-26
- [ ] Course homework @every:1w @repeat-count:5 due:2026-10-27
- [ ] Reopened task completed:2026-10-18

## Nesting and markers

- [ ] Plan the trip @kind:project
  - [ ] Book flights due:2026-11-15
  - [x] Pick dates
* [ ] Star bullet task
+ [ ] Plus bullet task
- [X] Uppercase done mark
1. [ ] Ordered item, not a task in the dialect

## Title text, not tokens

- [ ] Tag after a token ends the run @context:errands #home
- [ ] Bad values @status:frozen due:2026-13-40 @every:0w
- [ ] Duplicate due due:2026-10-24 due:2026-10-31
- [ ] Explain the `due:2026-01-01` syntax

```markdown
- [ ] Inside a code block, not a task due:2026-10-31
```

Toggle the first unchecked task, then edit this word to force a re-save: CHANGEME
