// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, type Ref } from "react";

/** One callback ref that feeds dnd-kit's node ref and the caller's forwarded ref. */
export function useMergedRef<T>(
  own: (node: T | null) => void,
  forwarded: Ref<T> | undefined,
): (node: T | null) => void {
  return useCallback(
    (node: T | null) => {
      own(node);
      if (typeof forwarded === "function") forwarded(node);
      else if (forwarded) forwarded.current = node;
    },
    [own, forwarded],
  );
}
