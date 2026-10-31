import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { AsterMark } from "@/components/public/Brand";
import { LoginForm } from "@/components/staff/LoginForm";
import { getClinic, isMock } from "@/lib/server/publicData";
import { FALLBACK_PROFILE } from "@/lib/site";

export const metadata: Metadata = {
  title: "Staff sign-in",
  robots: { index: false, follow: false },
};

export const dynamic = "force-dynamic";

export default async function StaffLoginPage() {
  const mock = process.env.NODE_ENV !== "production" && isMock();
  const name = (await getClinic())?.name ?? FALLBACK_PROFILE.name;
  return (
    <main className="grid min-h-dvh lg:grid-cols-[1fr_1.1fr]">
      <div className="flex flex-col justify-center px-6 py-12 sm:px-12">
        <div className="mx-auto w-full max-w-sm">
          <Link href="/" className="inline-flex items-center gap-2.5">
            <AsterMark />
            <span className="font-display text-xl">{name}</span>
          </Link>
          <h1 className="mt-10 font-display text-3xl">Staff sign-in</h1>
          <p className="mt-2 text-ink-2">For the clinic team. Patients can book or check a booking on the main site.</p>
          <div className="mt-8">
            <Suspense>
              <LoginForm />
            </Suspense>
          </div>
          {mock ? (
            <div className="mt-8 rounded-md border border-dashed border-line-strong p-4 text-sm text-ink-2">
              <p className="font-semibold text-ink">Development mock mode</p>
              <p className="mt-1">
                No API is configured, so the console runs on fixtures. Sign in as <code>owner@asterdental.example</code>,{" "}
                <code>admin@asterdental.example</code> or <code>viewer@asterdental.example</code>; the password is in{" "}
                <code>web/lib/server/mock/fixtures.ts</code>.
              </p>
            </div>
          ) : null}
        </div>
      </div>
      <div aria-hidden="true" className="relative hidden overflow-hidden border-l border-line bg-ink lg:block">
        <div className="absolute inset-0 grid grid-cols-6 gap-px opacity-[0.16]">
          {Array.from({ length: 6 * 22 }, (_, i) => (
            <span key={i} className={i % 7 === 0 || i % 11 === 3 ? "bg-accent" : "bg-paper/40"} />
          ))}
        </div>
        <div className="absolute inset-x-12 bottom-12">
          <p className="font-display text-4xl leading-tight text-paper">
            Today&apos;s diary, call-backs and conversations, <em className="text-accent-tint">in one place.</em>
          </p>
        </div>
      </div>
    </main>
  );
}
