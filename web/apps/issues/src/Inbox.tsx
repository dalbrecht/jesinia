// SPDX-License-Identifier: AGPL-3.0-or-later
// The Inbox (spec §3.1): issues opened after their repo was added, and any that became incomplete.
import { IssueList, type IssueListProps } from "./IssueList";

export function Inbox(props: Omit<IssueListProps, "view">) {
  return <IssueList view="inbox" {...props} />;
}
