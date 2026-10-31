import { Phone } from "lucide-react";

import type { ClinicProfile } from "@/lib/api/types";
import { cx } from "@/lib/cx";
import { formatIndian, telHref } from "@/lib/phone";
import { buttonClass, type ButtonSize, type ButtonVariant } from "@/components/ui/Button";

/** The receptionist line: the voice number when the clinic has one, else the front desk. */
export function receptionNumber(profile: Pick<ClinicProfile, "voice_number" | "phone_display">): {
  href: string;
  display: string;
} | null {
  const raw = profile.voice_number || profile.phone_display;
  const href = telHref(raw);
  if (!raw || !href) return null;
  return { href, display: raw.startsWith("+91") && !raw.includes(" ") ? formatIndian(raw) : raw };
}

export function CallReceptionist({
  profile,
  variant = "secondary",
  size = "lg",
  showNumber = true,
  label = "Call our receptionist",
  className,
}: {
  profile: Pick<ClinicProfile, "voice_number" | "phone_display">;
  variant?: ButtonVariant;
  size?: ButtonSize;
  showNumber?: boolean;
  label?: string;
  className?: string;
}) {
  const number = receptionNumber(profile);
  if (!number) return null;
  return (
    <a href={number.href} className={cx(buttonClass(variant, size), "gap-2.5", className)}>
      <Phone className="size-4" aria-hidden="true" />
      <span>{label}</span>
      {showNumber ? <span className="font-normal tnum opacity-80">{number.display}</span> : null}
    </a>
  );
}
