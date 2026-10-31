"use client";

import {
  forwardRef,
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import { cx } from "@/lib/cx";

/**
 * Form controls. Every control has a visible label, and its hint and error are wired with
 * aria-describedby; an error also sets aria-invalid.
 */

const control =
  "w-full rounded-md border border-line-strong bg-raised px-3 text-ink placeholder:text-ink-3 " +
  "transition-colors hover:border-ink-3 focus:border-accent focus:outline-none focus-visible:outline-2 " +
  "focus-visible:outline-accent focus-visible:outline-offset-1 disabled:bg-sunk disabled:text-ink-3 " +
  "aria-[invalid=true]:border-bad";

export interface FieldProps {
  label: ReactNode;
  hint?: ReactNode;
  error?: string | null;
  optional?: boolean;
  className?: string;
  children: (props: { id: string; "aria-describedby"?: string; "aria-invalid"?: true }) => ReactNode;
}

export function Field({ label, hint, error, optional, className, children }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  const describedBy = [hintId, errorId].filter(Boolean).join(" ") || undefined;
  return (
    <div className={cx("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-sm font-semibold text-ink">
        {label}
        {optional ? <span className="ml-1.5 font-normal text-ink-3">optional</span> : null}
      </label>
      {children({ id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {hint ? (
        <p id={hintId} className="text-sm text-ink-3">
          {hint}
        </p>
      ) : null}
      {error ? (
        <p id={errorId} className="text-sm font-medium text-bad">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input(
  { className, ...rest },
  ref,
) {
  return <input ref={ref} className={cx(control, "h-10", className)} {...rest} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...rest },
  ref,
) {
  return <textarea ref={ref} className={cx(control, "min-h-24 py-2 leading-relaxed", className)} {...rest} />;
});

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(function Select(
  { className, children, ...rest },
  ref,
) {
  return (
    <select
      ref={ref}
      className={cx(
        control,
        "h-10 appearance-none bg-[length:12px] bg-[right_0.75rem_center] bg-no-repeat pr-9",
        "bg-[url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 12 12'%3E%3Cpath d='M2 4l4 4 4-4' fill='none' stroke='%2357524a' stroke-width='1.5'/%3E%3C/svg%3E\")]",
        className,
      )}
      {...rest}
    >
      {children}
    </select>
  );
});

export interface PhoneInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "onChange" | "value"> {
  value: string;
  onValueChange: (digits: string) => void;
}

/** A fixed +91 prefix and a 10-digit national number. Pasting "+91 98765 43210" works. */
export const PhoneInput = forwardRef<HTMLInputElement, PhoneInputProps>(function PhoneInput(
  { value, onValueChange, className, ...rest },
  ref,
) {
  return (
    <div className={cx("flex h-10 w-full", className)}>
      <span
        aria-hidden="true"
        className="inline-flex items-center rounded-l-md border border-r-0 border-line-strong bg-sunk px-3 text-ink-2 tnum"
      >
        +91
      </span>
      <input
        ref={ref}
        type="tel"
        inputMode="numeric"
        autoComplete="tel-national"
        className={cx(control, "h-10 rounded-l-none tnum")}
        value={value}
        onChange={(event) => {
          let digits = event.target.value.replace(/\D/g, "");
          if (digits.length > 10 && digits.startsWith("91")) digits = digits.slice(2);
          if (digits.length > 10 && digits.startsWith("0")) digits = digits.slice(1);
          onValueChange(digits.slice(0, 10));
        }}
        {...rest}
      />
    </div>
  );
});

export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: ReactNode;
  description?: ReactNode;
}

export function Checkbox({ label, description, className, id, ...rest }: CheckboxProps) {
  const generated = useId();
  const inputId = id ?? generated;
  const descriptionId = description ? `${inputId}-desc` : undefined;
  return (
    <div className={cx("flex items-start gap-3", className)}>
      <input
        id={inputId}
        type="checkbox"
        aria-describedby={descriptionId}
        className="mt-0.5 size-[18px] shrink-0 cursor-pointer accent-[var(--color-accent)]"
        {...rest}
      />
      <div className="flex flex-col gap-0.5">
        <label htmlFor={inputId} className="cursor-pointer text-[0.95rem] leading-snug text-ink">
          {label}
        </label>
        {description ? (
          <p id={descriptionId} className="text-sm text-ink-3">
            {description}
          </p>
        ) : null}
      </div>
    </div>
  );
}
