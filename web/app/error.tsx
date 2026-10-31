"use client";

import { Button } from "@/components/ui/Button";

export default function ErrorPage({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="mx-auto flex min-h-[60dvh] max-w-xl flex-col items-start justify-center gap-4 px-6">
      <h1 className="font-display text-3xl">Something went wrong on this page.</h1>
      <p className="text-ink-2">Try again. If it keeps happening, call the clinic and we&apos;ll help.</p>
      <Button onClick={reset}>Try again</Button>
    </main>
  );
}
