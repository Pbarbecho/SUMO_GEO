/* SUMO-GEO frontend — MapLibre GL (basemap + extruded buildings) with a
 * deck.gl overlay for road-congestion colouring and live vehicles streamed
 * over a WebSocket. Everything runs in the browser, no build step. */

const API = "";                                   // same-origin (nginx proxies /api and /ws)
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://")
  + location.host + "/ws/live";

const els = {
  play: document.getElementById("btn-play"),
  reset: document.getElementById("btn-reset"),
  level: document.getElementById("level"),
  fps: document.getElementById("fps"),
  fpsVal: document.getElementById("fps-val"),
  pad: document.getElementById("pantilt"),
  padKnob: document.getElementById("pad-knob"),
  ptRead: document.getElementById("pt-read"),
  buildings: document.getElementById("toggle-buildings"),
  congestion: document.getElementById("toggle-congestion"),
  tl: document.getElementById("toggle-tl"),
  poi: document.getElementById("toggle-poi"),
  busy: document.getElementById("toggle-busy"),
  busyMin: document.getElementById("busy-min"),
  busyVal: document.getElementById("busy-val"),
  orbit: document.getElementById("orbit"),
  viewTop: document.getElementById("view-top"),
  zoomIn: document.getElementById("zoom-in"),
  zoomOut: document.getElementById("zoom-out"),
  vehCount: document.getElementById("veh-count"),
  simTime: document.getElementById("sim-time"),
  conn: document.getElementById("conn"),
  tint: document.getElementById("tint"),
  light_dawn: document.getElementById("light-dawn"),
  light_day: document.getElementById("light-day"),
  light_dusk: document.getElementById("light-dusk"),
  light_night: document.getElementById("light-night"),
};

let map, overlay, ws;
let networkGeo = { type: "FeatureCollection", features: [] };
let laneLinesGeo = { type: "FeatureCollection", features: [] };  // edges with 2+ lanes
let edgeColors = {};      // edgeId -> [r,g,b]
let edgeCounts = {};      // edgeId -> vehicle count (n) in the latest frame
let edgeMid = {};         // edgeId -> [lon,lat] midpoint (for the busy-street icons)
let vehicles = [];        // latest frame vehicles
let tlDefs = { type: "FeatureCollection", features: [] };  // static signal positions
let tlState = {};         // tlsID -> live state string ("GrGr...")
let paused = false;
let showCongestion = true;
let showTL = true;
let showBusy = false;       // icon markers on streets with >= busyMin vehicles
let busyMin = 2;            // busy-street threshold (from the selector)
let currentLevel = "mid";   // scenario level (low/mid/high) from the mapa/ files
let currentPreset = "day";  // light preset (dawn/day/dusk/night) — default day
let asphaltColor = [58, 62, 70];  // free-flow road colour, set per light preset

function hexToRgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
}

// --- realistic vehicles ------------------------------------------------------
// Drawn as oriented, extruded boxes at their true footprint (car vs. bus),
// coloured with a believable car palette; buses in transit orange. Congestion
// is carried by the road colour (LOS), so vehicle colour stays realistic.
let labelLayerId = null;   // first basemap symbol layer -> draw data beneath labels
const CAR_COLORS = [
  [236, 237, 240], [30, 33, 38], [180, 184, 190], [108, 114, 122],
  [178, 44, 44], [40, 74, 142], [26, 40, 62], [156, 128, 74], [64, 112, 86],
];
const BUS_COLOR = [250, 150, 20];
function hashId(s) { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0; return h; }
function isBus(d) { return /bus|coach|tram/i.test(d.type || ""); }
function vehicleColor(d) {
  return isBus(d) ? BUS_COLOR : CAR_COLORS[hashId(d.id) % CAR_COLORS.length];
}
function darken(c, f) { return [Math.round(c[0] * f), Math.round(c[1] * f), Math.round(c[2] * f)]; }
const GLASS = [32, 38, 50];        // windshield / windows
const HEADLIGHT = [255, 240, 205];
const TAILLIGHT = [214, 42, 38];

// A rectangle part, oriented by SUMO heading, centred `alongFrac`·L forward from
// the vehicle centre, sized `lenFrac`·L × `widFrac`·W. (0=N, clockwise.)
function partRect(d, L, W, alongFrac, lenFrac, widFrac) {
  const h = d.angle || 0;
  const [cx, cy] = offsetLonLat(d.lon, d.lat, h, alongFrac * L);
  const hl = (lenFrac * L) / 2, hw = (widFrac * W) / 2;
  const corner = (sl, sw) => {
    const [x, y] = offsetLonLat(cx, cy, h, sl * hl);
    return offsetLonLat(x, y, (h + 90) % 360, sw * hw);
  };
  return [corner(1, -1), corner(1, 1), corner(-1, 1), corner(-1, -1)];
}

// Procedural 3D vehicle: several extruded parts (chassis, greenhouse/windshield,
// roof, head/tail lights) at real size — 100% offline, perfect orientation.
function vehicleParts(d) {
  const L = d.len || (isBus(d) ? 12 : 4.5);
  const W = d.wid || (isBus(d) ? 2.55 : 1.8);
  const body = vehicleColor(d);
  const roof = darken(body, 0.82);
  if (isBus(d)) {
    return [
      { polygon: partRect(d, L, W, 0.00, 1.00, 1.00), height: 2.6, color: body },       // chassis
      { polygon: partRect(d, L, W, 0.00, 0.96, 0.90), height: 2.8, color: roof },        // roof cap
      { polygon: partRect(d, L, W, 0.46, 0.06, 0.90), height: 2.2, color: GLASS },       // windshield
      { polygon: partRect(d, L, W, 0.49, 0.02, 0.94), height: 1.0, color: HEADLIGHT },   // headlights
      { polygon: partRect(d, L, W, -0.49, 0.02, 0.94), height: 1.0, color: TAILLIGHT },  // taillights
    ];
  }
  return [
    { polygon: partRect(d, L, W, 0.00, 1.00, 1.00), height: 0.85, color: body },         // chassis
    { polygon: partRect(d, L, W, -0.06, 0.56, 0.86), height: 1.24, color: GLASS },       // greenhouse / windshield
    { polygon: partRect(d, L, W, -0.08, 0.48, 0.76), height: 1.44, color: roof },        // roof
    { polygon: partRect(d, L, W, 0.47, 0.05, 0.84), height: 0.62, color: HEADLIGHT },    // headlights
    { polygon: partRect(d, L, W, -0.48, 0.045, 0.84), height: 0.64, color: TAILLIGHT },  // taillights
  ];
}

// --- light presets (emulate Mapbox Standard's lightPreset in MapLibre) -------
// Each preset drives the sky/atmosphere, the 3D light, the building & asphalt
// colours, and a full-scene tint — so the whole map reads as that time of day.
const LIGHT_PRESETS = {
  dawn: {
    sky: { "sky-color": "#9ab4de", "horizon-color": "#f5c9a8", "fog-color": "#e6d4cc",
           "sky-horizon-blend": 0.7, "horizon-fog-blend": 0.5, "fog-ground-blend": 0.4, "atmosphere-blend": 0.8 },
    skyCss: "linear-gradient(to bottom, #6d86c0 0%, #a9a6c4 55%, #f5c9a8 100%)",
    light: { color: "#ffe6c7", intensity: 0.4, position: [1.4, 90, 18] },
    buildings: [0, "#cbc7c1", 12, "#c3beb4", 25, "#b6b0a4", 45, "#a49d90", 80, "#8f8778"],
    asphalt: [66, 70, 80], tint: "linear-gradient(to top, rgba(255,180,120,.18), rgba(120,130,175,.12))", blend: "soft-light",
  },
  day: {
    sky: { "sky-color": "#8fb6e8", "horizon-color": "#cfe0f2", "fog-color": "#e8eef5",
           "sky-horizon-blend": 0.8, "horizon-fog-blend": 0.4, "fog-ground-blend": 0.3, "atmosphere-blend": 0.6 },
    skyCss: "linear-gradient(to bottom, #7fb0ea 0%, #a8c9ef 60%, #dfeaf5 100%)",
    light: { color: "#ffffff", intensity: 0.4, position: [1.2, 210, 30] },
    buildings: [0, "#f1e5db", 25, "#ece0d2", 60, "#e3d7c8", 90, "#d8ccbc"],
    asphalt: [179, 187, 211], tint: "transparent", blend: "normal",
  },
  dusk: {
    sky: { "sky-color": "#38265a", "horizon-color": "#ff8a4c", "fog-color": "#d1774d",
           "sky-horizon-blend": 0.55, "horizon-fog-blend": 0.6, "fog-ground-blend": 0.5, "atmosphere-blend": 0.95 },
    skyCss: "linear-gradient(to bottom, #241a3a 0%, #5a2f63 45%, #ff8a4c 100%)",
    light: { color: "#ff9d5c", intensity: 0.5, position: [1.5, 250, 12] },
    buildings: [0, "#c08a5f", 20, "#8f6f6a", 45, "#5f4f5e", 80, "#3f3550"],
    asphalt: [48, 42, 56], tint: "linear-gradient(to top, rgba(255,120,50,.30), rgba(42,26,64,.42))", blend: "multiply",
  },
  night: {
    sky: { "sky-color": "#070c20", "horizon-color": "#26335c", "fog-color": "#16233f",
           "sky-horizon-blend": 0.5, "horizon-fog-blend": 0.6, "fog-ground-blend": 0.6, "atmosphere-blend": 1.0 },
    skyCss: "linear-gradient(to bottom, #050a1c 0%, #101836 55%, #26335c 100%)",
    light: { color: "#9fb4ff", intensity: 0.25, position: [1.5, 200, 60] },
    buildings: [0, "#3c4157", 20, "#31374c", 45, "#282c41", 80, "#202338"],
    asphalt: [30, 32, 45], tint: "linear-gradient(to top, rgba(14,20,46,.55), rgba(8,12,30,.70))", blend: "multiply",
  },
};

function applyLightPreset(name) {
  const p = LIGHT_PRESETS[name];
  if (!p || !map) return;
  currentPreset = name;
  try { map.setSky(p.sky); } catch (e) { /* older MapLibre: CSS sky below still applies */ }
  try { map.setLight(p.light); } catch (e) {}
  const mapEl = document.getElementById("map");
  if (mapEl) mapEl.style.background = p.skyCss;
  if (map.getLayer("buildings-3d")) {
    map.setPaintProperty("buildings-3d", "fill-extrusion-color",
      ["interpolate", ["linear"], ["get", "height"]].concat(p.buildings));
  }
  asphaltColor = p.asphalt;
  if (els.tint) { els.tint.style.background = p.tint; els.tint.style.mixBlendMode = p.blend; }
  refreshLayers();
  for (const k of ["dawn", "day", "dusk", "night"]) {
    const b = els["light_" + k];
    if (b) b.classList.toggle("active", k === name);
  }
}

// Re-theme the OpenFreeMap basemap to the exact Mapbox Standard "day" palette.
const MAP_COLORS = {
  land: "#f2edec", green: "#d1edcc", road: "#b3bbd3", water: "#aecae8", building: "#f1e5db",
};
function applyBasemapColors() {
  if (!map) return;
  const C = MAP_COLORS;
  for (const l of map.getStyle().layers) {
    const id = l.id.toLowerCase();
    try {
      if (l.type === "background") {
        map.setPaintProperty(l.id, "background-color", C.land);
      } else if (l.type === "fill") {
        if (/water|river|lake|ocean|sea|pond|basin|reservoir/.test(id)) map.setPaintProperty(l.id, "fill-color", C.water);
        else if (/park|wood|forest|grass|green|golf|pitch|garden|scrub|meadow|landcover|nature|cemetery|recreation|farm|vegetation|allotments/.test(id)) map.setPaintProperty(l.id, "fill-color", C.green);
        else if (/build/.test(id)) map.setPaintProperty(l.id, "fill-color", C.building);
        else map.setPaintProperty(l.id, "fill-color", C.land);
      } else if (l.type === "line") {
        if (/water|river|stream|canal|waterway/.test(id)) map.setPaintProperty(l.id, "line-color", C.water);
        else if (/road|street|highway|motorway|trunk|primary|secondary|tertiary|residential|service|transport|bridge|tunnel|path|track|rail|aeroway/.test(id)) map.setPaintProperty(l.id, "line-color", C.road);
      }
    } catch (e) { /* layer without that paint prop */ }
  }
}

// Show/hide the basemap POI symbols (shops, restaurants, banks, parking, bus &
// rail stops…) — all live in the "poi" source-layer. Street and place names stay.
function setPoiVisible(show) {
  if (!map) return;
  for (const l of map.getStyle().layers) {
    if (l.type === "symbol" && l["source-layer"] === "poi") {
      try { map.setLayoutProperty(l.id, "visibility", show ? "visible" : "none"); } catch (e) {}
    }
  }
}

// Show/hide ALL buildings: the SUMO polygons (buildings-3d) AND the basemap's
// OSM buildings (source-layer "building": both the 2D fill and the 3D extrusion).
function setBuildingsVisible(show) {
  if (!map) return;
  const v = show ? "visible" : "none";
  for (const l of map.getStyle().layers) {
    if (l.id === "buildings-3d" || l["source-layer"] === "building" || /building/i.test(l.id)) {
      try { map.setLayoutProperty(l.id, "visibility", v); } catch (e) {}
    }
  }
}

// SUMO signal codes -> colour (G/g green, y amber, u red-amber, r/s red, o off)
function tlColor(ch) {
  switch (ch) {
    case "G": case "g": return [46, 204, 64];
    case "y": case "Y": return [255, 210, 0];
    case "u":           return [255, 140, 0];
    case "r": case "s": return [255, 65, 54];
    default:            return [110, 116, 128];
  }
}
function tlColorFor(f) {
  const st = tlState[f.properties.tls];
  const ch = st ? st[f.properties.index] : "o";
  return tlColor(ch);
}

// --- signal head sprite (housing with 3 lenses, only the active one lit) -----
function signalSprite(litR, litY, litG) {
  const R = litR ? "#ff3b30" : "#3a1512";
  const Y = litY ? "#ffcc00" : "#3a3512";
  const G = litG ? "#2ecc40" : "#123a1e";
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="44" height="108">' +
    '<rect x="3" y="3" width="38" height="102" rx="12" fill="#14161c" stroke="#3a3f4d" stroke-width="2.5"/>' +
    '<circle cx="22" cy="27" r="14" fill="' + R + '"/>' +
    '<circle cx="22" cy="54" r="14" fill="' + Y + '"/>' +
    '<circle cx="22" cy="81" r="14" fill="' + G + '"/>' +
    '</svg>';
  return "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
}
function makeIconSet(anchorY, tag) {
  const mk = (lr, ly, lg, key) =>
    ({ url: signalSprite(lr, ly, lg), width: 44, height: 108, anchorY, mask: false, id: key + tag });
  return { off: mk(0, 0, 0, "off"), r: mk(1, 0, 0, "r"), y: mk(0, 1, 0, "y"), g: mk(0, 0, 1, "g"), u: mk(1, 1, 0, "u") };
}
const SIGNAL_ICON_HANG = makeIconSet(4, "_h");     // hangs from a mast arm
const SIGNAL_ICON_TOP  = makeIconSet(104, "_t");   // sits on top of a straight pole
function signalKey(f) {
  const st = tlState[f.properties.tls];
  const ch = st ? st[f.properties.index] : "o";
  if (ch === "u") return "u";
  if (ch === "r" || ch === "s") return "r";
  if (ch === "y" || ch === "Y") return "y";
  if (ch === "g" || ch === "G") return "g";
  return "off";
}
// move a lon/lat by `dist` metres along a compass `bearing` (deg)
function offsetLonLat(lon, lat, bearing, dist) {
  const R = 6378137, br = (bearing * Math.PI) / 180;
  const dLat = ((dist * Math.cos(br)) / R) * 180 / Math.PI;
  const dLon = ((dist * Math.sin(br)) / (R * Math.cos((lat * Math.PI) / 180))) * 180 / Math.PI;
  return [lon + dLon, lat + dLat];
}
const ARM_HEIGHT = 6.0, SIDE_DISTANCE = 3.2, STRAIGHT_H = 3.2;
// draw signals over the buildings (skip depth occlusion) so they're always visible.
// deck.gl's `parameters` prop uses classic WebGL keys (depthTest/depthMask), NOT
// luma.gl's WebGPU-style depthCompare/depthWriteEnabled — those silently broke the layers.
const TL_ON_TOP = { depthTest: false };
function sideBearing(f) { return ((f.properties.angle || 0) + 90) % 360; }   // right side of the approach
function poleBase(f) {
  const [lon, lat] = f.geometry.coordinates;
  return offsetLonLat(lon, lat, sideBearing(f), SIDE_DISTANCE);              // curb position
}
function signalPosition(f) {   // ménsula: head hangs over the road centre
  const [lon, lat] = f.geometry.coordinates;
  return [lon, lat, ARM_HEIGHT];
}
function signalArm(f) {        // arm: curb pole -> out over the lanes
  const [plon, plat] = poleBase(f);
  const [lon, lat] = f.geometry.coordinates;
  return [[plon, plat, ARM_HEIGHT], [lon, lat, ARM_HEIGHT]];
}
function signalHeadTop(f) {    // straight pole: head sits on top
  const [plon, plat] = poleBase(f);
  return [plon, plat, STRAIGHT_H];
}
function headPosition(f) {      // where the lit head sits: arm end (mast) vs pole top
  return f.properties.mast ? signalPosition(f) : signalHeadTop(f);
}
let tlMast = [], tlStraight = [];
function splitTrafficLights() {
  tlMast = tlDefs.features.filter((f) => f.properties.mast);
  tlStraight = tlDefs.features.filter((f) => !f.properties.mast);
}

async function fetchJSON(path, { tries = 20, delay = 2000 } = {}) {
  for (let i = 0; i < tries; i++) {
    try {
      const r = await fetch(API + path);
      if (!r.ok) throw new Error("HTTP " + r.status);
      return await r.json();
    } catch (e) {
      els.conn.textContent = `esperando al backend… (intento ${i + 1})`;
      els.conn.className = "offline";
      await new Promise((res) => setTimeout(res, delay));
    }
  }
  throw new Error("backend no disponible tras varios intentos");
}

async function boot() {
  const meta = await fetchJSON("/api/meta");

  map = new maplibregl.Map({
    container: "map",
    style: "https://tiles.openfreemap.org/styles/liberty",
    center: meta.center,
    zoom: 15.5,
    pitch: 55,
    maxPitch: 85,
    bearing: -18,
    antialias: true,
  });
  map.on("dragstart", stopOrbit);       // any manual pan/rotate cancels auto-orbit
  map.on("rotatestart", stopOrbit);
  map.on("rotate", syncPad);            // keep the Pan-Tilt pad in sync with gestures/orbit
  map.on("pitch", syncPad);
  map.on("moveend", syncPad);

  map.on("load", async () => {
    // deck.gl + Mapbox "single context" pattern (vis.gl): draw our data BENEATH
    // the basemap's text labels so street names stay legible on top, and share
    // the depth buffer so 3D buildings and vehicles occlude each other.
    labelLayerId = (map.getStyle().layers.find(
      (l) => l.type === "symbol" && l.layout && l.layout["text-field"]) || {}).id || undefined;
    try {
      map.setLight({ anchor: "viewport", color: "#fff", intensity: 0.35, position: [1.2, 210, 30] });
    } catch (e) { /* style without light support */ }

    applyBasemapColors();   // exact Mapbox Standard "day" palette (land/green/road/water)
    setPoiVisible(els.poi.checked);   // hide shop/bus-stop POI clutter by default

    // --- extruded building polygons (native MapLibre fill-extrusion) --------
    const buildings = await fetchJSON("/api/buildings");
    map.addSource("buildings", { type: "geojson", data: buildings });
    map.addLayer({
      id: "buildings-3d",
      type: "fill-extrusion",
      source: "buildings",
      paint: {
        // warm stone/beige, darkening with height -> reads like real buildings
        "fill-extrusion-color": [
          "interpolate", ["linear"], ["get", "height"],
          0, "#dcd7cd", 12, "#d0cabd", 25, "#c1baab", 45, "#ada695", 80, "#978d7d",
        ],
        "fill-extrusion-height": ["get", "height"],
        "fill-extrusion-base": 0,
        "fill-extrusion-opacity": 0.92,
        "fill-extrusion-vertical-gradient": true,
      },
    }, labelLayerId);
    setBuildingsVisible(els.buildings.checked);   // default: unchecked -> all buildings hidden

    // --- road network (deck.gl overlay, recoloured by congestion) ----------
    networkGeo = await fetchJSON("/api/network");
    laneLinesGeo = { type: "FeatureCollection",
      features: networkGeo.features.filter((f) => (f.properties.lanes || 1) >= 2) };
    edgeMid = {};
    for (const f of networkGeo.features) {
      const c = f.geometry.coordinates;
      edgeMid[f.properties.id] = c[Math.floor(c.length / 2)];
    }
    tlDefs = await fetchJSON("/api/trafficlights");
    splitTrafficLights();
    overlay = new deck.MapboxOverlay({ interleaved: true, layers: buildLayers() });
    map.addControl(overlay);

    applyLightPreset(currentPreset);   // default light preset (día)
    syncPad();                         // place the Pan-Tilt knob at the initial camera
    connect();
  });
}

// busy-street marker colour: amber -> red as the vehicle count grows
function busyColor(n) {
  const t = Math.max(0, Math.min(1, (n - busyMin) / 15));
  return [235, Math.round(150 - 110 * t), 40];
}

function buildLayers() {
  const roadW = (f) => 3.2 * (f.properties.lanes || 1);        // real carriageway width
  const layers = [
    // dark casing under the roads -> crisp, defined edges (surface colour unchanged)
    new deck.GeoJsonLayer({
      id: "network-casing",
      data: networkGeo,
      beforeId: labelLayerId,
      lineWidthUnits: "meters",
      getLineWidth: (f) => roadW(f) + 0.3,
      lineWidthMinPixels: 2.5,
      getLineColor: [64, 70, 86],
      lineCapRounded: true, lineJointRounded: true,
      pickable: false,
    }),
    // road surface — same colour as before (LOS when congested, else asphalt)
    new deck.GeoJsonLayer({
      id: "network",
      data: networkGeo,
      beforeId: labelLayerId,
      lineWidthUnits: "meters",
      getLineWidth: roadW,
      lineWidthMinPixels: 1.5,
      getLineColor: (f) =>
        (showCongestion && edgeColors[f.properties.id]) || asphaltColor,
      lineCapRounded: true, lineJointRounded: true,
      updateTriggers: { getLineColor: [edgeColors, showCongestion, asphaltColor] },
      pickable: false,
    }),
    // lane divider: thin centre line on carriageways with 2+ lanes (to identify them)
    new deck.GeoJsonLayer({
      id: "lane-lines",
      data: laneLinesGeo,
      beforeId: labelLayerId,
      lineWidthUnits: "meters",
      getLineWidth: 0.3,
      lineWidthMinPixels: 0.8,
      lineWidthMaxPixels: 2,
      getLineColor: [242, 242, 236],
      pickable: false,
    }),
  ];

  // traffic lights, coloured live from SUMO. Open junctions get a mast-arm
  // ("ménsula") with the head hanging over the road; tight ones a straight pole.
  if (showTL && tlDefs.features.length) {
    // poles: mast-arm ("ménsula") over the road, or a straight roadside pole
    layers.push(new deck.ColumnLayer({
      id: "tl-pole-mast", data: tlMast, diskResolution: 6, radius: 0.14, extruded: true,
      getPosition: poleBase, getFillColor: [30, 33, 40], getElevation: ARM_HEIGHT,
      parameters: TL_ON_TOP,
    }));
    layers.push(new deck.PathLayer({
      id: "tl-arm", data: tlMast, getPath: signalArm, getColor: [30, 33, 40],
      getWidth: 0.22, widthUnits: "meters", widthMinPixels: 2, capRounded: true, jointRounded: true,
      parameters: TL_ON_TOP,
    }));
    layers.push(new deck.ColumnLayer({
      id: "tl-pole-straight", data: tlStraight, diskResolution: 6, radius: 0.14, extruded: true,
      getPosition: poleBase, getFillColor: [30, 33, 40], getElevation: STRAIGHT_H,
      parameters: TL_ON_TOP,
    }));
    // signal heads: a dark housing + a lit lens coloured live from SUMO's phase.
    // Rendered as geometry (not IconLayer) so they show reliably like the poles.
    layers.push(new deck.ScatterplotLayer({
      id: "tl-housing", data: tlDefs.features, billboard: true,
      getPosition: headPosition, getFillColor: [18, 20, 26],
      getRadius: 1.9, radiusUnits: "meters", radiusMinPixels: 5, radiusMaxPixels: 18,
      parameters: TL_ON_TOP,
    }));
    layers.push(new deck.ScatterplotLayer({
      id: "tl-head", data: tlDefs.features, billboard: true,
      getPosition: headPosition, getFillColor: tlColorFor,
      getRadius: 1.25, radiusUnits: "meters", radiusMinPixels: 3.5, radiusMaxPixels: 13,
      parameters: TL_ON_TOP,
      updateTriggers: { getFillColor: [tlState] },
    }));
  }

  // procedural 3D vehicles: several extruded parts per vehicle (chassis,
  // greenhouse/windshield, roof, head/tail lights) at real size — 100% offline.
  layers.push(new deck.PolygonLayer({
    id: "vehicles",
    data: vehicles.flatMap(vehicleParts),
    extruded: true,
    getPolygon: (p) => p.polygon,
    getElevation: (p) => p.height,
    getFillColor: (p) => p.color,
    material: { ambient: 0.5, diffuse: 0.7, shininess: 20 },
    stroked: false,
    pickable: false,
    updateTriggers: { getPolygon: vehicles, getElevation: vehicles, getFillColor: vehicles },
  }));

  // busy streets: a beacon marker on every street whose current vehicle count
  // reaches the selector threshold (busyMin)
  if (showBusy) {
    const pts = [];
    for (const id in edgeCounts) {
      if (edgeCounts[id] >= busyMin && edgeMid[id]) {
        pts.push({ position: edgeMid[id], count: edgeCounts[id] });
      }
    }
    // floating pin: a tether down to the street + a round "drop" head hovering
    // above it + the vehicle count. Built from reliable layers because deck.gl's
    // IconLayer does not render in this interleaved setup.
    const PIN_H = 15;                     // metres the pin floats above the street
    const atStreet = (d) => [d.position[0], d.position[1], 0];
    const atHead = (d) => [d.position[0], d.position[1], PIN_H];
    const headR = (d) => 13 + Math.min(d.count, 10);   // head radius (px)
    layers.push(new deck.LineLayer({
      id: "busy-tether",
      data: pts,
      getSourcePosition: atStreet,
      getTargetPosition: atHead,
      getColor: (d) => [...busyColor(d.count), 200],
      getWidth: 2, widthMinPixels: 1.5,
      parameters: { depthTest: false },
      updateTriggers: { getColor: busyMin },
    }));
    // inverted-teardrop point: a down-triangle just below the head. Drawn BEFORE
    // the head so the round head covers its top -> a smooth teardrop silhouette.
    layers.push(new deck.TextLayer({
      id: "busy-point",
      data: pts,
      billboard: true,
      getPosition: atHead,
      getText: () => "▼",
      characterSet: ["▼"],               // not in the default ASCII set -> declare it
      getSize: (d) => headR(d) * 2.1, sizeUnits: "pixels",
      getColor: (d) => busyColor(d.count),
      getPixelOffset: (d) => [0, headR(d) - 4],
      getTextAnchor: "middle", getAlignmentBaseline: "top",
      parameters: { depthTest: false },
      updateTriggers: { getColor: busyMin, getSize: busyMin, getPixelOffset: busyMin },
    }));
    layers.push(new deck.ScatterplotLayer({
      id: "busy-head",
      data: pts,
      billboard: true,
      getPosition: atHead,
      getRadius: headR,
      radiusUnits: "pixels",
      getFillColor: (d) => busyColor(d.count),
      parameters: { depthTest: false },
      pickable: true,
      updateTriggers: { getRadius: busyMin, getFillColor: busyMin },
    }));
    layers.push(new deck.TextLayer({
      id: "busy-count",
      data: pts,
      billboard: true,
      getPosition: atHead,
      getText: (d) => String(d.count),
      getSize: 14, sizeUnits: "pixels",
      getColor: [255, 255, 255],
      fontWeight: 700,
      getTextAnchor: "middle", getAlignmentBaseline: "center",
      parameters: { depthTest: false },
      updateTriggers: { getText: busyMin },
    }));
  }

  return layers;
}

function refreshLayers() {
  if (overlay) overlay.setProps({ layers: buildLayers() });
}

// --- WebSocket live stream --------------------------------------------------
// Each connection is tracked by identity (sock === ws). When we switch scenario
// or reset, the previous socket is superseded, so its late frames and its close
// event are ignored — this prevents two SUMO streams alternating (the flicker).
function connect() {
  const sock = new WebSocket(WS_URL + "?level=" + currentLevel);
  ws = sock;
  sock.onopen = () => { if (sock === ws) setConn(true); };
  sock.onerror = () => { if (sock === ws) setConn(false); };
  sock.onclose = () => {
    if (sock !== ws) return;                       // superseded by a newer connection
    setConn(false);
    setTimeout(() => { if (sock === ws) connect(); }, 2000);   // auto-reconnect only if still current
  };
  sock.onmessage = (ev) => {
    if (sock !== ws) return;                        // drop frames from an old/switched-out run
    const msg = JSON.parse(ev.data);
    if (msg.type === "frame") {
      vehicles = msg.vehicles;
      edgeColors = {}; edgeCounts = {};
      for (const e of msg.edges) { edgeColors[e.id] = hexToRgb(e.color); edgeCounts[e.id] = e.n; }
      tlState = msg.tls || {};
      els.vehCount.textContent = vehicles.length;
      els.simTime.textContent = msg.t;
      refreshLayers();
    } else if (msg.type === "end") {
      els.conn.textContent = "simulación finalizada";
    } else if (msg.type === "error") {
      els.conn.textContent = msg.message;
    }
  };
}
function setConn(on) {
  els.conn.textContent = on ? "en vivo" : "desconectado";
  els.conn.className = on ? "online" : "offline";
}

// --- controls ---------------------------------------------------------------
els.play.onclick = () => {
  paused = !paused;
  els.play.textContent = paused ? "▶ Reanudar" : "⏸ Pausar";
  send({ cmd: paused ? "pause" : "play" });
};
els.reset.onclick = () => {
  if (ws) ws.close();
  vehicles = []; edgeColors = {}; refreshLayers();
  paused = false; els.play.textContent = "⏸ Pausar";
  connect();
};
els.level.onchange = () => {
  currentLevel = els.level.value;                 // switch scenario -> reconnect
  if (ws) ws.close();
  vehicles = []; edgeColors = {}; tlState = {}; refreshLayers();
  paused = false; els.play.textContent = "⏸ Pausar";
  connect();
};
els.fps.oninput = () => {
  els.fpsVal.textContent = `${els.fps.value} fps`;
  send({ cmd: "speed", fps: Number(els.fps.value) });
};
els.buildings.onchange = () => setBuildingsVisible(els.buildings.checked);
els.congestion.onchange = () => { showCongestion = els.congestion.checked; refreshLayers(); };
els.tl.onchange = () => { showTL = els.tl.checked; refreshLayers(); };
els.poi.onchange = () => setPoiVisible(els.poi.checked);
els.busy.onchange = () => { showBusy = els.busy.checked; refreshLayers(); };
els.busyMin.oninput = () => { busyMin = Number(els.busyMin.value); els.busyVal.textContent = busyMin; refreshLayers(); };

// --- unified Pan-Tilt pad: X = bearing (pan), Y = pitch (tilt) --------------
const PITCH_MAX = 85;
const COMPASS = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"];
function placeKnob(nx, ny, bearing, pitch) {
  if (els.padKnob) { els.padKnob.style.left = `${nx * 100}%`; els.padKnob.style.top = `${ny * 100}%`; }
  if (els.ptRead) {
    const b = Math.round(bearing);
    const dir = COMPASS[Math.round((((b % 360) + 360) % 360) / 45) % 8];
    els.ptRead.textContent = `${dir} ${b}° · tilt ${Math.round(pitch)}°`;
  }
}
function syncPad() {                     // reflect the camera on the pad (gestures / orbit)
  if (!map) return;
  const b = map.getBearing(), p = map.getPitch();
  placeKnob((b / 180 + 1) / 2, 1 - p / PITCH_MAX, b, p);
  setViewLabel(p <= 5);                  // keep the 2D/3D button label in sync
}
function padPoint(ev) {                   // pointer -> bearing (X) + pitch (Y)
  const r = els.pad.getBoundingClientRect();
  const nx = Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width));
  const ny = Math.min(1, Math.max(0, (ev.clientY - r.top) / r.height));
  if (map) {
    stopOrbit();
    map.setBearing((nx * 2 - 1) * 180);     // left -180 .. right +180 (centre = north)
    map.setPitch((1 - ny) * PITCH_MAX);     // top = horizon (85°), bottom = top-down (0°)
    syncPad();                              // knob follows the (clamped) camera
  }
}
let padDrag = false;
els.pad.addEventListener("pointerdown", (ev) => {
  padDrag = true; els.pad.classList.add("grabbing");
  try { els.pad.setPointerCapture(ev.pointerId); } catch (e) {}
  padPoint(ev);
});
els.pad.addEventListener("pointermove", (ev) => { if (padDrag) padPoint(ev); });
els.pad.addEventListener("pointerup", () => { padDrag = false; els.pad.classList.remove("grabbing"); });
els.pad.addEventListener("dblclick", () => { if (map) { stopOrbit(); map.easeTo({ bearing: 0, pitch: 55, duration: 400 }); } });

let orbitRAF = null;
function stopOrbit() {
  if (orbitRAF) { cancelAnimationFrame(orbitRAF); orbitRAF = null; els.orbit.classList.remove("active"); }
}
els.orbit.onclick = () => {
  if (!map) return;
  if (orbitRAF) { stopOrbit(); return; }        // toggle off
  els.orbit.classList.add("active");            // start a slow continuous orbit
  const spin = () => { map.setBearing(map.getBearing() + 0.15); orbitRAF = requestAnimationFrame(spin); };
  orbitRAF = requestAnimationFrame(spin);
};

// --- zoom -----------------------------------------------------------------
els.zoomIn.onclick = () => map && map.zoomIn();
els.zoomOut.onclick = () => map && map.zoomOut();

// --- 2D top-down / 3D perspective toggle ----------------------------------
let topView = false;                     // label shows the view the NEXT click gives
function setViewLabel(top) {
  if (!els.viewTop) return;
  topView = top;
  els.viewTop.textContent = top ? "3D" : "2D";
  els.viewTop.classList.toggle("active", top);
}
els.viewTop.onclick = () => {
  if (!map) return;
  stopOrbit();
  const goTop = !topView;
  map.easeTo({ pitch: goTop ? 0 : 55, duration: 500 });  // 2D top-down vs 3D perspective
  setViewLabel(goTop);
};

// --- light presets (dawn / day / dusk / night) ----------------------------
els.light_dawn.onclick = () => applyLightPreset("dawn");
els.light_day.onclick = () => applyLightPreset("day");
els.light_dusk.onclick = () => applyLightPreset("dusk");
els.light_night.onclick = () => applyLightPreset("night");

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

// --- auto-hide the control panel: collapses to its title bar when the pointer
//     leaves, expands on hover (shown briefly on load so the controls are found)
(() => {
  const p = document.getElementById("panel");
  if (!p) return;
  let t = null;
  p.addEventListener("mouseenter", () => { clearTimeout(t); p.classList.remove("collapsed"); });
  p.addEventListener("mouseleave", () => { clearTimeout(t); t = setTimeout(() => p.classList.add("collapsed"), 600); });
  setTimeout(() => p.classList.add("collapsed"), 2500);
})();

boot().catch((e) => { els.conn.textContent = "error: " + e.message; });
