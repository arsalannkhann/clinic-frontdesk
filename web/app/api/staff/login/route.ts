import { login } from "@/lib/server/auth";

export const dynamic = "force-dynamic";

export function POST(request: Request) {
  return login(request);
}
