// SPDX-License-Identifier: AGPL-3.0-or-later
/** Register the app's service worker (served next to its index.html as `sw.js`). */
export async function registerServiceWorker(scope: string): Promise<boolean> {
  if (!("serviceWorker" in navigator)) return false;
  try {
    await navigator.serviceWorker.register(`${scope}sw.js`, { scope });
    return true;
  } catch {
    return false;
  }
}
