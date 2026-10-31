import { act, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({}), usePathname: () => "/staff", useSearchParams: () => new URLSearchParams() }));

import { cancelConfirmation } from "@/components/staff/AppointmentDrawer";
import { firstFreeMinute } from "@/components/staff/calendar/CalendarPage";
import { startsIn } from "@/components/staff/OverviewPage";
import { useConfirm } from "@/components/ui/ConfirmDialog";
import type { CalendarAppointment, ScheduleException } from "@/lib/api/types";

function Harness({ onAnswer }: { onAnswer: (yes: boolean) => void }) {
  const [confirm, dialog] = useConfirm();
  const [asked, setAsked] = useState(0);
  return (
    <>
      <button
        type="button"
        onClick={async () => {
          setAsked((n) => n + 1);
          onAnswer(await confirm({ title: "Call Asha now?", body: "This places a call.", confirmLabel: "Call now", cancelLabel: "Not now", tone: "primary" }));
        }}
      >
        Ask {asked}
      </button>
      {dialog}
    </>
  );
}

describe("confirm dialog", () => {
  it("is an alert dialog that starts on the safe choice and resolves the answer", async () => {
    const answers: boolean[] = [];
    render(<Harness onAnswer={(yes) => answers.push(yes)} />);

    fireEvent.click(screen.getByRole("button", { name: /Ask/ }));
    const dialog = await screen.findByRole("alertdialog", { name: "Call Asha now?" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(document.activeElement?.textContent).toBe("Not now");

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Not now" })));
    expect(answers).toEqual([false]);

    fireEvent.click(screen.getByRole("button", { name: /Ask/ }));
    await act(async () => fireEvent.click(await screen.findByRole("button", { name: "Call now" })));
    expect(answers).toEqual([false, true]);
  });

  it("describes what a cancellation does", () => {
    const options = cancelConfirmation({ starts_at: "2026-11-02T04:30:00Z", patient_name: "Asha Rao", service_name: "Cleaning" }, "Asia/Kolkata");
    expect(options.title).toMatch(/Cancel this appointment/);
    expect(String(options.body)).toMatch(/Cleaning for Asha Rao .* at 10:00/);
    expect(options.cancelLabel).toBe("Keep it");
  });
});

describe("calendar booking defaults", () => {
  const zone = "Asia/Kolkata";
  const window = { start: 9 * 60, end: 11 * 60 };
  const appt = (starts: string, ends: string, status: CalendarAppointment["status"] = "confirmed"): CalendarAppointment => ({
    id: starts,
    doctor_id: "d1",
    service_id: "s1",
    service_name: "Cleaning",
    colour: null,
    starts_at: starts,
    ends_at: ends,
    status,
    sync_state: "synced",
    channel: "web",
    patient_name: null,
    reference: null,
  });

  it("picks the first free slot, skipping bookings, blocked time and the past", () => {
    // 09:00-09:30 IST booked, 09:30-09:45 a break.
    const booked = [appt("2026-11-02T03:30:00Z", "2026-11-02T04:00:00Z")];
    const breaks: ScheduleException[] = [{ id: "x", doctor_id: "d1", kind: "break", starts_at: "2026-11-02T04:00:00Z", ends_at: "2026-11-02T04:15:00Z", label: "" }];
    expect(firstFreeMinute("2026-11-02", "d1", zone, window, booked, breaks, null)).toBe(9 * 60 + 45);
    // Cancelled bookings and other dentists' bookings don't block.
    expect(firstFreeMinute("2026-11-02", "d1", zone, window, [appt("2026-11-02T03:30:00Z", "2026-11-02T04:00:00Z", "cancelled")], [], null)).toBe(9 * 60);
    expect(firstFreeMinute("2026-11-02", "d2", zone, window, booked, [], null)).toBe(9 * 60);
    // At 10:05 IST the first start is 10:15.
    expect(firstFreeMinute("2026-11-02", "d2", zone, window, [], [], Date.parse("2026-11-02T04:35:00Z"))).toBe(10 * 60 + 15);
    expect(firstFreeMinute("2026-11-02", "d2", zone, window, [], [], Date.parse("2026-11-03T00:00:00Z"))).toBeNull();
    // A grid stretched to midnight by an all-day block still starts bookings at 7 am.
    expect(firstFreeMinute("2026-11-02", "d2", zone, { start: 0, end: 24 * 60 }, [], [], null)).toBe(7 * 60);
  });

  it("says when the next patient is due", () => {
    const now = Date.parse("2026-11-02T04:00:00Z");
    expect(startsIn("2026-11-02T04:20:00Z", now)).toBe("in 20 min");
    expect(startsIn("2026-11-02T05:30:00Z", now)).toBe("in 1 h 30 min");
    expect(startsIn("2026-11-02T03:59:00Z", now)).toBe("now");
  });
});
