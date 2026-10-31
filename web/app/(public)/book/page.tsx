import type { Metadata } from "next";
import { Suspense } from "react";

import { BookingStepper } from "@/components/public/booking/BookingStepper";
import { receptionNumber } from "@/components/public/CallLink";
import { Loading, Skeleton } from "@/components/ui/States";
import { getClinic, getDoctors, getServices } from "@/lib/server/publicData";
import { FALLBACK_PROFILE } from "@/lib/site";

export const revalidate = 300;

export const metadata: Metadata = {
  title: "Book an appointment",
  description: "Choose a treatment, a dentist and a time that suits you. You get a booking reference straight away.",
  alternates: { canonical: "/book" },
};

function StepperSkeleton() {
  return (
    <Loading label="Loading the booking form" className="grid gap-3 sm:grid-cols-2">
      {Array.from({ length: 6 }, (_, i) => (
        <Skeleton key={i} className="h-24 rounded-lg" />
      ))}
    </Loading>
  );
}

export default async function BookPage() {
  const [clinic, services, doctors] = await Promise.all([getClinic(), getServices(), getDoctors()]);
  const profile = clinic ?? FALLBACK_PROFILE;
  const number = receptionNumber(profile);
  return (
    <div className="mx-auto max-w-6xl px-4 pt-10 pb-8 sm:px-6 md:pt-14">
      <header className="mb-10 max-w-2xl">
        <p className="text-xs font-semibold tracking-[0.16em] text-accent uppercase">Book online</p>
        <h1 className="mt-3 font-display text-4xl leading-tight tracking-[-0.01em] sm:text-5xl">Find a time at {profile.name}</h1>
      </header>
      <Suspense fallback={<StepperSkeleton />}>
        <BookingStepper
          services={services ?? []}
          doctors={doctors ?? []}
          timeZone={profile.timezone}
          callHref={number?.href ?? null}
          callDisplay={number?.display ?? null}
        />
      </Suspense>
    </div>
  );
}
