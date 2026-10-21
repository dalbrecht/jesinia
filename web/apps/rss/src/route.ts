// SPDX-License-Identifier: AGPL-3.0-or-later
// The feed management screen has its own address, /rss/manage (served by the static fallback),
// so a reload stays on it and the browser's Back and Forward move between it and the reader.
import { useCallback, useEffect, useState } from "react";

/** The app's base path ("/rss/" when built, "/" under test), always ending in a slash. */
const BASE = import.meta.env.BASE_URL.endsWith("/") ? import.meta.env.BASE_URL : `${import.meta.env.BASE_URL}/`;

const atManage = () => /\/manage\/?$/.test(window.location.pathname);

export function useManageRoute(): { managing: boolean; open(): void; close(): void } {
  const [managing, setManaging] = useState(atManage);
  useEffect(() => {
    const sync = () => setManaging(atManage());
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, []);
  const open = useCallback(() => {
    if (!atManage()) window.history.pushState(null, "", `${BASE}manage`);
    setManaging(true);
  }, []);
  const close = useCallback(() => {
    if (atManage()) window.history.pushState(null, "", BASE);
    setManaging(false);
  }, []);
  return { managing, open, close };
}
