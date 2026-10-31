import Link from "next/link";

import { AsterMark } from "@/components/public/Brand";

export default function NotFound() {
  return (
    <main className="mx-auto flex min-h-dvh max-w-xl flex-col items-start justify-center gap-5 px-6">
      <AsterMark className="size-10" />
      <h1 className="font-display text-4xl">This page isn&apos;t here.</h1>
      <p className="text-lg text-ink-2">The link may be old, or the page may have moved.</p>
      <div className="flex gap-4">
        <Link href="/" className="font-semibold text-accent hover:underline">
          Go to the clinic home page
        </Link>
        <Link href="/book" className="font-semibold text-ink-2 hover:underline">
          Book online
        </Link>
      </div>
    </main>
  );
}
