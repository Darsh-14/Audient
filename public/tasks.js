// Plain-language names for tool calls ("Book flight UK-406 · 3 passengers"), shared by the live view
// and the benchmark replays. Unknown tools fall back to a readable version of their name.
export const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;

function day(iso) {
  const d = new Date(`${iso}T00:00`);
  return isNaN(d) ? iso : d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

export function taskTitle(c) {
  const a = c.args || {};
  switch (c.tool) {
    case "search_flights": return [`Search flights ${a.origin ?? "?"} → ${a.destination ?? "?"}`, [a.date && day(a.date), a.passengers && plural(a.passengers, "passenger")]];
    case "book_flight": return [`Book flight ${a.flight_id}`, [a.passengers && plural(a.passengers, "passenger")]];
    case "create_ticket": return ["Open a support ticket", [a.device, a.priority && `${a.priority} priority`]];
    case "get_route": return [`Route to the ${a.destination}`.replace("the the", "the"), []];
    case "lookup_manual": return [`Check the ${a.device_model ?? "device"} manual`, [a.error_code && `error ${a.error_code}`, a.indicator && `${a.indicator.replaceAll("_", " ")} light`]];
    case "reserve_table": return [`Reserve a table at ${a.restaurant}`, [a.party_size && plural(a.party_size, "guest"), a.time]];
    default: {
      const words = c.tool.replaceAll("_", " ");
      return [words[0].toUpperCase() + words.slice(1), Object.values(a).slice(0, 3).map(String)];
    }
  }
}

export function taskOutcome(res) {
  if (!res || typeof res !== "object") return "";
  const list = Object.values(res).find((v) => Array.isArray(v));
  if (list) return plural(list.length, "option") + " found";
  if (res.booking_ref) return `Booked · ${res.booking_ref}`;
  if (res.reservation_id) return `Reserved · ${res.reservation_id}`;
  if (res.ticket_id) return `Ticket ${res.ticket_id}`;
  if (res.eta_min) return `${res.eta_min} min · ${res.distance_km} km`;
  if (res.found === false) return "Not in the manual";
  if (res.instructions) return res.section || "Found in the manual";
  return "";
}
