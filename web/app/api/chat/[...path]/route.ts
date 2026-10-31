import { chatHandler } from "@/lib/server/routes";

export const dynamic = "force-dynamic";
// Chat replies can wait on the LLM; stay under the platform's function limit.
export const maxDuration = 30;

export const POST = chatHandler;
