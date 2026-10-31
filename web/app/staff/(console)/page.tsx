import type { Metadata } from "next";

import { OverviewPage } from "@/components/staff/OverviewPage";

// The console layout's title template applies to child segments only, not to this page in the
// same segment, so the title is spelled out to match the others ("Calendar · Staff console").
export const metadata: Metadata = { title: { absolute: "Today · Staff console" } };

export default function Page() {
  return <OverviewPage />;
}
