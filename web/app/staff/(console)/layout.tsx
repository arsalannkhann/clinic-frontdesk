import type { Metadata } from "next";

import { StaffShell } from "@/components/staff/StaffShell";

export const metadata: Metadata = {
  title: { default: "Staff console", template: "%s · Staff console" },
  robots: { index: false, follow: false },
};

export default function ConsoleLayout({ children }: { children: React.ReactNode }) {
  return <StaffShell>{children}</StaffShell>;
}
