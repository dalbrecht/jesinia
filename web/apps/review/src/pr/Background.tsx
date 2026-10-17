// SPDX-License-Identifier: AGPL-3.0-or-later
// Background (spec §3.1): how the parts of the system the PR touches work today, with the code
// areas involved and the KB pages that explain the domain. Agent output: safe Markdown only.
import { SafeMarkdown } from "@jesinia/shell";
import type { BackgroundSection } from "./model";
import s from "./pr.module.css";

export function Background({ sections }: { sections: BackgroundSection[] }) {
  return (
    <article className={s.page} aria-label="Background">
      <h2>Background</h2>
      {sections.length === 0 ? <p className={s.muted}>Nothing to explain: the PR touches nothing new to you.</p> : null}
      {sections.map((section, i) => (
        <section key={i} className={s.group}>
          <h3>{section.title}</h3>
          <div className={s.prose}>
            <SafeMarkdown markdown={section.body} />
          </div>
          {section.code.length ? (
            <ul className={s.refs} aria-label="Code areas">
              {section.code.map((c, j) => (
                <li key={j}>
                  <code>{c.path}</code>
                </li>
              ))}
            </ul>
          ) : null}
          {section.kb.length ? (
            <ul className={s.refs} aria-label="KB pages">
              {section.kb.map((page, j) => (
                <li key={j}>{page}</li>
              ))}
            </ul>
          ) : null}
        </section>
      ))}
    </article>
  );
}
