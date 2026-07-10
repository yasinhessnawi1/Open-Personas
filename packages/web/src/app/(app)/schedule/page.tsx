import { CalendarView } from "@/components/schedule/calendar-view";
import { unwrap } from "@/lib/api";
import { serverApi } from "@/lib/api/server";

/**
 * Spec A8 (T8) — the calendar area: time's view of the owner's personas' commitments.
 * Spec A10 (T5) — plus the user's direct create door ("New reminder").
 *
 * A sibling of the Tasks/Review area (A8-D-4) — one area family, two lenses (A6 is state's view;
 * this is time's). The client `CalendarView` renders occurrences from the engine's own API
 * (criterion 5) and edits through the same CAS door as chat (twin parity). The create dialog's
 * executor picker (A10-D-3: explicit + required) and the profile timezone anchor are fetched
 * server-side here (the calls-page precedent); both fail soft so the calendar always renders.
 */
export default async function SchedulePage() {
  const api = await serverApi();
  const [personas, profile] = await Promise.all([
    api
      .GET("/v1/personas")
      .then(unwrap)
      .catch(() => []),
    api
      .GET("/v1/me/profile")
      .then(unwrap)
      .catch(() => null),
  ]);
  return (
    <CalendarView
      personas={personas.map((p) => ({ id: p.id, name: p.name }))}
      defaultTimezone={profile?.timezone ?? null}
    />
  );
}
