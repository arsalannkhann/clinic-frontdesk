"use client";

import {
  CalendarDays,
  ClipboardList,
  Home,
  LogOut,
  Menu,
  MessagesSquare,
  PhoneCall,
  Settings,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import useSWR, { SWRConfig } from "swr";

import { api, ApiError, errorMessage, fetcher, onUnauthorized } from "@/lib/api/client";
import type { ClinicConfig, StaffUser } from "@/lib/api/types";
import { cx } from "@/lib/cx";
import { role as roleLabel } from "@/lib/labels";
import { isValidTimeZone } from "@/lib/time";
import { AsterMark } from "@/components/public/Brand";
import { Button } from "@/components/ui/Button";
import { ErrorState, Loading, Skeleton } from "@/components/ui/States";
import { ToastProvider } from "@/components/ui/Toast";
import { canWriteRole, StaffContext, type StaffContextValue } from "./StaffContext";

const NAV = [
  { href: "/staff", label: "Today", icon: Home },
  { href: "/staff/calendar", label: "Calendar", icon: CalendarDays },
  { href: "/staff/appointments", label: "Appointments", icon: ClipboardList },
  { href: "/staff/callbacks", label: "Call-backs", icon: PhoneCall },
  { href: "/staff/conversations", label: "Conversations", icon: MessagesSquare },
  { href: "/staff/settings", label: "Settings", icon: Settings },
];

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <ul className="flex flex-col gap-0.5">
      {NAV.map(({ href, label, icon: Icon }) => {
        const active = href === "/staff" ? pathname === "/staff" : pathname.startsWith(href);
        return (
          <li key={href}>
            <Link
              href={href}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              className={cx(
                "flex items-center gap-3 rounded-md px-3 py-2 text-[0.95rem] transition-colors",
                active ? "bg-ink text-paper" : "text-ink-2 hover:bg-sunk hover:text-ink",
              )}
            >
              <Icon className="size-4" aria-hidden="true" />
              {label}
            </Link>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * The console chrome: sidebar, mobile header and main area. It renders straight away and
 * stays mounted across navigation (it lives in the console layout); while the signed-in user
 * and clinic load, only the account block and the content area show skeletons.
 */
function Shell({ value, children }: { value: StaffContextValue | null; children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [menuOpen, setMenuOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);

  useEffect(() => setMenuOpen(false), [pathname]);

  async function signOut() {
    setSigningOut(true);
    try {
      await api("/api/staff/logout", { method: "POST" });
    } catch {
      // The cookie is cleared server-side either way.
    }
    router.replace("/staff/login");
    router.refresh();
  }

  const clinicName = value?.clinic.name ?? null;
  const name = clinicName ? (
    <span className="block truncate font-display text-lg leading-tight">{clinicName}</span>
  ) : (
    <Skeleton className="h-5 w-32" />
  );

  const account = (
    <div className="flex flex-col gap-2 border-t border-line pt-4">
      {value ? (
        <div className="min-w-0 px-1">
          <p className="truncate text-sm font-semibold text-ink">{value.user.email}</p>
          <p className="text-xs text-ink-3">{roleLabel(value.user.role).text}</p>
        </div>
      ) : (
        <Loading label="Loading your account" className="flex flex-col gap-1.5 px-1">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-3 w-16" />
        </Loading>
      )}
      <Button variant="ghost" size="sm" onClick={signOut} loading={signingOut} icon={<LogOut className="size-4" aria-hidden="true" />} className="justify-start">
        Sign out
      </Button>
    </div>
  );

  return (
    <div className="min-h-dvh bg-paper lg:grid lg:grid-cols-[15rem_1fr]">
      <a href="#staff-main" className="sr-only z-50 rounded-md bg-ink px-4 py-2 text-paper focus:not-sr-only focus:fixed focus:top-3 focus:left-3">
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-dvh flex-col gap-6 border-r border-line bg-raised/60 px-4 py-5 lg:flex">
        <Link href="/staff" className="flex items-center gap-2.5 px-1">
          <AsterMark className="size-6 shrink-0" />
          <span className="min-w-0">
            {name}
            <span className="block text-xs text-ink-3">Staff console</span>
          </span>
        </Link>
        <nav aria-label="Staff" className="flex-1">
          <NavLinks />
        </nav>
        {account}
      </aside>

      <div className="min-w-0">
        <header className="sticky top-0 z-30 flex h-14 items-center justify-between border-b border-line bg-paper/95 px-4 backdrop-blur lg:hidden">
          <Link href="/staff" className="flex min-w-0 items-center gap-2">
            <AsterMark className="size-6 shrink-0" />
            <span className="min-w-0">{name}</span>
          </Link>
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-expanded={menuOpen}
            aria-controls="staff-mobile-nav"
            className="inline-flex size-10 items-center justify-center rounded-md border border-line-strong bg-raised"
          >
            {menuOpen ? <X className="size-5" aria-hidden="true" /> : <Menu className="size-5" aria-hidden="true" />}
            <span className="sr-only">{menuOpen ? "Close menu" : "Open menu"}</span>
          </button>
        </header>
        {menuOpen ? (
          <nav id="staff-mobile-nav" aria-label="Staff" className="border-b border-line bg-raised px-4 py-4 lg:hidden">
            <NavLinks onNavigate={() => setMenuOpen(false)} />
            <div className="mt-4">{account}</div>
          </nav>
        ) : null}
        <main id="staff-main" tabIndex={-1} className="mx-auto w-full max-w-[90rem] px-4 py-6 focus:outline-none sm:px-6 lg:px-8 lg:py-8">
          {value && !value.canWrite ? (
            <p className="mb-5 rounded-md border border-line bg-sunk/70 px-3 py-2 text-sm text-ink-2">
              You have read-only access. Ask the clinic owner if you need to make changes.
            </p>
          ) : null}
          {value ? (
            children
          ) : (
            <Loading label="Loading the staff console">
              <Skeleton className="h-9 w-56" />
              <Skeleton className="mt-3 h-5 w-40" />
              <Skeleton className="mt-8 h-64" />
            </Loading>
          )}
        </main>
      </div>
    </div>
  );
}

export function StaffShell({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  useEffect(
    () =>
      onUnauthorized(() => {
        const next = pathname && pathname !== "/staff" ? `?next=${encodeURIComponent(pathname)}` : "";
        router.replace(`/staff/login${next}`);
      }),
    [router, pathname],
  );

  const me = useSWR<StaffUser>("/api/staff/me", fetcher, { revalidateOnFocus: true, shouldRetryOnError: false });
  const clinic = useSWR<ClinicConfig>(me.data ? "/api/admin/clinic" : null, fetcher, { shouldRetryOnError: false });

  const value = useMemo<StaffContextValue | null>(() => {
    if (!me.data || !clinic.data) return null;
    const timeZone = clinic.data.timezone;
    return {
      user: me.data,
      clinic: clinic.data,
      canWrite: canWriteRole(me.data.role),
      isOwner: me.data.role === "owner",
      timeZone,
    };
  }, [me.data, clinic.data]);

  const error = me.error ?? clinic.error;
  // A failed background revalidation keeps the console as it is; only a first load that fails
  // replaces it with the error.
  if (error && !value && !(error instanceof ApiError && error.status === 401)) {
    return (
      <div className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center px-6">
        <ErrorState
          title="The staff console couldn't load"
          message={errorMessage(error)}
          action={
            <Button size="sm" variant="secondary" onClick={() => void Promise.all([me.mutate(), clinic.mutate()])}>
              Try again
            </Button>
          }
        />
      </div>
    );
  }
  if (value && !isValidTimeZone(value.timeZone)) {
    return (
      <div className="mx-auto flex min-h-dvh max-w-lg flex-col justify-center px-6">
        <ErrorState
          title="Clinic time zone unavailable"
          message="The clinic's time zone isn't recognised by this browser, so times can't be shown safely. Update the browser or check Settings."
        />
      </div>
    );
  }
  return (
    <SWRConfig value={{ fetcher, revalidateOnFocus: true, shouldRetryOnError: (err) => !(err instanceof ApiError && err.status < 500) }}>
      <StaffContext.Provider value={value}>
        <ToastProvider>
          <Shell value={value}>{children}</Shell>
        </ToastProvider>
      </StaffContext.Provider>
    </SWRConfig>
  );
}
