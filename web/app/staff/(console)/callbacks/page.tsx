import type { Metadata } from "next";

import { CallbacksPage } from "@/components/staff/CallbacksPage";

export const metadata: Metadata = { title: "Call-backs" };

export default function Page() {
  return <CallbacksPage />;
}
