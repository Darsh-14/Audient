// Real implementations of some tools, run in the browser and handed to the agent's tool environment.
// Everything else stays simulated (see audient/harness/mock_env.py).
//   get_weather  Open-Meteo: geocoding, then the daily forecast for up to 16 days.
//   find_places  OpenStreetMap (Overpass API): the nearest places of a kind around you or a named area.
//   get_route    OpenStreetMap: OSRM driving route (time and distance) from where you are.
// Your location is read only when one of these needs it (the browser asks first); the coordinates go to
// these OpenStreetMap services and nowhere else. None of them needs a key.

const WMO = {
  0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
  51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
  61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
  71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
  82: "heavy rain showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorms", 96: "thunderstorms with hail",
  99: "thunderstorms with hail",
};

async function json(url, init) {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

const localDay = (d = new Date()) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

export async function getWeather({ location, date }) {
  const geo = await json(`https://geocoding-api.open-meteo.com/v1/search?name=${encodeURIComponent(location)}&count=1&language=en&format=json`);
  const place = geo.results?.[0];
  if (!place) return { found: false, location, source: "open-meteo" };
  const f = await json(`https://api.open-meteo.com/v1/forecast?latitude=${place.latitude}&longitude=${place.longitude}` +
    "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max" +
    "&current=temperature_2m&timezone=auto&forecast_days=16");
  const day = date || localDay();
  const i = f.daily.time.indexOf(day);
  const name = place.admin1 && place.admin1 !== place.name ? `${place.name}, ${place.admin1}` : place.name;
  if (i < 0) return { found: false, location: name, date: day, source: "open-meteo" };
  const out = { location: name, date: day, condition: WMO[f.daily.weather_code[i]] || "mixed weather",
                high_c: Math.round(f.daily.temperature_2m_max[i]), low_c: Math.round(f.daily.temperature_2m_min[i]) };
  const rain = f.daily.precipitation_probability_max?.[i];
  if (rain != null) out.rain_chance_pct = rain;
  if (i === 0 && f.current) out.now_c = Math.round(f.current.temperature_2m);
  out.source = "open-meteo";
  return out;
}

// ---------------------------------------------------------------- where the user is
const NO_LOCATION = "I need your location for that. Allow location access for this page, or name an area, like “near Koregaon Park”.";
let fix = null;
function here() {
  if (fix && performance.now() - fix.at < 120000) return Promise.resolve(fix);
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) return reject(new Error("no geolocation"));
    navigator.geolocation.getCurrentPosition(
      (p) => { fix = { lat: p.coords.latitude, lon: p.coords.longitude, at: performance.now() }; resolve(fix); },
      reject, { enableHighAccuracy: false, timeout: 10000, maximumAge: 120000 });
  });
}

// a named place or area, preferring matches near the user
async function geocode(q, near) {
  const box = near ? `&viewbox=${near.lon - 0.4},${near.lat + 0.4},${near.lon + 0.4},${near.lat - 0.4}` : "";
  const res = await json(`https://nominatim.openstreetmap.org/search?q=${encodeURIComponent(q)}&format=jsonv2&limit=1${box}`);
  const r = res[0];
  return r ? { lat: +r.lat, lon: +r.lon, name: r.name || String(r.display_name).split(",")[0] } : null;
}

// ---------------------------------------------------------------- nearby places (OpenStreetMap tags)
const KIND_TAGS = {
  cinema: ['["amenity"="cinema"]'], restaurant: ['["amenity"="restaurant"]'], cafe: ['["amenity"="cafe"]'],
  hospital: ['["amenity"="hospital"]'], clinic: ['["amenity"="clinic"]', '["amenity"="doctors"]'],
  pharmacy: ['["amenity"="pharmacy"]'], atm: ['["amenity"="atm"]'], bank: ['["amenity"="bank"]'], fuel: ['["amenity"="fuel"]'],
  "charging station": ['["amenity"="charging_station"]'], police: ['["amenity"="police"]'], supermarket: ['["shop"="supermarket"]'],
  mall: ['["shop"="mall"]'], park: ['["leisure"="park"]'], hotel: ['["tourism"="hotel"]'], parking: ['["amenity"="parking"]'],
  gym: ['["leisure"="fitness_centre"]'], temple: ['["amenity"="place_of_worship"]["religion"="hindu"]'],
  mosque: ['["amenity"="place_of_worship"]["religion"="muslim"]'], church: ['["amenity"="place_of_worship"]["religion"="christian"]'],
  school: ['["amenity"="school"]'], college: ['["amenity"="college"]', '["amenity"="university"]'], "bus stop": ['["highway"="bus_stop"]'],
  "metro station": ['["station"="subway"]'], "railway station": ['["railway"="station"]["station"!="subway"]'],
  airport: ['["aeroway"="aerodrome"]["iata"]'], bakery: ['["shop"="bakery"]'], bar: ['["amenity"="bar"]'], pub: ['["amenity"="pub"]'],
  museum: ['["tourism"="museum"]'], library: ['["amenity"="library"]'], "post office": ['["amenity"="post_office"]'],
  toilet: ['["amenity"="toilets"]'],
};
// the same words the agent's rule parser knows (audient/nlu.py PLACE_KINDS), for kinds a language model words differently
const KIND_ALIASES = {
  "movie theatre": "cinema", "movie theater": "cinema", theatre: "cinema", theater: "cinema", multiplex: "cinema",
  "coffee shop": "cafe", chemist: "pharmacy", "medical store": "pharmacy", "petrol pump": "fuel", "petrol station": "fuel",
  "gas station": "fuel", "fuel station": "fuel", "ev charger": "charging station", "police station": "police",
  "grocery store": "supermarket", washroom: "toilet", "place to eat": "restaurant",
};
export function canonicalKind(text) {
  let t = String(text || "").toLowerCase().trim().replace(/^(?:the\s+)?(?:nearest|closest|nearby)\s+/, "");
  for (const k of [t, t.replace(/(?:es|s)$/, "")]) {
    if (KIND_TAGS[k]) return k;
    if (KIND_ALIASES[k]) return KIND_ALIASES[k];
  }
  return null;
}

const km = (a, b) => {
  const R = 6371, r = Math.PI / 180, dLat = (b.lat - a.lat) * r, dLon = (b.lon - a.lon) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a.lat * r) * Math.cos(b.lat * r) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
};
let lastPlaces = [];  // so "take me to the first one" / "navigate to <name>" can route to what was just listed

async function nearest(kind, from, n = 3) {
  const filters = KIND_TAGS[kind];
  for (const radius of kind === "airport" ? [60000] : [4000, 12000]) {
    const q = `[out:json][timeout:20];(${filters.map((f) => `nwr${f}(around:${radius},${from.lat},${from.lon});`).join("")});out center tags 80;`;
    const data = await json("https://overpass-api.de/api/interpreter", { method: "POST", body: "data=" + encodeURIComponent(q),
      headers: { "Content-Type": "application/x-www-form-urlencoded" } });
    const found = (data.elements || []).map((e) => {
      const lat = e.lat ?? e.center?.lat, lon = e.lon ?? e.center?.lon, t = e.tags || {};
      return { name: t.name || t["name:en"], lat, lon, area: t["addr:suburb"] || t["addr:street"] || t["addr:city"] };
    }).filter((p) => p.lat != null && p.name);
    if (found.length) {
      return found.map((p) => ({ ...p, d: km(from, p) })).sort((a, b) => a.d - b.d).slice(0, n);
    }
  }
  return [];
}

export async function findPlaces({ kind, near }) {
  const k = canonicalKind(kind);
  let from;
  try { from = near ? await geocode(near) : await here(); } catch { return { found: false, kind, reason: NO_LOCATION }; }
  if (!from) return { found: false, kind, reason: `I couldn't find ${near} on the map.` };
  let hits;
  if (k) {
    hits = await nearest(k, from);
  } else {  // a kind of place the tag list doesn't cover: ask the map by name around the user
    const g = await geocode(kind, from);
    hits = g ? [{ ...g, d: km(from, g) }] : [];
  }
  if (!hits.length) return { found: false, kind, reason: `I couldn't find a ${k || kind} within 12 km${near ? ` of ${near}` : ""}.` };
  lastPlaces = hits;
  const out = { kind: k || kind, places: hits.map((p) => ({ name: p.name, distance_km: Math.round(p.d * 10) / 10, ...(p.area ? { area: p.area } : {}) })) };
  if (near) out.near = from.name || near;
  out.source = "openstreetmap";
  return out;
}

export async function getRoute({ destination }) {
  const d = String(destination || "").trim(), dl = d.toLowerCase();
  if (/^(?:my\s+)?(?:home|work|office|house|workplace)$/.test(dl)) {
    return { found: false, destination: d, reason: `I don't have your ${dl.replace(/^my\s+/, "")} address saved, so I can't route there yet. Tell me the place or the area.` };
  }
  let from;
  try { from = await here(); } catch { return { found: false, destination: d, reason: NO_LOCATION }; }
  let to = lastPlaces.find((p) => dl.includes(p.name.toLowerCase()) || p.name.toLowerCase().includes(dl));
  const kind = to ? null : canonicalKind(dl);
  if (kind) {
    const n = (await nearest(kind, from, 1))[0];
    if (n) to = { ...n, label: n.name.toLowerCase().includes(kind) ? n.name : `${n.name}, the nearest ${kind}` };
  }
  if (!to) to = await geocode(d, from);
  if (!to) return { found: false, destination: d, reason: `I couldn't find ${d} on the map.` };
  const r = await json(`https://router.project-osrm.org/route/v1/driving/${from.lon},${from.lat};${to.lon},${to.lat}?overview=false`);
  const route = r.routes?.[0];
  if (!route) return { found: false, destination: d, reason: `I couldn't find a driving route to ${to.name}.` };
  const out = { destination: to.label || to.name, eta_min: Math.max(1, Math.round(route.duration / 60)),
                distance_km: Math.round(route.distance / 100) / 10 };
  if (route.legs?.[0]?.summary) out.via = route.legs[0].summary;
  out.source = "osrm";
  return out;
}

export const LIVE_TOOLS = { get_weather: getWeather, find_places: findPlaces, get_route: getRoute };
