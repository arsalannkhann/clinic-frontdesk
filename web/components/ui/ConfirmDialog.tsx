"use client";

import { useCallback, useRef, useState, type ReactNode } from "react";

import { Button } from "./Button";
import { Dialog } from "./Modal";

/**
 * A confirmation step for actions that can't be taken back (placing a call, cancelling an
 * appointment). It is a native modal <dialog> with role="alertdialog": the page behind is
 * inert, Escape or the backdrop means "no", focus starts on the safe choice and returns to
 * the button that asked.
 */

export interface ConfirmOptions {
  title: ReactNode;
  body: ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  tone?: "danger" | "primary";
}

export function ConfirmDialog({
  open,
  options,
  onAnswer,
}: {
  open: boolean;
  options: ConfirmOptions | null;
  onAnswer: (yes: boolean) => void;
}) {
  return (
    <Dialog
      open={open}
      onClose={() => onAnswer(false)}
      role="alertdialog"
      title={options?.title ?? ""}
      description={options?.body}
      footer={
        options ? (
          <>
            <Button variant="ghost" data-autofocus onClick={() => onAnswer(false)}>
              {options.cancelLabel ?? "Go back"}
            </Button>
            <Button variant={options.tone === "primary" ? "primary" : "danger"} onClick={() => onAnswer(true)}>
              {options.confirmLabel}
            </Button>
          </>
        ) : null
      }
    >
      {null}
    </Dialog>
  );
}

/** `const [confirm, dialog] = useConfirm()`; render `dialog`, then `if (await confirm({...}))`. */
export function useConfirm(): [(options: ConfirmOptions) => Promise<boolean>, ReactNode] {
  const [options, setOptions] = useState<ConfirmOptions | null>(null);
  const resolver = useRef<((yes: boolean) => void) | null>(null);

  const confirm = useCallback((next: ConfirmOptions) => {
    resolver.current?.(false);
    setOptions(next);
    return new Promise<boolean>((resolve) => {
      resolver.current = resolve;
    });
  }, []);

  const answer = useCallback((yes: boolean) => {
    resolver.current?.(yes);
    resolver.current = null;
    setOptions(null);
  }, []);

  return [confirm, <ConfirmDialog key="confirm" open={options !== null} options={options} onAnswer={answer} />];
}
