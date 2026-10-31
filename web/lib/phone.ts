/**
 * Indian mobile numbers. The form shows a fixed +91 prefix and takes the 10-digit number;
 * the API receives E.164 (`+91XXXXXXXXXX`). Indian mobile numbers start with 6, 7, 8 or 9.
 */

export type PhoneResult = { ok: true; e164: string } | { ok: false; message: string };

/** Digits only, dropping a pasted +91, 91 or leading 0. */
export function nationalDigits(input: string): string {
  let digits = input.replace(/\D/g, "");
  if (digits.length === 12 && digits.startsWith("91")) digits = digits.slice(2);
  else if (digits.length === 11 && digits.startsWith("0")) digits = digits.slice(1);
  return digits;
}

export function toIndianE164(input: string): PhoneResult {
  const digits = nationalDigits(input);
  if (!digits) return { ok: false, message: "Enter your mobile number." };
  if (digits.length !== 10) return { ok: false, message: "Enter the 10-digit mobile number, without +91." };
  if (!/^[6-9]/.test(digits)) return { ok: false, message: "Indian mobile numbers start with 6, 7, 8 or 9." };
  return { ok: true, e164: `+91${digits}` };
}

/** "+91 90000 00100" for display; anything else is returned unchanged. */
export function formatIndian(e164: string): string {
  const match = /^\+91(\d{5})(\d{5})$/.exec(e164);
  return match ? `+91 ${match[1]} ${match[2]}` : e164;
}

/** A tel: href from an E.164 or display number, or null when it has no digits. */
export function telHref(value: string | null | undefined): string | null {
  if (!value) return null;
  const plus = value.trim().startsWith("+");
  const digits = value.replace(/\D/g, "");
  if (digits.length < 6) return null;
  return `tel:${plus ? "+" : ""}${digits}`;
}
