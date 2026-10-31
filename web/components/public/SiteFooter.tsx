import Link from "next/link";

import type { ClinicProfile } from "@/lib/api/types";
import { AsterMark } from "./Brand";
import { receptionNumber } from "./CallLink";

export function SiteFooter({ profile }: { profile: ClinicProfile }) {
  const number = receptionNumber(profile);
  return (
    <footer className="mt-24 border-t border-line bg-sunk/60">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-12 sm:px-6 md:grid-cols-[1.4fr_1fr_1fr]">
        <div className="flex flex-col gap-3">
          <div className="flex items-center gap-2.5">
            <AsterMark />
            <span className="font-display text-xl">{profile.name}</span>
          </div>
          {profile.address ? <p className="max-w-xs text-sm text-ink-2">{profile.address}</p> : null}
          <p className="max-w-sm text-xs text-ink-3">
            This is a demonstration site for a fictional clinic. Names, numbers and reviews are synthetic.
          </p>
        </div>
        <div>
          <h2 className="text-xs font-semibold tracking-[0.12em] text-ink-3 uppercase">Patients</h2>
          <ul className="mt-3 flex flex-col gap-2 text-sm">
            <li>
              <Link href="/book" className="text-ink hover:text-accent">
                Book online
              </Link>
            </li>
            <li>
              <Link href="/booking" className="text-ink hover:text-accent">
                Check a booking
              </Link>
            </li>
            <li>
              <Link href="/callback" className="text-ink hover:text-accent">
                Request a call back
              </Link>
            </li>
          </ul>
        </div>
        <div>
          <h2 className="text-xs font-semibold tracking-[0.12em] text-ink-3 uppercase">Contact</h2>
          <ul className="mt-3 flex flex-col gap-2 text-sm">
            {number ? (
              <li>
                <a href={number.href} className="text-ink tnum hover:text-accent">
                  {number.display}
                </a>
              </li>
            ) : null}
            {profile.email ? (
              <li>
                <a href={`mailto:${profile.email}`} className="text-ink hover:text-accent">
                  {profile.email}
                </a>
              </li>
            ) : null}
            {profile.hours_note ? <li className="text-ink-2">{profile.hours_note}</li> : null}
            <li>
              <Link href="/staff" className="text-ink-3 hover:text-ink">
                Staff sign-in
              </Link>
            </li>
          </ul>
        </div>
      </div>
    </footer>
  );
}
