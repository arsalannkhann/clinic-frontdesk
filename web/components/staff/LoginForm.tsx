"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { api, errorMessage } from "@/lib/api/client";
import type { LoginResult } from "@/lib/api/types";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Field";

const HOME = "/staff";

function currentOrigin(): string {
  return typeof window === "undefined" ? "http://localhost" : window.location.origin;
}

/**
 * Only a same-origin console path is accepted as a post-login destination. The value is
 * resolved with `new URL(next, origin)`; it must stay on this origin, resolve to `/staff` or
 * `/staff/...` (not the sign-in page), and contain no dot segments, encoded or not: the path
 * as written must equal the normalised path. Anything else goes to `/staff`.
 */
export function safeNext(value: string | null, origin: string = currentOrigin()): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return HOME;
  if (/[\\\u0000-\u001f\u007f\s]/.test(value)) return HOME;
  let url: URL;
  try {
    url = new URL(value, origin);
  } catch {
    return HOME;
  }
  if (url.origin !== new URL(origin).origin) return HOME;
  const rawPath = value.split(/[?#]/, 1)[0]!;
  if (rawPath !== url.pathname) return HOME; // a dot segment (or %2e) was normalised away
  if (rawPath.split("/").some((segment) => /^(?:\.|%2e){1,2}$/i.test(segment))) return HOME;
  if (url.pathname !== HOME && !url.pathname.startsWith(`${HOME}/`)) return HOME;
  if (url.pathname === `${HOME}/login` || url.pathname.startsWith(`${HOME}/login/`)) return HOME;
  return `${url.pathname}${url.search}${url.hash}`;
}

export function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (!email.trim() || !password) {
      setError("Enter your email and password.");
      return;
    }
    setBusy(true);
    try {
      await api<LoginResult>("/api/staff/login", { method: "POST", body: { email: email.trim(), password } });
      setPassword("");
      router.replace(safeNext(params.get("next")));
      router.refresh();
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-5">
      <Field label="Email">
        {(props) => (
          <Input {...props} type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
        )}
      </Field>
      <Field label="Password">
        {(props) => (
          <Input {...props} type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        )}
      </Field>
      {error ? (
        <p role="alert" className="rounded-md border border-bad/25 bg-bad-tint/60 px-3 py-2 text-sm font-medium">
          {error}
        </p>
      ) : null}
      <Button type="submit" size="lg" loading={busy}>
        Sign in
      </Button>
    </form>
  );
}
