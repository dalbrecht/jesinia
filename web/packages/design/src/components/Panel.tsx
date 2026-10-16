// SPDX-License-Identifier: AGPL-3.0-or-later
import { forwardRef, useId, type HTMLAttributes, type ReactNode } from "react";
import s from "./Panel.module.css";
import { cx } from "./util";

export type PanelProps = {
  variant?: "default" | "raised" | "flush";
  title?: ReactNode;
  headingLevel?: 2 | 3 | 4;
  actions?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
} & Omit<HTMLAttributes<HTMLElement>, "title">;

export const Panel = forwardRef<HTMLElement, PanelProps>(function Panel(
  { variant = "default", title, headingLevel = 2, actions, footer, children, className, ...rest },
  ref,
) {
  const titleId = useId();
  const Heading = `h${headingLevel}` as const;
  const classes = cx(s.panel, variant !== "default" && s[variant], className);
  const header =
    title !== undefined || actions !== undefined ? (
      <div className={s.header}>
        {title !== undefined ? (
          <Heading id={titleId} className={s.title}>
            {title}
          </Heading>
        ) : (
          <span />
        )}
        {actions !== undefined ? <div className={s.actions}>{actions}</div> : null}
      </div>
    ) : null;
  const content = (
    <>
      {header}
      <div className={s.body}>{children}</div>
      {footer !== undefined ? <div className={s.footer}>{footer}</div> : null}
    </>
  );
  if (title !== undefined) {
    return (
      <section {...rest} ref={ref} className={classes} aria-labelledby={titleId}>
        {content}
      </section>
    );
  }
  return (
    <div {...rest} ref={ref as React.Ref<HTMLDivElement>} className={classes}>
      {content}
    </div>
  );
});
