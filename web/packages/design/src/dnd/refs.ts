// SPDX-License-Identifier: AGPL-3.0-or-later
import { useCallback, type Ref } from "react";

/** One callback ref that feeds dnd-kit's node refs and the caller's forwarded ref. */
export function useMergedRef<T>(
  forwarded: Ref<T> | undefined,
  ...own: ((node: T | null) => void)[]
): (node: T | null) => void {
  return useCallback(
    (node: T | null) => {
      for (const set of own) set(node);
      if (typeof forwarded === "function") forwarded(node);
      else if (forwarded) forwarded.current = node;
    },
    [forwarded, ...own],
  );
}
