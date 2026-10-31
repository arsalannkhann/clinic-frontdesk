import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { SWRConfig } from "swr";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({}), usePathname: () => "/staff", useSearchParams: () => new URLSearchParams() }));

import { CalendarPage, TRUNCATED_NOTICE } from "@/components/staff/calendar/CalendarPage";
import { CallbacksPage, callOutcomeNote, canReopen, reopenFailure } from "@/components/staff/CallbacksPage";
import { StaffContext, type StaffContextValue } from "@/components/staff/StaffContext";
import { ToastProvider } from "@/components/ui/Toast";
import { ApiError, fetcher } from "@/lib/api/client";
import type { CallbackItem, ClinicConfig } from "@/lib/api/types";
import { CALL_OUTCOMES, LAST_CALL_FAILED, REOPEN_FAILURES } from "@/lib/labels";

const IST = "Asia/Kolkata";
const ID = "0b5d1c1e-7a52-4f0e-9a51-3c1a8f0d0001";

const staff = (role: "owner" | "viewer" = "owner"): StaffContextValue => ({
  user: { id: "u1", email: "owner@asterdental.example", role },
  clinic: { id: "c1", name: "Aster", timezone: IST, outbound_enabled: true } as ClinicConfig,
  canWrite: role !== "viewer",
  isOwner: role === "owner",
  timeZone: IST,
});

function Shell({ children, value = staff() }: { children: ReactNode; value?: StaffContextValue }) {
  return (
    <SWRConfig value={{ provider: () => new Map(), fetcher, dedupingInterval: 0, shouldRetryOnError: false }}>
      <StaffContext.Provider value={value}>
        <ToastProvider>{children}</ToastProvider>
      </StaffContext.Provider>
    </SWRConfig>
  );
}

const callback = (over: Partial<CallbackItem> = {}): CallbackItem => ({
  id: ID,
  name: "Asha Rao",
  phone_masked: "****1234",
  reason: "Question about a crown",
  source: "web",
  status: "calling",
  created_at: "2026-10-31T04:00:00Z",
  resolved_at: null,
  execution_id: null,
  call_requested_at: "2026-10-31T04:30:00Z",
  call_outcome: "unknown",
  synthetic: false,
  ...over,
});

type Call = { url: string; init: RequestInit };
let calls: Call[];
let handle: (call: Call) => Response | undefined;
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  calls = [];
  handle = () => undefined;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const call = { url: String(url), init };
      calls.push(call);
      return handle(call) ?? json({ error: { code: "not_found", message: "Not found" } }, 404);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

describe("call outcomes and reopen", () => {
  it("explains each outcome of a call in progress, and a failed last call on an open request", () => {
    expect(callOutcomeNote({ status: "calling", call_outcome: "unknown" })).toBe(
      "We couldn't confirm the call went through — check with the patient or reopen to try again.",
    );
    expect(callOutcomeNote({ status: "calling", call_outcome: "placed" })).toBe(CALL_OUTCOMES.placed);
    expect(callOutcomeNote({ status: "calling", call_outcome: "failed" })).toBe(CALL_OUTCOMES.failed);
    expect(callOutcomeNote({ status: "calling", call_outcome: null })).toBeNull();
    expect(callOutcomeNote({ status: "open", call_outcome: "failed" })).toBe(LAST_CALL_FAILED);
    expect(callOutcomeNote({ status: "done", call_outcome: "placed" })).toBeNull();
    // An older API that does not send call_outcome yet.
    expect(callOutcomeNote({ status: "calling" })).toBeNull();
  });

  it("offers Reopen only to writers, on a call with an outcome", () => {
    for (const outcome of ["placed", "unknown", "failed"] as const) {
      expect(canReopen({ status: "calling", call_outcome: outcome }, true)).toBe(true);
      expect(canReopen({ status: "calling", call_outcome: outcome }, false)).toBe(false);
    }
    expect(canReopen({ status: "calling", call_outcome: null }, true)).toBe(false);
    expect(canReopen({ status: "open", call_outcome: "failed" }, true)).toBe(false);
    expect(canReopen({ status: "done", call_outcome: "placed" }, true)).toBe(false);
  });

  it("explains a refused reopen and refreshes when the request moved on", () => {
    expect(reopenFailure(new ApiError(409, "call_in_progress", "x"))).toEqual({ text: REOPEN_FAILURES.call_in_progress, refresh: false });
    expect(reopenFailure(new ApiError(409, "callback_resolved", "x"))).toEqual({ text: REOPEN_FAILURES.callback_resolved, refresh: true });
    expect(reopenFailure(new ApiError(404, "request_failed", "x"))).toEqual({ text: REOPEN_FAILURES.not_found, refresh: true });
    expect(reopenFailure(new ApiError(503, "unavailable", "Try again shortly."))).toEqual({ text: "Try again shortly.", refresh: false });
  });

  it("shows the outcome and reopens after confirming, without dialling", async () => {
    const user = userEvent.setup();
    let reopened = false;
    handle = (c) => {
      if (c.url.startsWith("/api/staff/outbound-status")) return json({ enabled: true, has_from_number: true, has_outbound_agent: true });
      if (c.url === "/api/staff/callbacks?status=open") return json(reopened ? [callback({ status: "open", call_outcome: null, call_requested_at: null })] : []);
      if (c.url === "/api/staff/callbacks?status=calling") return json(reopened ? [] : [callback()]);
      if (c.url === `/api/staff/callbacks/${ID}/reopen`) {
        reopened = true;
        return json({ id: ID, status: "open" });
      }
      return undefined;
    };
    render(
      <Shell>
        <CallbacksPage />
      </Shell>,
    );
    await user.click(await screen.findByRole("tab", { name: "Calling" }));
    expect(await screen.findByText(CALL_OUTCOMES.unknown!)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Reopen" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(within(dialog).getByText(/Nothing is dialled/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Keep as is" }));
    expect(calls.some((c) => c.url.endsWith("/reopen"))).toBe(false);

    await user.click(screen.getByRole("button", { name: "Reopen" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Reopen" }));
    await waitFor(() => expect(calls.filter((c) => c.url.endsWith("/reopen"))).toHaveLength(1));
    const reopen = calls.find((c) => c.url.endsWith("/reopen"))!;
    expect(reopen.init.method).toBe("POST");
    expect(calls.some((c) => c.url.endsWith("/call"))).toBe(false);
    expect(await screen.findByText(/back under Open/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(CALL_OUTCOMES.unknown!)).not.toBeInTheDocument());
  });

  it("shows a refused reopen as a message and keeps the request", async () => {
    const user = userEvent.setup();
    handle = (c) => {
      if (c.url.startsWith("/api/staff/outbound-status")) return json({ enabled: true, has_from_number: true, has_outbound_agent: true });
      if (c.url.startsWith("/api/staff/callbacks?status=")) return json(c.url.endsWith("calling") ? [callback({ call_outcome: "placed" })] : []);
      if (c.url.endsWith("/reopen")) return json({ error: { code: "call_in_progress", message: "The call is still being placed" } }, 409);
      return undefined;
    };
    render(
      <Shell>
        <CallbacksPage />
      </Shell>,
    );
    await user.click(await screen.findByRole("tab", { name: "Calling" }));
    expect(await screen.findByText(CALL_OUTCOMES.placed!)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reopen" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: "Reopen" }));
    expect(await screen.findByText(REOPEN_FAILURES.call_in_progress!)).toBeInTheDocument();
    expect(screen.getByText(CALL_OUTCOMES.placed!)).toBeInTheDocument();
  });

  it("shows viewers the outcome but no Reopen", async () => {
    const user = userEvent.setup();
    handle = (c) => (c.url.startsWith("/api/staff/callbacks?status=") ? json(c.url.endsWith("calling") ? [callback({ name: null })] : []) : undefined);
    render(
      <Shell value={staff("viewer")}>
        <CallbacksPage />
      </Shell>,
    );
    await user.click(await screen.findByRole("tab", { name: "Calling" }));
    expect(await screen.findByText(CALL_OUTCOMES.unknown!)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen" })).not.toBeInTheDocument();
  });
});

describe("calendar truncation", () => {
  const calendar = (truncated?: boolean) => ({
    timezone: IST,
    doctors: [{ id: "d1", name: "Dr. Kavya Iyer", colour: null }],
    appointments: [],
    exceptions: [],
    ...(truncated === undefined ? {} : { truncated }),
  });

  async function show(body: unknown) {
    handle = (c) => {
      if (c.url.startsWith("/api/staff/calendar?")) return json(body);
      if (c.url.startsWith("/api/admin/") || c.url.startsWith("/api/public/")) return json([]);
      return undefined;
    };
    render(
      <Shell>
        <CalendarPage />
      </Shell>,
    );
    await waitFor(() => expect(calls.some((c) => c.url.startsWith("/api/staff/calendar?"))).toBe(true));
  }

  it("says quietly when the range held more appointments than one read returns", async () => {
    await show(calendar(true));
    expect(await screen.findByText(TRUNCATED_NOTICE)).toBeInTheDocument();
    expect(TRUNCATED_NOTICE).toBe("Showing the first 2,000 appointments — narrow the range");
  });

  it("shows no notice when everything fits, or the field is absent", async () => {
    await show(calendar(false));
    expect(await screen.findAllByText(/Dr. Kavya Iyer/)).not.toHaveLength(0);
    expect(screen.queryByText(TRUNCATED_NOTICE)).not.toBeInTheDocument();
  });
});
