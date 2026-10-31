import type { Metadata } from "next";
import Link from "next/link";
import { Suspense } from "react";

import { AsterMark } from "@/components/public/Brand";
import { DiaryIllustration } from "@/components/public/DiaryIllustration";
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
      <div aria-hidden="true" className="hidden flex-col justify-center gap-12 border-l border-line bg-sunk/60 px-12 py-16 lg:flex xl:px-20">
        <div className="mx-auto w-full max-w-lg -rotate-1 drop-shadow-sm">
          <DiaryIllustration />
        </div>
        <p className="mx-auto max-w-lg font-display text-4xl leading-tight text-ink">
          Today&apos;s diary, call-backs and conversations, <em className="text-accent">in one place.</em>
        </p>
      </div>
    </main>
  );
}
