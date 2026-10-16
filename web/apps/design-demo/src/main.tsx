// SPDX-License-Identifier: AGPL-3.0-or-later
import "@jesinia/design/fonts.css";
import "@jesinia/design/reset.css";
import "@jesinia/design/tokens.css";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Demo } from "./Demo";
import "./demo.css";

createRoot(document.getElementById("root") as HTMLElement).render(
  <StrictMode>
    <Demo />
  </StrictMode>,
);
