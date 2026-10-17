-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Resumable rounds (ruling R51): a round a stop or crash cut short is resumed on restart,
-- not replaced, so its finished jobs and the author's drafts stay with it. `interruptions`
-- counts the times this round was cut short (a crash or a stop; a routine head move or a
-- transient GitHub failure doesn't count); its head fails after 3.
ALTER TABLE rounds ADD COLUMN interruptions INTEGER NOT NULL DEFAULT 0;
UPDATE rounds SET interruptions = interrupted;
