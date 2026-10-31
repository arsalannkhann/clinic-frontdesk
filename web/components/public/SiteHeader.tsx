import { Menu, Phone } from "lucide-react";
import Link from "next/link";

import type { ClinicProfile } from "@/lib/api/types";
import { ButtonLink } from "@/components/ui/Button";
import { BrandLink } from "./Brand";
import { receptionNumber } from "./CallLink";

const NAV = [
  { href: "/#treatments", label: "Treatments" },
  { href: "/#dentists", label: "Dentists" },
  { href: "/#visit", label: "Visit" },
  { href: "/booking", label: "Check a booking" },
];

export function SiteHeader({ profile }: { profile: ClinicProfile }) {
  const number = receptionNumber(profile);
  return (
    <header className="sticky top-0 z-30 border-b border-line/80 bg-paper/92 backdrop-blur supports-[backdrop-filter]:bg-paper/80">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
        <BrandLink name={profile.name} />
        <nav aria-label="Main" className="hidden items-center gap-6 md:flex">
          {NAV.map((item) => (
            <Link key={item.href} href={item.href} className="text-[0.95rem] text-ink-2 hover:text-ink">
              {item.label}
            </Link>
          ))}
        </nav>
        <div className="flex items-center gap-2">
          {number ? (
            <a
              href={number.href}
              className="hidden items-center gap-2 rounded-md px-3 py-2 text-[0.95rem] font-semibold text-ink hover:bg-sunk lg:inline-flex"
            >
              <Phone className="size-4 text-accent" aria-hidden="true" />
              <span className="tnum">{number.display}</span>
            </a>
          ) : null}
          <ButtonLink href="/book" size="md" className="hidden sm:inline-flex">
            Book online
          </ButtonLink>
          {/* No-JS mobile menu: <details> opens and closes without scripts. */}
          <details className="group relative md:hidden">
            <summary className="flex size-10 cursor-pointer list-none items-center justify-center rounded-md border border-line-strong bg-raised [&::-webkit-details-marker]:hidden">
              <Menu className="size-5" aria-hidden="true" />
              <span className="sr-only">Menu</span>
            </summary>
            <nav
              aria-label="Mobile"
              className="absolute right-0 mt-2 flex w-60 flex-col rounded-lg border border-line bg-raised p-2 shadow-float"
            >
              {NAV.map((item) => (
                <Link key={item.href} href={item.href} className="rounded-md px-3 py-2.5 text-ink hover:bg-sunk">
                  {item.label}
                </Link>
              ))}
              <Link href="/callback" className="rounded-md px-3 py-2.5 text-ink hover:bg-sunk">
                Request a call back
              </Link>
              <Link href="/book" className="mt-1 rounded-md bg-accent px-3 py-2.5 text-center font-semibold text-white">
                Book online
              </Link>
              {number ? (
                <a href={number.href} className="mt-1 rounded-md border border-line-strong px-3 py-2.5 text-center font-semibold text-ink">
                  Call {number.display}
                </a>
              ) : null}
            </nav>
          </details>
        </div>
      </div>
    </header>
  );
}
