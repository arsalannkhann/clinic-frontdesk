import type { Metadata } from "next";

import { CallReceptionist } from "@/components/public/CallLink";
import { CallbackForm } from "@/components/public/CallbackForm";
import { getClinic } from "@/lib/server/publicData";
import { FALLBACK_PROFILE } from "@/lib/site";

export const revalidate = 300;

export const metadata: Metadata = {
  title: "Request a call back",
  description: "Leave your number and the front desk will call you back during clinic hours.",
  alternates: { canonical: "/callback" },
};

export default async function CallbackPage() {
  const profile = (await getClinic()) ?? FALLBACK_PROFILE;
  return (
    <div className="mx-auto grid max-w-5xl gap-12 px-4 pt-10 sm:px-6 md:grid-cols-[1.1fr_1fr] md:pt-14">
      <div>
        <p className="text-xs font-semibold tracking-[0.16em] text-accent uppercase">Call back</p>
        <h1 className="mt-3 font-display text-4xl leading-tight sm:text-5xl">We&apos;ll ring you</h1>
        <p className="mt-4 mb-8 text-lg text-ink-2">
          Questions about cost, insurance or a treatment plan are easier on the phone. Leave your number and a person from the front desk will
          call.
        </p>
        <CallbackForm hoursNote={profile.hours_note} />
      </div>
      <aside className="h-fit rounded-lg border border-line bg-raised p-6">
        <h2 className="font-display text-xl">Rather call now?</h2>
        <p className="mt-2 text-ink-2">Our receptionist answers straight away, books appointments and can pass you to the front desk.</p>
        <div className="mt-5">
          <CallReceptionist profile={profile} size="md" variant="primary" />
        </div>
        <p className="mt-6 border-t border-line pt-4 text-sm text-ink-3">
          For severe swelling, difficulty breathing or bleeding that won&apos;t stop, go to the nearest emergency department or call 112.
        </p>
      </aside>
    </div>
  );
}
