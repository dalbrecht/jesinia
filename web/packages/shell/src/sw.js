// SPDX-License-Identifier: AGPL-3.0-or-later
// Minimal service worker: makes each app installable as a PWA. It caches nothing, so
// data is always live from the local server.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
