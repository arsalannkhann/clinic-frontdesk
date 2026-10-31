"use client";

import { X } from "lucide-react";
import { useEffect, useId, useRef, type ReactNode } from "react";

import { cx } from "@/lib/cx";

/**
 * Dialog and Drawer on the native <dialog> element: showModal() makes the rest of the page
 * inert, traps focus, closes on Escape and returns focus to the opener, so keyboard and
 * screen-reader behaviour comes from the platform rather than a hand-rolled trap.
 */

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  variant: "dialog" | "drawer";
  className?: string;
}

function Modal({ open, onClose, title, description, children, footer, variant, className }: ModalProps) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } else if (!open && dialog.open) {
      if (typeof dialog.close === "function") dialog.close();
      else dialog.removeAttribute("open");
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        // A click on the backdrop lands on the <dialog> itself.
        if (event.target === ref.current) onClose();
      }}
      className={cx(
        "bg-raised p-0 text-ink shadow-float backdrop:animate-fade open:flex flex-col",
        variant === "dialog"
          ? "m-auto max-h-[min(90vh,52rem)] w-[min(100vw-2rem,34rem)] rounded-lg border border-line open:animate-rise"
          : "my-0 mr-0 ml-auto h-dvh max-h-dvh w-[min(100vw,30rem)] border-l border-line open:animate-slide-in",
        className,
      )}
    >
      <header className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
        <div className="min-w-0">
          <h2 id={titleId} className="font-display text-xl leading-tight text-ink">
            {title}
          </h2>
          {description ? (
            <p id={descriptionId} className="mt-1 text-sm text-ink-2">
              {description}
            </p>
          ) : null}
        </div>
        <button
          type="button"
          onClick={onClose}
          className="-mr-1.5 rounded-md p-1.5 text-ink-2 hover:bg-sunk hover:text-ink"
          aria-label="Close"
        >
          <X className="size-5" aria-hidden="true" />
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{open ? children : null}</div>
      {footer && open ? (
        <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-line bg-paper/60 px-5 py-3">
          {footer}
        </footer>
      ) : null}
    </dialog>
  );
}

export function Dialog(props: Omit<ModalProps, "variant">) {
  return <Modal {...props} variant="dialog" />;
}

export function Drawer(props: Omit<ModalProps, "variant">) {
  return <Modal {...props} variant="drawer" />;
}
