import type { Metadata } from "next";

import { CalendarPage } from "@/components/staff/calendar/CalendarPage";

export const metadata: Metadata = { title: "Calendar" };

export default function Page() {
  return <CalendarPage />;
}
