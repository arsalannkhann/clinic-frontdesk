import type { Metadata } from "next";

import { AppointmentsPage } from "@/components/staff/AppointmentsPage";

export const metadata: Metadata = { title: "Appointments" };

export default function Page() {
  return <AppointmentsPage />;
}
