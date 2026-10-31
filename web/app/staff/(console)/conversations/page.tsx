import type { Metadata } from "next";

import { ConversationsPage } from "@/components/staff/ConversationsPage";

export const metadata: Metadata = { title: "Conversations" };

export default function Page() {
  return <ConversationsPage />;
}
