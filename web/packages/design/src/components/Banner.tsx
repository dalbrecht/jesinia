// SPDX-License-Identifier: AGPL-3.0-or-later
import { Children, createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import s from "./Banner.module.css";
import { Button } from "./Button";
import { cx } from "./util";

type Action = { label: string; onAction: () => void };
export type BannerProps = {
  tone: "warn" | "danger";
  cause: string;
  children: ReactNode;
  action?: Action;
  secondaryAction?: Action;
};

/** True once the region has rendered its first frame: later banners are announced. */
const Mounted = createContext<{ current: boolean } | null>(null);

export function Banner({ tone, cause, children, action, secondaryAction }: BannerProps) {
  const mounted = useContext(Mounted);
  const [announce] = useState(() => mounted?.current === true);
  const role = announce ? (tone === "danger" ? "alert" : "status") : undefined;
  return (
    <div className={cx(s.banner, s[tone])} role={role} data-tone={tone}>
      <span className={s.icon} aria-hidden="true">
        {tone === "danger" ? "×" : "!"}
      </span>
      <span className={s.detail}>
        <span className={s.cause}>{cause}</span> {children}
      </span>
      {action || secondaryAction ? (
        <span className={s.actions}>
          {action ? (
            <Button size="sm" onClick={action.onAction}>
              {action.label}
            </Button>
          ) : null}
          {secondaryAction ? (
            <Button variant="ghost" size="sm" onClick={secondaryAction.onAction}>
              {secondaryAction.label}
            </Button>
          ) : null}
        </span>
      ) : null}
    </div>
  );
}

/** The "System status" landmark: banners most severe first, at most two plus "+n more". */
export function BannerRegion({ children, onMore }: { children: ReactNode; onMore?: () => void }) {
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
  }, []);
  const banners = Children.toArray(children);
  const shown = banners.slice(0, 2);
  const hidden = banners.length - shown.length;
  return (
    <Mounted.Provider value={mounted}>
      <div role="region" aria-label="System status" className={s.region}>
        {shown}
        {hidden > 0 ? (
          <div className={s.more}>
            <Button variant="ghost" size="sm" onClick={onMore}>
              {`+${hidden} more`}
            </Button>
          </div>
        ) : null}
      </div>
    </Mounted.Provider>
  );
}
