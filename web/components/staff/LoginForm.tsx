"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, type FormEvent } from "react";

import { api, errorMessage } from "@/lib/api/client";
import type { LoginResult } from "@/lib/api/types";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Field";

/** Only same-site console paths are accepted as a post-login destination. */
export function safeNext(value: string | null): string {
  if (!value || !/^\/staff(?:[/?#]|$)/.test(value) || value.includes("\\")) return "/staff";
  if (value.startsWith("/staff/login")) return "/staff";
  return value;
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
