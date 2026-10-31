import { ChatLauncher } from "@/components/public/chat/ChatWidget";
import { receptionNumber } from "@/components/public/CallLink";
import { SiteFooter } from "@/components/public/SiteFooter";
import { SiteHeader } from "@/components/public/SiteHeader";
import { getClinic } from "@/lib/server/publicData";
import { FALLBACK_PROFILE } from "@/lib/site";

export const revalidate = 300;

export default async function PublicLayout({ children }: { children: React.ReactNode }) {
  const profile = (await getClinic()) ?? FALLBACK_PROFILE;
  const number = receptionNumber(profile);
  return (
    <>
      <a
        href="#main"
        className="sr-only z-50 rounded-md bg-ink px-4 py-2 text-paper focus:not-sr-only focus:fixed focus:top-3 focus:left-3"
      >
        Skip to content
      </a>
      <SiteHeader profile={profile} />
      <main id="main" tabIndex={-1} className="focus:outline-none">
        {children}
      </main>
      <SiteFooter profile={profile} />
      <ChatLauncher
        enabled={profile.chat_enabled}
        clinicName={profile.name}
        timeZone={profile.timezone}
        callHref={number?.href ?? null}
      />
    </>
  );
}
