"use client";

import type { ReactNode } from "react";

import { buttonClass, type ButtonSize, type ButtonVariant } from "@/components/ui/Button";
import { openChat, type ChatOpenMode } from "./chat/ChatWidget";

/** Opens the chat panel; `mode="call"` opens the voice call screen instead. */
export function OpenChatButton({
  children,
  variant = "secondary",
  size = "lg",
  mode = "text",
  className,
}: {
  children: ReactNode;
  variant?: ButtonVariant;
  size?: ButtonSize;
  mode?: ChatOpenMode;
  className?: string;
}) {
  return (
    <button type="button" onClick={() => openChat(undefined, { mode })} className={buttonClass(variant, size, className)}>
      {children}
    </button>
  );
}
