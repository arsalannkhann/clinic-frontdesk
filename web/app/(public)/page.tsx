import { ArrowRight, CalendarDays, MapPin, MessageCircle, Phone } from "lucide-react";
import type { Metadata } from "next";
import Link from "next/link";

import { CallReceptionist, receptionNumber } from "@/components/public/CallLink";
import { DiaryIllustration } from "@/components/public/DiaryIllustration";
import { Monogram } from "@/components/public/Monogram";
import { OpenChatButton } from "@/components/public/OpenChatButton";
import { ButtonLink } from "@/components/ui/Button";
import { Notice } from "@/components/ui/States";
import { safeColour } from "@/lib/calendar";
import { siteUrl } from "@/lib/server/config";
import { getClinic, getDoctors, getServices } from "@/lib/server/publicData";
import { dentistJsonLd, jsonLdString } from "@/lib/seo";
import { FALLBACK_PROFILE, FAQ, JOURNEY, SAMPLE_REVIEWS } from "@/lib/site";

export const revalidate = 300;

export async function generateMetadata(): Promise<Metadata> {
  const profile = (await getClinic()) ?? FALLBACK_PROFILE;
  const description =
    profile.tagline ??
    "A dental practice in Indiranagar, Bengaluru. Book online, chat with the clinic assistant or call the receptionist.";
  return {
    title: { absolute: `${profile.name}, Indiranagar · Dentist in Bengaluru` },
    description,
    alternates: { canonical: "/" },
    openGraph: {
      type: "website",
      title: profile.name,
      description,
      url: "/",
      siteName: profile.name,
      locale: "en_IN",
    },
    twitter: { card: "summary", title: profile.name, description },
  };
}

function SectionHeading({ eyebrow, title, children, id }: { eyebrow: string; title: string; children?: React.ReactNode; id: string }) {
  return (
    <div className="max-w-2xl">
      <p className="text-xs font-semibold tracking-[0.16em] text-accent uppercase">{eyebrow}</p>
      <h2 id={id} className="mt-3 font-display text-3xl leading-[1.1] tracking-[-0.01em] text-ink sm:text-[2.6rem]">
        {title}
      </h2>
      {children ? <div className="mt-4 text-lg leading-relaxed text-ink-2">{children}</div> : null}
    </div>
  );
}

export default async function HomePage() {
  const [clinic, services, doctors] = await Promise.all([getClinic(), getServices(), getDoctors()]);
  const profile = clinic ?? FALLBACK_PROFILE;
  const number = receptionNumber(profile);
  const serviceName = new Map((services ?? []).map((s) => [s.id, s.name]));
  const jsonLd = dentistJsonLd(profile, services, doctors, siteUrl());

  return (
    <>
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: jsonLdString(jsonLd) }} />

      {/* Hero */}
      <section aria-labelledby="hero-title" className="mx-auto grid max-w-6xl items-center gap-12 px-4 pt-12 pb-16 sm:px-6 md:pt-20 lg:grid-cols-[1.15fr_1fr]">
        <div>
          <p className="flex items-center gap-2 text-sm text-ink-2">
            <MapPin className="size-4 text-accent" aria-hidden="true" />
            Indiranagar, Bengaluru
          </p>
          <h1 id="hero-title" className="mt-5 font-display text-[2.6rem] leading-[1.04] tracking-[-0.02em] text-ink sm:text-6xl">
            Dental care that takes the time to <em className="text-accent">explain</em>.
          </h1>
          <p className="mt-6 max-w-xl text-lg leading-relaxed text-ink-2">
            {profile.tagline ?? FALLBACK_PROFILE.tagline} Book a real opening online, ask the clinic assistant a question, or call and
            speak to our receptionist.
          </p>
          <div className="mt-8 flex flex-col gap-3 sm:flex-row sm:flex-wrap">
            <ButtonLink href="/book" size="lg" icon={<CalendarDays className="size-4" aria-hidden="true" />}>
              Book online
            </ButtonLink>
            <CallReceptionist profile={profile} />
          </div>
          {profile.hours_note ? <p className="mt-6 text-sm text-ink-3">{profile.hours_note}</p> : null}
        </div>
        <div className="relative mx-auto w-full max-w-md lg:max-w-none">
          <DiaryIllustration />
        </div>
      </section>

      {!clinic ? (
        <div className="mx-auto max-w-6xl px-4 sm:px-6">
          <Notice tone="warn">
            Some clinic details could not be loaded just now. Booking still works; if anything looks missing, please refresh in a minute.
          </Notice>
        </div>
      ) : null}

      {/* Three ways */}
      <section aria-labelledby="ways-title" className="border-y border-line bg-raised/70">
        <div className="mx-auto max-w-6xl px-4 py-16 sm:px-6">
          <SectionHeading eyebrow="Three ways to book" title="Whichever is easiest for you." id="ways-title">
            All three see the same diary, so a time offered online is a time the clinic can actually keep.
          </SectionHeading>
          <div className="mt-10 grid gap-px overflow-hidden rounded-lg border border-line bg-line md:grid-cols-3">
            <div className="flex flex-col gap-3 bg-raised p-6">
              <CalendarDays className="size-6 text-accent" aria-hidden="true" />
              <h3 className="font-display text-xl">Online</h3>
              <p className="flex-1 text-ink-2">Pick a treatment, a dentist and a time. Takes about a minute; you get a reference at the end.</p>
              <Link href="/book" className="inline-flex items-center gap-1 font-semibold text-accent hover:text-accent-strong">
                Book online <ArrowRight className="size-4" aria-hidden="true" />
              </Link>
            </div>
            <div className="flex flex-col gap-3 bg-raised p-6">
              <MessageCircle className="size-6 text-accent" aria-hidden="true" />
              <h3 className="font-display text-xl">Chat</h3>
              <p className="flex-1 text-ink-2">
                Ask the clinic&apos;s virtual assistant about times, treatments or parking. It suggests a slot; you confirm it.
              </p>
              {profile.chat_enabled ? (
                <OpenChatButton variant="quiet" size="md" className="justify-start">
                  Start a chat <ArrowRight className="size-4" aria-hidden="true" />
                </OpenChatButton>
              ) : (
                <p className="text-sm text-ink-3">Chat is unavailable right now.</p>
              )}
            </div>
            <div className="flex flex-col gap-3 bg-raised p-6">
              <Phone className="size-6 text-accent" aria-hidden="true" />
              <h3 className="font-display text-xl">Phone</h3>
              <p className="flex-1 text-ink-2">
                Our receptionist answers every call, books and reads your reference back. Prefer a person? Ask to be called back.
              </p>
              <div className="flex flex-wrap gap-x-4 gap-y-1">
                {number ? (
                  <a href={number.href} className="inline-flex items-center gap-1 font-semibold text-accent tnum hover:text-accent-strong">
                    {number.display} <ArrowRight className="size-4" aria-hidden="true" />
                  </a>
                ) : null}
                <Link href="/callback" className="font-semibold text-ink-2 hover:text-ink">
                  Request a call back
                </Link>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Treatments */}
      <section aria-labelledby="treatments-title" id="treatments" className="mx-auto max-w-6xl scroll-mt-20 px-4 py-20 sm:px-6">
        <SectionHeading eyebrow="Treatments" title="What we do, and how long it takes." id="treatments-title">
          Times are chair time, so you can plan the rest of your day. We tell you the cost before any treatment, never after.
        </SectionHeading>
        {services && services.length ? (
          <ul className="mt-10 grid gap-x-10 gap-y-0 md:grid-cols-2">
            {services.map((service) => (
              <li key={service.id} className="flex gap-4 border-t border-line py-6">
                <span aria-hidden="true" className="mt-1.5 h-10 w-1 shrink-0 rounded-full" style={{ background: safeColour(service.colour) }} />
                <div className="flex-1">
                  <div className="flex items-baseline justify-between gap-4">
                    <h3 className="font-display text-xl text-ink">{service.name}</h3>
                    <span className="shrink-0 text-sm text-ink-3 tnum">{service.duration_minutes} min</span>
                  </div>
                  {service.description ? <p className="mt-1.5 leading-relaxed text-ink-2">{service.description}</p> : null}
                  <Link
                    href={`/book?service=${encodeURIComponent(service.id)}`}
                    className="mt-2 inline-flex items-center gap-1 text-sm font-semibold text-accent hover:text-accent-strong"
                  >
                    Book {service.name.toLowerCase()} <ArrowRight className="size-3.5" aria-hidden="true" />
                  </Link>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mt-8 text-ink-2">Our list of treatments could not be loaded. You can still book online or call us.</p>
        )}
      </section>

      {/* Dentists */}
      <section aria-labelledby="dentists-title" id="dentists" className="scroll-mt-20 border-y border-line bg-raised/70">
        <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
          <SectionHeading eyebrow="Dentists" title="Our dentists, each with a speciality." id="dentists-title">
            You can choose who you see, or take the first available opening.
          </SectionHeading>
          {doctors && doctors.length ? (
            <ul className="mt-10 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
              {doctors.map((doctor) => (
                <li key={doctor.id} className="flex flex-col gap-4 rounded-lg border border-line bg-paper p-6">
                  <div className="flex items-center gap-4">
                    <Monogram name={doctor.name} colour={doctor.colour} className="size-14 text-xl" />
                    <h3 className="font-display text-xl leading-tight">{doctor.name}</h3>
                  </div>
                  {doctor.public_bio ? <p className="flex-1 leading-relaxed text-ink-2">{doctor.public_bio}</p> : null}
                  {doctor.service_ids.length ? (
                    <p className="text-sm text-ink-3">
                      {doctor.service_ids
                        .map((id) => serviceName.get(id))
                        .filter(Boolean)
                        .join(" · ")}
                    </p>
                  ) : null}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-8 text-ink-2">Our dentists&apos; profiles could not be loaded just now.</p>
          )}
        </div>
      </section>

      {/* Journey */}
      <section aria-labelledby="journey-title" className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
        <SectionHeading eyebrow="Your visit" title="From booking to aftercare." id="journey-title" />
        <ol className="mt-10 grid gap-8 md:grid-cols-4">
          {JOURNEY.map((step, index) => (
            <li key={step.title} className="border-t-2 border-ink pt-5">
              <span className="font-display text-3xl text-accent italic tnum">{String(index + 1).padStart(2, "0")}</span>
              <h3 className="mt-2 font-display text-xl">{step.title}</h3>
              <p className="mt-2 leading-relaxed text-ink-2">{step.body}</p>
            </li>
          ))}
        </ol>
      </section>

      {/* Reviews */}
      <section aria-labelledby="reviews-title" className="mx-auto max-w-6xl px-4 pb-20 sm:px-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <SectionHeading eyebrow="Patients" title="What a visit is like." id="reviews-title" />
          <p className="rounded-full border border-warn/30 bg-warn-tint px-3 py-1 text-sm font-semibold text-warn">
            Sample content: illustrative reviews, not from real patients
          </p>
        </div>
        <ul className="mt-10 grid gap-6 md:grid-cols-3">
          {SAMPLE_REVIEWS.map((review) => (
            <li key={review.quote}>
              <figure className="flex h-full flex-col justify-between gap-6 rounded-lg border border-dashed border-line-strong p-6">
                <blockquote className="font-display text-xl leading-snug text-ink">&ldquo;{review.quote}&rdquo;</blockquote>
                <figcaption className="text-sm text-ink-3">{review.who}</figcaption>
              </figure>
            </li>
          ))}
        </ul>
      </section>

      {/* Visit */}
      <section aria-labelledby="visit-title" id="visit" className="scroll-mt-20 border-y border-line bg-ink text-paper">
        <div className="mx-auto grid max-w-6xl gap-12 px-4 py-20 sm:px-6 md:grid-cols-2">
          <div>
            <p className="text-xs font-semibold tracking-[0.16em] text-accent-tint uppercase">Visit</p>
            <h2 id="visit-title" className="mt-3 font-display text-3xl leading-[1.1] sm:text-[2.6rem]">
              In the middle of Indiranagar.
            </h2>
            {profile.about ? <p className="mt-5 max-w-lg text-lg leading-relaxed text-paper/80">{profile.about}</p> : null}
          </div>
          <dl className="grid gap-6 self-end text-[1.05rem]">
            {profile.address ? (
              <div>
                <dt className="text-sm text-paper/60">Address</dt>
                <dd className="mt-1">{profile.address}</dd>
                {profile.map_url ? (
                  <dd className="mt-2">
                    <a href={profile.map_url} className="font-semibold text-accent-tint underline underline-offset-4 hover:text-white" rel="noopener noreferrer" target="_blank">
                      Open in a map
                    </a>
                  </dd>
                ) : null}
              </div>
            ) : null}
            {profile.hours_note ? (
              <div>
                <dt className="text-sm text-paper/60">Hours</dt>
                <dd className="mt-1">{profile.hours_note}</dd>
              </div>
            ) : null}
            {number ? (
              <div>
                <dt className="text-sm text-paper/60">Phone</dt>
                <dd className="mt-1">
                  <a href={number.href} className="tnum underline-offset-4 hover:underline">
                    {number.display}
                  </a>
                </dd>
              </div>
            ) : null}
            {profile.email ? (
              <div>
                <dt className="text-sm text-paper/60">Email</dt>
                <dd className="mt-1">
                  <a href={`mailto:${profile.email}`} className="underline-offset-4 hover:underline">
                    {profile.email}
                  </a>
                </dd>
              </div>
            ) : null}
          </dl>
        </div>
      </section>

      {/* FAQ */}
      <section aria-labelledby="faq-title" className="mx-auto max-w-3xl px-4 pt-20 sm:px-6">
        <SectionHeading eyebrow="Questions" title="Before you come in." id="faq-title" />
        <div className="mt-8 divide-y divide-line border-y border-line">
          {FAQ.map((item) => (
            <details key={item.q} className="group py-5">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-6 text-lg font-semibold text-ink [&::-webkit-details-marker]:hidden">
                {item.q}
                <span aria-hidden="true" className="font-display text-2xl leading-none text-accent transition-transform group-open:rotate-45">
                  +
                </span>
              </summary>
              <p className="mt-3 leading-relaxed text-ink-2">{item.a}</p>
            </details>
          ))}
        </div>
      </section>
    </>
  );
}
