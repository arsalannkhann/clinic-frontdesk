"use client";

import type { ReactNode } from "react";

import { buttonClass, type ButtonSize, type ButtonVariant } from "@/components/ui/Button";
import { openChat } from "./chat/ChatWidget";

export function OpenChatButton({
  children,
  variant = "secondary",
  size = "lg",
  className,
}: {
  children: ReactNode;
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
}) {
  return (
    <button type="button" onClick={() => openChat()} className={buttonClass(variant, size, className)}>
      {children}
    </button>
  );
}
