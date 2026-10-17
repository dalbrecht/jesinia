// SPDX-License-Identifier: AGPL-3.0-or-later
export { APPS, RUNNER_NAMES, type AppInfo } from "./apps";
export { AppShell, appCommands, statusItems, type AppShellProps, type ShellApp } from "./AppShell";
export { ChatRail, type ChatRailProps } from "./chat/ChatRail";
export { EMPTY, EVENT_KINDS, lineDiff, markApproving, reduce, type AgentEvent, type ChatState } from "./chat/events";
export { ClientProvider, useClient } from "./context";
export { registerServiceWorker } from "./pwa";
export {
  httpOnly,
  IMG_PROPS,
  LINK_PROPS,
  renderMarkdown,
  resolveHttpUrl,
  SafeMarkdown,
  safeHttpUrl,
} from "./safe-markdown";
export { TagPicker, type TagPickerProps } from "./TagPicker";
export { useServerStatus, type ServerStatus } from "./status";
export { applyTheme, cycleTheme, readTheme, type ThemeChoice } from "./theme";
export type * from "./types";
export { baseRoutes, fakeClient, HEALTHY, syncStatus, type Routes } from "./testing";
