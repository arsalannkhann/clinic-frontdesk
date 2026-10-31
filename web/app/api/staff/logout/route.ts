import { logout } from "@/lib/server/auth";

export const dynamic = "force-dynamic";

export function POST(request: Request) {
  return logout(request);
}
