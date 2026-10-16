// SPDX-License-Identifier: AGPL-3.0-or-later
import js from "@eslint/js";
import security from "eslint-plugin-security";
import globals from "globals";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["**/dist/**", "**/node_modules/**", "**/coverage/**", "packages/client/src/openapi.d.ts"] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  security.configs.recommended,
  {
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
  },
  {
    files: ["**/*.ts", "**/*.tsx"],
    rules: {
      // Omitting props by destructuring into `_name` (or a rest sibling) is deliberate.
      "@typescript-eslint/no-unused-vars": [
        "error",
        { ignoreRestSiblings: true, varsIgnorePattern: "^_", argsIgnorePattern: "^_" },
      ],
      // Flags every `obj[key]`; in TypeScript the key types (unions, typed records) already
      // bound these lookups, so the rule is noise here. Still on for plain JS.
      "security/detect-object-injection": "off",
    },
  },
  {
    // Tests read fixture files; dev scripts (icon export, client generation) take the
    // paths they read and write from their command line.
    files: ["**/__tests__/**", "tests/**", "**/scripts/**"],
    rules: { "security/detect-non-literal-fs-filename": "off" },
  },
);
