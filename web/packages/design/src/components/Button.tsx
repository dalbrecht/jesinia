// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  forwardRef,
  type AnchorHTMLAttributes,
  type ButtonHTMLAttributes,
  type ForwardedRef,
  type MouseEvent,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import s from "./Button.module.css";
import { Spinner } from "./Spinner";
import { cx } from "./util";

type ButtonBase = {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  icon?: ReactNode;
  iconTrailing?: ReactNode;
} & ({ children: ReactNode; "aria-label"?: string } | { children?: never; "aria-label": string; icon: ReactNode });

type AsButton = ButtonBase & { href?: undefined; loading?: boolean } & Omit<
    ButtonHTMLAttributes<HTMLButtonElement>,
    "children" | "aria-label"
  >;
type AsLink = ButtonBase & { href: string; loading?: never; disabled?: boolean } & Omit<
    AnchorHTMLAttributes<HTMLAnchorElement>,
    "children" | "href" | "aria-label"
  >;

export type ButtonProps = AsButton | AsLink;

export const Button = forwardRef<HTMLButtonElement | HTMLAnchorElement, ButtonProps>(function Button(props, ref) {
  const { variant = "secondary", size = "md", icon, iconTrailing, children, className } = props;
  const classes = cx(s.button, s[variant], s[size], children === undefined && s.iconOnly, className);
  const lead = icon ? <span className={s.icon}>{icon}</span> : null;
  const trail = iconTrailing ? <span className={s.icon}>{iconTrailing}</span> : null;

  if (props.href !== undefined) {
    const {
      variant: _v,
      size: _s,
      icon: _i,
      iconTrailing: _t,
      children: _c,
      className: _cn,
      href,
      disabled,
      onClick,
      onKeyDown,
      ...rest
    } = props;
    const block = (e: MouseEvent<HTMLAnchorElement> | KeyboardEvent<HTMLAnchorElement>) => e.preventDefault();
    return (
      <a
        {...rest}
        ref={ref as ForwardedRef<HTMLAnchorElement>}
        className={classes}
        {...(disabled
          ? {
              role: "link",
              "aria-disabled": true,
              tabIndex: -1,
              onClick: block,
              onKeyDown: (e: KeyboardEvent<HTMLAnchorElement>) => e.key === "Enter" && block(e),
            }
          : { href, onClick, onKeyDown })}
      >
        {lead}
        {children}
        {trail}
      </a>
    );
  }

  const {
    variant: _v,
    size: _s,
    icon: _i,
    iconTrailing: _t,
    children: _c,
    className: _cn,
    href: _h,
    loading,
    type = "button",
    onClick,
    ...rest
  } = props;
  return (
    <button
      {...rest}
      ref={ref as ForwardedRef<HTMLButtonElement>}
      type={type}
      className={classes}
      aria-busy={loading || undefined}
      aria-disabled={loading || rest["aria-disabled"] || undefined}
      onClick={(e) => {
        if (loading) {
          e.preventDefault();
          return;
        }
        onClick?.(e);
      }}
    >
      {loading ? <Spinner /> : lead}
      {children}
      {trail}
    </button>
  );
});
