import type { Metadata } from "next";

import { OverviewPage } from "@/components/staff/OverviewPage";

export const metadata: Metadata = { title: "Today" };

export default function Page() {
  return <OverviewPage />;
}
