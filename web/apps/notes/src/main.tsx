// SPDX-License-Identifier: AGPL-3.0-or-later
import "@jesinia/design/fonts.css";
import "@jesinia/design/reset.css";
import "@jesinia/design/tokens.css";
import { registerServiceWorker } from "@jesinia/shell";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
void registerServiceWorker("/notes/");
