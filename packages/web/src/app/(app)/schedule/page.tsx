import { CalendarView } from "@/components/schedule/calendar-view";

/**
 * Spec A8 (T8) — the calendar area: time's view of the owner's personas' commitments.
 *
 * A sibling of the Tasks/Review area (A8-D-4) — one area family, two lenses (A6 is state's view;
 * this is time's). The client `CalendarView` renders occurrences from the engine's own API
 * (criterion 5) and edits through the same CAS door as chat (twin parity).
 */
export default function SchedulePage() {
  return <CalendarView />;
}
