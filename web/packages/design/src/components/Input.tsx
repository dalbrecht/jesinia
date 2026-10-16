// SPDX-License-Identifier: AGPL-3.0-or-later
import {
  forwardRef,
  useId,
  useImperativeHandle,
  useRef,
  useState,
  type ChangeEvent,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import s from "./Input.module.css";
import { Spinner, VisuallyHidden } from "./Spinner";
import { cx } from "./util";

type InputCommon = {
  label: string;
  hideLabel?: boolean;
  help?: ReactNode;
  error?: ReactNode;
  loading?: boolean;
  leading?: ReactNode;
  trailing?: ReactNode;
};

type SingleLine = InputCommon & { multiline?: false; variant?: "text" | "search" | "path" } & Omit<
    InputHTMLAttributes<HTMLInputElement>,
    "size"
  >;
type MultiLine = InputCommon & { multiline: true; onSubmit?: (value: string) => void } & Omit<
    TextareaHTMLAttributes<HTMLTextAreaElement>,
    "onSubmit"
  >;
export type InputProps = SingleLine | MultiLine;

const MAX_ROWS = 8;

function setNativeValue(el: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

export const Input = forwardRef<HTMLInputElement | HTMLTextAreaElement, InputProps>(function Input(props, ref) {
  const id = useId();
  const fieldId = props.id ?? `${id}-field`;
  const noteId = `${id}-note`;
  const invalid = props.error !== undefined && props.error !== null && props.error !== false;
  const note = invalid ? props.error : props.help;
  const labelText = props.required ? `${props.label} (required)` : props.label;
  const variant = props.multiline ? "text" : (props.variant ?? "text");
  const inner = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  useImperativeHandle(ref, () => inner.current as HTMLInputElement & HTMLTextAreaElement);
  const initial = String(props.value ?? props.defaultValue ?? "");
  const [text, setText] = useState(initial);
  const current = props.value !== undefined ? String(props.value) : text;

  const common = {
    id: fieldId,
    className: s.input,
    "aria-invalid": invalid || undefined,
    "aria-describedby": note ? noteId : undefined,
    "aria-busy": props.loading || undefined,
  };

  let field: ReactNode;
  if (props.multiline) {
    const { label: _l, hideLabel: _h, help: _he, error: _e, loading: _lo, leading: _le, trailing: _t, multiline: _m,
      onSubmit, onKeyDown, onChange, ...rest } = props; // prettier-ignore
    const rows = Math.min(MAX_ROWS, Math.max(1, current.split("\n").length));
    field = (
      <textarea
        {...rest}
        {...common}
        ref={inner}
        rows={rows}
        onChange={(e: ChangeEvent<HTMLTextAreaElement>) => {
          setText(e.target.value);
          onChange?.(e);
        }}
        onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
          onKeyDown?.(e);
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && onSubmit) {
            e.preventDefault();
            onSubmit(e.currentTarget.value);
          }
        }}
      />
    );
  } else {
    const { label: _l, hideLabel: _h, help: _he, error: _e, loading: _lo, leading: _le, trailing: _t, multiline: _m,
      variant: _v, onKeyDown, onChange, ...rest } = props; // prettier-ignore
    field = (
      <input
        {...rest}
        {...common}
        ref={inner}
        type={variant === "search" ? "search" : (rest.type ?? "text")}
        spellCheck={variant === "path" ? false : rest.spellCheck}
        onChange={(e: ChangeEvent<HTMLInputElement>) => {
          setText(e.target.value);
          onChange?.(e);
        }}
        onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
          onKeyDown?.(e);
          if (variant === "search" && e.key === "Escape" && e.currentTarget.value) {
            e.stopPropagation();
            setNativeValue(e.currentTarget, "");
          }
        }}
      />
    );
  }

  const label = (
    <label htmlFor={fieldId} className={s.label}>
      {labelText}
    </label>
  );
  return (
    <div className={s.field}>
      {props.hideLabel ? <VisuallyHidden>{label}</VisuallyHidden> : label}
      <div
        className={cx(s.control, s[variant], invalid && s.invalid, props.disabled && s.disabled)}
        data-variant={variant}
      >
        {props.leading ??
          (variant === "search" ? (
            <span className={s.adorn} aria-hidden="true">
              ⌕
            </span>
          ) : null)}
        {field}
        {variant === "search" && current ? (
          <button
            type="button"
            className={s.clear}
            aria-label="Clear search"
            onClick={() => {
              if (inner.current) setNativeValue(inner.current, "");
              inner.current?.focus();
            }}
          >
            ×
          </button>
        ) : null}
        {props.loading ? (
          <span className={s.adorn}>
            <Spinner />
          </span>
        ) : null}
        {props.trailing}
      </div>
      {note ? (
        <div id={noteId} className={invalid ? s.error : s.help}>
          {invalid ? <span aria-hidden="true">× </span> : null}
          {note}
        </div>
      ) : null}
    </div>
  );
});
