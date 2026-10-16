// SPDX-License-Identifier: AGPL-3.0-or-later
import s from "./shared.module.css";

/** 16 px, currentColor, 800 ms per turn; static under reduced motion. Decorative. */
export function Spinner() {
  return <span className={s.spinner} aria-hidden="true" data-spinner="" />;
}

export function VisuallyHidden(props: { children: React.ReactNode; id?: string }) {
  return (
    <span className={s.visuallyHidden} id={props.id}>
      {props.children}
    </span>
  );
}
