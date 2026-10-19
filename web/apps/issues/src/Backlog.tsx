// SPDX-License-Identifier: AGPL-3.0-or-later
// The Backlog (spec §3.2): existing issues, re-triaged in batches through saved filters.
import { IssueList, type IssueListProps } from "./IssueList";

export function Backlog(props: Omit<IssueListProps, "view">) {
  return <IssueList view="backlog" {...props} />;
}
