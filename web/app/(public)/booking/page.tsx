import type { Metadata } from "next";

import { CallReceptionist } from "@/components/public/CallLink";
import { LookupForm } from "@/components/public/LookupForm";
import { getClinic } from "@/lib/server/publicData";
import { FALLBACK_PROFILE } from "@/lib/site";

export const revalidate = 300;

export const metadata: Metadata = {
  title: "Check a booking",
  description: "See the status of your appointment with your booking reference and phone number.",
  alternates: { canonical: "/booking" },
};

export default async function BookingLookupPage() {
  const profile = (await getClinic()) ?? FALLBACK_PROFILE;
  return (
    <div className="mx-auto max-w-5xl px-4 pt-10 sm:px-6 md:pt-14">
      <header className="mb-10 max-w-2xl">
        <p className="text-xs font-semibold tracking-[0.16em] text-accent uppercase">Your booking</p>
        <h1 className="mt-3 font-display text-4xl leading-tight sm:text-5xl">Check a booking</h1>
        <p className="mt-4 text-lg text-ink-2">
          Enter the reference from your confirmation and the mobile number you booked with. To change or cancel, call us.
        </p>
      </header>
      <LookupForm timeZone={profile.timezone} />
      <div className="mt-12 border-t border-line pt-8">
        <CallReceptionist profile={profile} size="md" />
      </div>
    </div>
  );
}
