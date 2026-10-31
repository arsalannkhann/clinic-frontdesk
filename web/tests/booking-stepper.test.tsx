import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SWRConfig } from "swr";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BookingStepper } from "@/components/public/booking/BookingStepper";
import type { PublicDoctor, PublicService } from "@/lib/api/types";
import { addDays, todayKey, zonedToIso } from "@/lib/time";

vi.mock("next/navigation", () => ({ useSearchParams: () => new URLSearchParams() }));

const IST = "Asia/Kolkata";
const SERVICES: PublicService[] = [
  { id: "s1", name: "Check-up and cleaning", description: null, duration_minutes: 45, colour: "#3a6b5c" },
  { id: "s2", name: "Tooth-coloured filling", description: null, duration_minutes: 60, colour: "#8a5a2b" },
];
const DOCTORS: PublicDoctor[] = [
  { id: "d1", name: "Dr. Kavya Iyer", public_bio: null, photo_url: null, colour: null, service_ids: ["s1", "s2"] },
  { id: "d2", name: "Dr. Arjun Rao", public_bio: null, photo_url: null, colour: null, service_ids: ["s1"] },
];

type Call = { url: string; init: RequestInit };
let calls: Call[];
let bookingResponse: () => Response;

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

beforeEach(() => {
  calls = [];
  const day = addDays(todayKey(IST), 1);
  const startsAt = zonedToIso(`${day}T10:00`, IST);
  bookingResponse = () => json({ error: { code: "idempotency_conflict", message: "Key reused." } }, 409);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      if (url.startsWith("/api/public/slots")) {
        return json({ timezone: IST, slots: [{ doctor_id: "d1", starts_at: startsAt, ends_at: startsAt }] });
      }
      if (url === "/api/public/bookings") return bookingResponse();
      return json({ error: { code: "not_found", message: "Not found" } }, 404);
    }),
  );
});
afterEach(() => vi.unstubAllGlobals());

function renderStepper() {
  return render(
    <SWRConfig value={{ provider: () => new Map(), dedupingInterval: 0 }}>
      <BookingStepper services={SERVICES} doctors={DOCTORS} timeZone={IST} callHref={null} callDisplay={null} />
    </SWRConfig>,
  );
}

describe("booking stepper keyboard", () => {
  it("does not advance on arrow keys; Enter and Space choose", async () => {
    const user = userEvent.setup();
    renderStepper();
    const first = screen.getByRole("button", { name: /Check-up and cleaning/ });
    first.focus();
    await user.keyboard("{ArrowDown}{ArrowRight}{ArrowUp}");
    expect(screen.getByRole("heading", { name: "Which treatment?" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Who would you like to see?" })).not.toBeInTheDocument();

    await user.tab();
    expect(screen.getByRole("button", { name: /Tooth-coloured filling/ })).toHaveFocus();
    await user.keyboard("{Enter}");
    const doctorHeading = await screen.findByRole("heading", { name: "Who would you like to see?" });
    await waitFor(() => expect(doctorHeading).toHaveFocus());

    await user.tab();
    expect(screen.getByRole("button", { name: /Any available dentist/ })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("heading", { name: "Who would you like to see?" })).toBeInTheDocument();
    await user.keyboard(" ");
    expect(await screen.findByRole("heading", { name: "Choose a time" })).toBeInTheDocument();
  });

  it("books with the keyboard only and treats an idempotency conflict as 'may already be booked'", async () => {
    const user = userEvent.setup();
    renderStepper();
    screen.getByRole("button", { name: /Check-up and cleaning/ }).focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("heading", { name: "Who would you like to see?" });
    await user.tab();
    await user.keyboard("{Enter}");
    await screen.findByRole("heading", { name: "Choose a time" });
    const time = await screen.findByRole("button", { name: /10:00/ });
    time.focus();
    await user.keyboard("{Enter}");
    await screen.findByRole("heading", { name: "Your details" });

    await user.tab();
    await user.keyboard("A");
    await user.tab();
    await user.keyboard("98765");
    await user.keyboard("{Enter}");
    expect(await screen.findByText("Enter the patient's full name.")).toBeInTheDocument();
    expect(calls.filter((c) => c.url === "/api/public/bookings")).toHaveLength(0);

    // Field errors clear as soon as the field changes.
    await user.type(screen.getByLabelText(/Patient's full name/), "nanya Krishnan");
    expect(screen.queryByText("Enter the patient's full name.")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText(/Mobile number/), "43210");
    await user.keyboard("{Enter}");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("We may already have your booking — check it with your reference, or try again.");
    expect(screen.getByRole("link", { name: "Check a booking with your reference" })).toHaveAttribute("href", "/booking");
    // Still on the details step: the slot was not lost.
    expect(screen.getByRole("heading", { name: "Your details" })).toBeInTheDocument();

    const keys = () =>
      calls
        .filter((c) => c.url === "/api/public/bookings")
        .map((c) => (c.init.headers as Record<string, string>)["Idempotency-Key"]);
    bookingResponse = () => json({ reference: "K7M2Q9XA", status: "confirmed", starts_at: "2026-11-03T04:30:00Z", doctor_name: "Dr. Kavya Iyer", service_name: "Check-up and cleaning" }, 202);
    await user.click(screen.getByRole("button", { name: "Book this appointment" }));
    expect(await screen.findByText("K7M2Q9XA")).toBeInTheDocument();
    const [firstKey, secondKey] = keys();
    expect(firstKey).toMatch(/^web-/);
    expect(secondKey).toMatch(/^web-/);
    expect(secondKey).not.toBe(firstKey);
  });
});
