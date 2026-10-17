-- SPDX-License-Identifier: AGPL-3.0-or-later
-- A session limited to named tools (e.g. a review job: the repo tools only) is offered only
-- those, and the gate refuses every other tool it calls, reads included. NULL: no limit.
ALTER TABLE sessions ADD COLUMN allowed_tools TEXT;
