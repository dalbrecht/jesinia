// SPDX-License-Identifier: AGPL-3.0-or-later
export { APPS, RUNNER_NAMES, type AppInfo } from "./apps";
export { AppShell, appCommands, statusItems, type AppShellProps, type ShellApp } from "./AppShell";
export { ChatRail, type ChatRailProps } from "./chat/ChatRail";
export {
  EMPTY,
  EVENT_KINDS,
  lineDiff,
  markApproving,
  markAwaiting,
  reduce,
  type AgentEvent,
  type ChatState,
} from "./chat/events";
export { ClientProvider, useClient } from "./context";
export { registerServiceWorker } from "./pwa";
export {
  httpOnly,
  IMG_PROPS,
  isKbAssetUrl,
  LINK_PROPS,
  MAX_LINK_CHARS,
  PAGE_LINK_SCHEME,
  renderMarkdown,
  resolveHttpUrl,
  SafeMarkdown,
  safeHttpUrl,
  type MdNode,
  safeZoteroUrl,
  type PageLinks,
  type SafeMarkdownProps,
} from "./safe-markdown";
export { TagPicker, type TagPickerHandle, type TagPickerProps } from "./TagPicker";
export { useServerStatus, type ServerStatus } from "./status";
export {
  hasHidden,
  revealHidden,
  VisibleText,
  visibleText,
  type HiddenKind,
  type HiddenRun,
  type RevealOptions,
} from "./visible";
export { applyTheme, cycleTheme, readTheme, type ThemeChoice } from "./theme";
export type * from "./types";
export { baseRoutes, fakeClient, HEALTHY, syncStatus, type Routes } from "./testing";
