/* SUMO-GEO frontend — MapLibre GL (basemap + extruded buildings) with a
 * deck.gl overlay for road-congestion colouring and live vehicles streamed
 * over a WebSocket. Everything runs in the browser, no build step. */

const API = "";                                   // same-origin (nginx proxies /api and /ws)
const WS_URL = (location.protocol === "https:" ? "wss://" : "ws://")
  + location.host + "/ws/live";

const els = {
  play: document.getElementById("btn-play"),
  reset: document.getElementById("btn-reset"),
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
  replayBtn: document.getElementById("btn-replay"),
  replayBar: document.getElementById("replay-bar"),
  rpSeek: document.getElementById("rp-seek"),
  rpTimeCur: document.getElementById("rp-time-cur"),
  rpTimeTotal: document.getElementById("rp-time-total"),
  rowMsgs: document.getElementById("row-msgs"),
  rowMsgFilter: document.getElementById("row-msg-filter"),
  stepRow: document.getElementById("replay-step-row"),
  rowVehFilter: document.getElementById("row-veh-filter"),
  rpSpeedRow: document.getElementById("rp-speed-row"),
  rpSlow: document.getElementById("rp-slow"),
  rpFast: document.getElementById("rp-fast"),
  rpSpeed: document.getElementById("rp-speed"),
  rpStep: document.getElementById("rp-step"),
  rpBack: document.getElementById("rp-back"),
  rpPlay: document.getElementById("rp-play"),
  vehFilter: document.getElementById("veh-filter"),
  msgsToggle: document.getElementById("toggle-msgs"),
  fCam: document.getElementById("f-cam"),
  fCpm: document.getElementById("f-cpm"),
  fDenm: document.getElementById("f-denm"),
  arcLbl: document.getElementById("toggle-arc-lbl"),
  zen: document.getElementById("btn-zen"),
  detect: document.getElementById("toggle-detect"),
  sensorMode: document.getElementById("sensor-mode"),
  followBadge: document.getElementById("follow-badge"),
  followVeh: document.getElementById("follow-veh"),
  followHud: document.getElementById("follow-hud"),
  followExit: document.getElementById("follow-exit"),
};

// modo presentación: ocultar todos los paneles (solo mapa + simulación)
els.zen.onclick = () => document.body.classList.toggle("zen");

// --- replay offline (mensajes V2X desde .pcap de VaN3Twin) -------------------
let replayMode = false;        // conectar con ?replay=1
let liveV2xSeen = false;       // llegaron mensajes V2X en modo vivo (pcap en caliente)
let liveV2xTimer = null;       // refresco periódico del panel PHY en vivo
let liveStations = new Set();  // estaciones vistas en vivo (opciones del filtro emisor)
let replayT0 = 0, replayT1 = 0;
let rpDragging = false;
let showMsgs = true;
let showArcLabels = true;      // etiqueta "N m · X dBm" sobre cada enlace
let selStation = null;         // filtro: solo mensajes emitidos por esta estación
let msgEvents = [];            // {kind:'tx'|'rx', type, wallT, simT, st, pos, pos2}
// vida visual de un evento (ms): los arcos TX→RX duran más que el pulso de
// transmisión para que dé tiempo a verlos/clicarlos antes de desvanecerse
const MSG_TTL_TX = 1200;       // pulso radial de transmisión
const MSG_TTL_RX = 2600;       // arco de recepción (el que se inspecciona)
const msgTtl = (e) => (e.kind === "tx" ? MSG_TTL_TX : MSG_TTL_RX);
// radio final del pulso TX = alcance REAL medido en la corrida (range_m.p95
// del panel PHY, cargado de /api/replay/info); fallback si aún no llegó
let phyRangeM = null;
const PULSE_FALLBACK_M = 150;
const MSG_COLORS = { CAM: [77, 163, 255], CPM: [55, 200, 113], DENM: [255, 83, 71] };
// barra de tiempo del replay: formato m:ss + relleno de progreso del <input
// type=range> (el navegador ignora `background` en el thumb/track nativos,
// así que el degradado se recalcula a mano en cada cambio de valor).
function fmtClock(s) {
  s = Math.max(0, Math.round(s || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
function updateSeekFill() {
  const el = els.rpSeek;
  if (!el) return;
  const min = Number(el.min) || 0, max = Number(el.max) || 100, val = Number(el.value);
  const pct = max > min ? Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100)) : 0;
  el.style.background = `linear-gradient(to right, var(--accent) ${pct}%, var(--line) ${pct}%)`;
}
function msgFilterOn(type) {
  if (type === "CAM") return els.fCam.checked;
  if (type === "CPM") return els.fCpm.checked;
  if (type === "DENM") return els.fDenm.checked;
  return true;
}

let map, overlay, ws;
let networkGeo = { type: "FeatureCollection", features: [] };
let laneLinesGeo = { type: "FeatureCollection", features: [] };  // edges with 2+ lanes
let edgeColors = {};      // edgeId -> [r,g,b]
let edgeCounts = {};      // edgeId -> vehicle count (n) in the latest frame
let edgeStats = {};       // edgeId -> full live stats {n, occ, speed, density, los}
let edgeMid = {};         // edgeId -> [lon,lat] midpoint (for the busy-street icons)
let losStamp = 0;         // bumped when a new LOS snapshot is accepted (throttled)
let lastLosMs = 0;
const LOS_UPDATE_MS = 700; // recolouring 34k+ edges every frame is wasteful — throttle it
let vehicles = [];        // latest frame vehicles (interpolados para el render)

// --- interpolación de movimiento entre frames -------------------------------
// Cada frame del backend avanza APP_STEP_LENGTH seg simulados de golpe (en modo
// van3twin, 0.5-1 s): sin esto los vehículos "saltan". Entre frame y frame se
// interpola posición y rumbo a ritmo de requestAnimationFrame.
const INTERP_MAX_VEH = 1500;   // por encima, refresco directo (escenarios masivos)
const INTERP_MAX_JUMP = 80;    // m; salto mayor = teletransporte/reinserción -> no interpolar
// Factor de estiramiento de la ventana de interpolación. interpPeriod es una
// MEDIA móvil del intervalo entre frames: la mitad de los frames llegan más
// tarde que la media (jitter), el vehículo alcanzaba su objetivo antes de
// tiempo y se quedaba PARADO esperando -> movimiento avanza-y-para. Con la
// ventana estirada el movimiento nunca se agota; como cada frame nuevo
// reinicia la interpolación desde la posición DIBUJADA, el pequeño retraso se
// recupera solo, sin saltos (mismo enfoque que "render one interval behind").
const INTERP_STRETCH = 1.30;
let frameVehicles = [];        // último frame recibido (crudo)
let interpPrev = new Map();    // id -> {lon,lat,angle} dibujados en el frame anterior
let interpT0 = 0;              // performance.now() del último frame
let interpPeriod = 100;        // ms entre frames (media móvil)

function angleLerp(a, b, t) {
  const d = ((b - a + 540) % 360) - 180;      // camino angular más corto
  return (a + d * t + 360) % 360;
}
function metersBetween(lon1, lat1, lon2, lat2) {
  const kx = 111320 * Math.cos(lat1 * Math.PI / 180);
  return Math.hypot((lon2 - lon1) * kx, (lat2 - lat1) * 110540);
}
// Ritmo del tick adaptado al tamaño de la flota: interpolar 500 vehículos a
// 60 fps recalcula demasiada geometría; mejor menos ticks pero fluidos.
function interpMinInterval(n) {
  if (n <= 150) return 0;      // cada rAF (~60 fps)
  if (n <= 400) return 33;     // ~30 fps
  if (n <= 800) return 50;     // ~20 fps
  return 90;                   // ~11 fps
}
let interpLastDraw = 0;

function interpTick() {
  requestAnimationFrame(interpTick);
  const now = performance.now();
  if (now - interpLastDraw < interpMinInterval(frameVehicles.length)) return;
  const t = interpT0 ? Math.min((now - interpT0) / (interpPeriod * INTERP_STRETCH), 1) : 1;
  const needVeh = !paused && frameVehicles.length > 0 &&
    frameVehicles.length <= INTERP_MAX_VEH && t < 1;
  // animar fade aunque no haya interp.; en modo paso (congelado) no hace falta
  const needMsg = msgEvents.length > 0 && !(replayMode && paused);
  if (!needVeh && !needMsg) return;
  if (needVeh) {
    vehicles = frameVehicles.map((v) => {
      const p = interpPrev.get(v.id);
      if (!p || metersBetween(p.lon, p.lat, v.lon, v.lat) > INTERP_MAX_JUMP) return v;
      return { ...v,
        lon: p.lon + (v.lon - p.lon) * t,
        lat: p.lat + (v.lat - p.lat) * t,
        angle: angleLerp(p.angle, v.angle, t) };
    });
  }
  interpLastDraw = now;
  refreshDynamicLayers();                      // vehículos + mensajes
}
requestAnimationFrame(interpTick);
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
// Paleta "atardecer andino": tonos medios/cálidos originales, pensados para el
// tinte MULTIPLICATIVO sobre la carrocería gris claro del .glb (colores muy
// oscuros dejarían los coches como manchas negras) y para distinguirse bien
// sobre el mapa claro y los paneles azul UCuenca.
const CAR_COLORS = [
  [246, 240, 226],   // crema perlado
  [224, 122, 95],    // terracota
  [240, 190, 70],    // mostaza
  [140, 178, 128],   // salvia
  [64, 180, 166],    // turquesa andino
  [70, 110, 200],    // cobalto
  [150, 96, 160],    // ciruela
  [242, 140, 130],   // coral
  [120, 138, 160],   // pizarra azulada
  [190, 100, 50],    // cobre quemado
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

const SHADOW = [10, 12, 16, 105];      // soft contact shadow under each vehicle
const TIRE = [22, 24, 28];

// Procedural 3D vehicle: extruded parts (contact shadow, chassis, wheels,
// greenhouse/windshield, roof, head/tail lights) at real size — 100% offline.
function vehicleParts(d) {
  const L = d.len || (isBus(d) ? 12 : 4.5);
  const W = d.wid || (isBus(d) ? 2.55 : 1.8);
  const body = vehicleColor(d);
  const roof = darken(body, 0.82);
  if (isBus(d)) {
    return [
      { polygon: partRect(d, L * 1.06, W * 1.25, 0, 1, 1), height: 0.03, color: SHADOW },// contact shadow
      { polygon: partRect(d, L, W, 0.30, 0.10, 1.02), height: 0.55, color: TIRE },       // front axle/wheels
      { polygon: partRect(d, L, W, -0.28, 0.10, 1.02), height: 0.55, color: TIRE },      // rear axle/wheels
      { polygon: partRect(d, L, W, 0.00, 1.00, 1.00), height: 2.6, color: body },        // chassis
      { polygon: partRect(d, L, W, 0.00, 0.96, 0.90), height: 2.8, color: roof },        // roof cap
      { polygon: partRect(d, L, W, 0.46, 0.06, 0.90), height: 2.2, color: GLASS },       // windshield
      { polygon: partRect(d, L, W, 0.49, 0.02, 0.94), height: 1.0, color: HEADLIGHT },   // headlights
      { polygon: partRect(d, L, W, -0.49, 0.02, 0.94), height: 1.0, color: TAILLIGHT },  // taillights
    ];
  }
  return [
    { polygon: partRect(d, L * 1.10, W * 1.30, 0, 1, 1), height: 0.03, color: SHADOW },  // contact shadow
    { polygon: partRect(d, L, W, 0.32, 0.14, 1.04), height: 0.34, color: TIRE },         // front wheels
    { polygon: partRect(d, L, W, -0.32, 0.14, 1.04), height: 0.34, color: TIRE },        // rear wheels
    { polygon: partRect(d, L, W, 0.00, 1.00, 1.00), height: 0.85, color: body },         // chassis
    { polygon: partRect(d, L, W, -0.06, 0.56, 0.86), height: 1.24, color: GLASS },       // greenhouse / windshield
    { polygon: partRect(d, L, W, -0.08, 0.48, 0.76), height: 1.44, color: roof },        // roof
    { polygon: partRect(d, L, W, 0.47, 0.05, 0.84), height: 0.62, color: HEADLIGHT },    // headlights
    { polygon: partRect(d, L, W, -0.48, 0.045, 0.84), height: 0.64, color: TAILLIGHT },  // taillights
  ];
}

// --- vehículos como modelos glTF instanciados (deck.ScenegraphLayer) --------
// Un .glb low-poly por clase (frontend/models/, generados proceduralmente:
// silueta con capó, parabrisas, ruedas y luces; ~300 tris). Instancing GPU:
// por vehículo solo se suben posición/orientación/escala — MÁS rápido que las
// cajas extruidas (que regeneraban 8 polígonos por vehículo por tick en CPU).
// Modelos construidos Z-up con el morro hacia +Y (norte) a tamaño real.
// Fallback automático a cajas si la capa no existe en el bundle, si falla la
// carga de un .glb o con flotas enormes.
// Kenney Car Kit (kenney.nl, licencia CC0) convertido a nuestra convención
// (Z-up, morro +Y, suelo z=0) — ver script de conversión en el contexto. Los
// modelos traen su color en TEXTURA (la paleta CAR_COLORS ya no los tiñe;
// queda para el fallback de cajas). Variedad: variante elegida por hash del
// id, estable entre frames. Sin bus en el kit -> se conserva el procedural.
// Si los coches aparecieran marcha atrás, poner yawOff: 180 en los kenney.
const VEH_MODELS = {
  car: [
    { url: "models/kenney/sedan.glb",            len: 2.55, wid: 1.50 },
    { url: "models/kenney/suv.glb",              len: 2.70, wid: 1.50 },
    { url: "models/kenney/taxi.glb",             len: 2.75, wid: 1.50 },
    { url: "models/kenney/van.glb",              len: 2.75, wid: 1.50 },
    { url: "models/kenney/hatchback-sports.glb", len: 2.85, wid: 1.30 },
  ],
  bus:       [{ url: "models/bus.glb",              len: 12.12, wid: 2.54 }],
  emergency: [{ url: "models/kenney/ambulance.glb", len: 3.25,  wid: 1.50 }],
};
const MODEL_MAX_VEH = 3000;
// SUMO da rumbo 0=N horario; el yaw de deck es antihorario alrededor de Z.
// Si al probar el modelo apareciera girado, ajustar SOLO esta función.
const MODEL_YAW = (a) => -(a || 0);
// requiere ScenegraphLayer (bundle deck) Y GLTFLoader (bundle @loaders.gl/gltf,
// global `loaders` — en deck.gl 9 el cargador glTF NO viene en dist.min.js)
let vehModelsOk = typeof deck !== "undefined" && !!deck.ScenegraphLayer &&
                  typeof loaders !== "undefined" && !!loaders.GLTFLoader;

function vehClass(d) {
  if (/emergency|ambulan|police|firebrigade|rescue/i.test(d.type || "")) return "emergency";
  if (isBus(d)) return "bus";
  return "car";
}
function makeVehicleModelLayers() {
  // una capa por VARIANTE de modelo (instancing por .glb); la variante de cada
  // vehículo sale del hash de su id -> estable entre frames
  const groups = new Map();
  for (const v of vehicles) {
    const k = vehClass(v);
    const vars = VEH_MODELS[k];
    const i = vars.length > 1 ? hashId(v.id) % vars.length : 0;
    let g = groups.get(k + "-" + i);
    if (!g) groups.set(k + "-" + i, g = { m: vars[i], data: [] });
    g.data.push(v);
  }
  const out = [];
  for (const [key, g] of groups) {
    const m = g.m;
    out.push(new deck.ScenegraphLayer({
      id: "veh-glb-" + key,
      data: g.data,
      scenegraph: m.url,             // misma URL entre ticks -> no se recarga
      loaders: [loaders.GLTFLoader],
      getPosition: (d) => [d.lon, d.lat, 0],
      getOrientation: (d) => [0, MODEL_YAW(d.angle) + (m.yawOff || 0), 0],
      getScale: (d) => {
        const s = (d.len || m.len) / m.len;
        return [(d.wid || m.wid) / m.wid, s, (s + 1) / 2];
      },
      sizeScale: 1,
      _lighting: "pbr",
      pickable: true,
      onError: () => { vehModelsOk = false; refreshLayers(); },
    }));
  }
  return out;
}

// LOD: with thousands of vehicles (or zoomed far out) the small parts are
// sub-pixel — draw shadow + one body box per vehicle instead of 8 parts.
const LOD_MAX_DETAILED = 1500, LOD_MIN_ZOOM = 14.5;
function vehicleLod(d) {
  const L = d.len || (isBus(d) ? 12 : 4.5);
  const W = d.wid || (isBus(d) ? 2.55 : 1.8);
  return [
    { polygon: partRect(d, L * 1.08, W * 1.25, 0, 1, 1), height: 0.03, color: SHADOW },
    { polygon: partRect(d, L, W, 0, 1, 1), height: isBus(d) ? 2.8 : 1.4, color: vehicleColor(d) },
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
    ambient: 0.55, sunColor: [255, 214, 170], sunIntensity: 1.3, sunDir: [-1, 0.1, -0.35],
    buildings: [0, "#cbc7c1", 12, "#c3beb4", 25, "#b6b0a4", 45, "#a49d90", 80, "#8f8778"],
    asphalt: [66, 70, 80], tint: "linear-gradient(to top, rgba(255,180,120,.18), rgba(120,130,175,.12))", blend: "soft-light",
  },
  day: {
    sky: { "sky-color": "#8fb6e8", "horizon-color": "#cfe0f2", "fog-color": "#e8eef5",
           "sky-horizon-blend": 0.8, "horizon-fog-blend": 0.4, "fog-ground-blend": 0.3, "atmosphere-blend": 0.6 },
    skyCss: "linear-gradient(to bottom, #7fb0ea 0%, #a8c9ef 60%, #dfeaf5 100%)",
    light: { color: "#ffffff", intensity: 0.4, position: [1.2, 210, 30] },
    ambient: 0.65, sunColor: [255, 255, 250], sunIntensity: 1.7, sunDir: [-0.3, -0.5, -1],
    buildings: [0, "#f1e5db", 25, "#ece0d2", 60, "#e3d7c8", 90, "#d8ccbc"],
    asphalt: [179, 187, 211], tint: "transparent", blend: "normal",
  },
  dusk: {
    sky: { "sky-color": "#38265a", "horizon-color": "#ff8a4c", "fog-color": "#d1774d",
           "sky-horizon-blend": 0.55, "horizon-fog-blend": 0.6, "fog-ground-blend": 0.5, "atmosphere-blend": 0.95 },
    skyCss: "linear-gradient(to bottom, #241a3a 0%, #5a2f63 45%, #ff8a4c 100%)",
    light: { color: "#ff9d5c", intensity: 0.5, position: [1.5, 250, 12] },
    ambient: 0.45, sunColor: [255, 150, 90], sunIntensity: 1.4, sunDir: [1, 0.25, -0.28],
    buildings: [0, "#c08a5f", 20, "#8f6f6a", 45, "#5f4f5e", 80, "#3f3550"],
    asphalt: [48, 42, 56], tint: "linear-gradient(to top, rgba(255,120,50,.30), rgba(42,26,64,.42))", blend: "multiply",
  },
  night: {
    sky: { "sky-color": "#070c20", "horizon-color": "#26335c", "fog-color": "#16233f",
           "sky-horizon-blend": 0.5, "horizon-fog-blend": 0.6, "fog-ground-blend": 0.6, "atmosphere-blend": 1.0 },
    skyCss: "linear-gradient(to bottom, #050a1c 0%, #101836 55%, #26335c 100%)",
    light: { color: "#9fb4ff", intensity: 0.25, position: [1.5, 200, 60] },
    ambient: 0.35, sunColor: [150, 170, 255], sunIntensity: 0.55, sunDir: [0.3, -0.4, -1],
    buildings: [0, "#3c4157", 20, "#31374c", 45, "#282c41", 80, "#202338"],
    asphalt: [30, 32, 45], tint: "linear-gradient(to top, rgba(14,20,46,.55), rgba(8,12,30,.70))", blend: "multiply",
  },
};

// deck.gl scene lighting synced with the preset: ambient + directional "sun".
// Shades the extruded vehicles (and any 3D deck geometry) so faces facing the
// sun are lit and the others fall into shadow — the core of the realism boost.
function makeLighting(name) {
  const p = LIGHT_PRESETS[name];
  return new deck.LightingEffect({
    ambient: new deck.AmbientLight({ color: [255, 255, 255], intensity: p.ambient }),
    sun: new deck.DirectionalLight({ color: p.sunColor, intensity: p.sunIntensity, direction: p.sunDir }),
  });
}

function applyLightPreset(name) {
  const p = LIGHT_PRESETS[name];
  if (!p || !map) return;
  currentPreset = name;
  if (overlay) overlay.setProps({ effects: [makeLighting(name)] });
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

// --- sensor looks (inspirado en gods-eye-view: CRT/NVG/FLIR re-tiñen toda la
// escena) — aquí como filtro CSS sobre #map (mapa + overlay deck.gl comparten
// el mismo elemento), sin shaders GLSL adicionales: mismo efecto visual,
// cero coste de mantenimiento en un stack "sin build step".
const SENSOR_MODES = {
  normal: { filter: "none", scan: false },
  nvg:    { filter: "brightness(1.3) contrast(1.15) saturate(4.5) sepia(.55) hue-rotate(68deg)", scan: false },
  flir:   { filter: "grayscale(1) invert(1) contrast(1.35) sepia(1) hue-rotate(190deg) saturate(5)", scan: false },
  noir:   { filter: "grayscale(1) contrast(1.35) brightness(.92)", scan: false },
  crt:    { filter: "contrast(1.12) saturate(1.3) brightness(1.03)", scan: true },
};
function applySensorMode(name) {
  const m = SENSOR_MODES[name] || SENSOR_MODES.normal;
  const mapEl = document.getElementById("map");
  if (mapEl) mapEl.style.filter = m.filter;
  document.body.dataset.sensor = m.scan ? "crt" : "";
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
  // cancelar órbita/seguimiento SOLO con gestos del usuario. OJO: jumpTo/setBearing
  // disparan "rotatestart" también (programático, sin originalEvent) — sin el
  // guard, el propio jumpTo de la cámara cockpit cancelaba el seguimiento en el
  // primer frame (por eso "no funcionaba").
  map.on("dragstart", () => { stopOrbit(); stopFollow(); });
  map.on("rotatestart", (e) => { if (e && e.originalEvent) { stopOrbit(); stopFollow(); } });
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
    setupInspector();                  // right-click SUMO stats on vehicles/roads/signals
    connect();
  });
}

// busy-street marker colour: amber -> red as the vehicle count grows
function busyColor(n) {
  const t = Math.max(0, Math.min(1, (n - busyMin) / 15));
  return [235, Math.round(150 - 110 * t), 40];
}

const roadW = (f) => 3.2 * (f.properties.lanes || 1);          // real carriageway width
// Static layers are created ONCE and reused across frames — recreating them per
// frame made deck.gl re-diff the 34k-edge geometries at every simulation step.
let _casingLayer = null, _laneLayer = null;

// procedural 3D vehicles (shadow, chassis, wheels, windshield, roof, lights),
// with LOD: simple boxes when there are thousands of vehicles or zoomed out.
// Separada de buildLayers para que la interpolación pueda refrescar SOLO esta
// capa (reconstruir las ~10 capas restantes a 60 fps mataba el rendimiento
// con flotas grandes).
function makeVehiclesLayer() {
  // camino preferente: modelos glTF instanciados; cajas como fallback/escala
  if (vehModelsOk && vehicles.length <= MODEL_MAX_VEH) {
    try { return makeVehicleModelLayers(); } catch (e) { vehModelsOk = false; }
  }
  return [makeVehicleBoxLayer()];
}

function makeVehicleBoxLayer() {
  const detailed = vehicles.length <= LOD_MAX_DETAILED
    && (!map || map.getZoom() >= LOD_MIN_ZOOM);
  const mkParts = detailed ? vehicleParts : vehicleLod;
  return new deck.PolygonLayer({
    id: "vehicles",
    // each part keeps a ref to its vehicle so right-click picking can inspect it
    data: vehicles.flatMap((d) => mkParts(d).map((p) => (p.veh = d, p))),
    extruded: true,
    getPolygon: (p) => p.polygon,
    getElevation: (p) => p.height,
    getFillColor: (p) => p.color,
    material: { ambient: 0.42, diffuse: 0.78, shininess: 90, specularColor: [90, 95, 105] },
    stroked: false,
    pickable: true,
    updateTriggers: { getPolygon: [vehicles, detailed], getElevation: [vehicles, detailed], getFillColor: [vehicles, detailed] },
  });
}

// --- overlay de detección (inspirado en el HUD "detection mesh" de
// gods-eye-view): caja + ID sobre cada vehículo, como si un nodo V2X lo
// estuviera "viendo". Reutiliza partRect() ya existente para el rectángulo.
let showDetection = false;
const DETECT_COLOR = [80, 255, 140];
function makeDetectionLayer() {
  if (!showDetection || vehicles.length === 0 || vehicles.length > LOD_MAX_DETAILED) return [];
  const data = vehicles.map((d) => {
    const L = d.len || (isBus(d) ? 12 : 4.5);
    const W = d.wid || (isBus(d) ? 2.55 : 1.8);
    const box = partRect(d, L * 1.25, W * 1.7, 0, 1, 1);
    const h = (isBus(d) ? 3.0 : 1.6);
    return { veh: d, path: [...box, box[0]].map(([lon, lat]) => [lon, lat, h]), h };
  });
  return [
    new deck.PathLayer({
      id: "detect-box",
      data,
      getPath: (d) => d.path,
      getColor: [...DETECT_COLOR, 220],
      getWidth: 1.6, widthUnits: "pixels",
      pickable: false,
      parameters: { depthTest: false },
      updateTriggers: { getPath: [vehicles] },
    }),
    new deck.TextLayer({
      id: "detect-label",
      data,
      billboard: true,
      getPosition: (d) => [d.veh.lon, d.veh.lat, d.h + 1.1],
      getText: (d) => d.veh.station != null ? `veh${d.veh.station}` : d.veh.id,
      getSize: 10.5, sizeUnits: "pixels",
      getColor: [...DETECT_COLOR, 235],
      background: true,
      getBackgroundColor: [8, 24, 14, 175],
      backgroundPadding: [3, 1],
      getTextAnchor: "middle", getAlignmentBaseline: "bottom",
      parameters: { depthTest: false },
      updateTriggers: { getPosition: [vehicles], getText: [vehicles] },
    }),
  ];
}

function buildLayers() {
  if (!_casingLayer) {
    // dark casing under the roads -> crisp, defined edges (surface colour unchanged)
    _casingLayer = new deck.GeoJsonLayer({
      id: "network-casing",
      data: networkGeo,
      beforeId: labelLayerId,
      lineWidthUnits: "meters",
      getLineWidth: (f) => roadW(f) + 0.3,
      lineWidthMinPixels: 2.5,
      getLineColor: [64, 70, 86],
      lineCapRounded: true, lineJointRounded: true,
      pickable: false,
    });
    // lane divider: thin centre line on carriageways with 2+ lanes
    _laneLayer = new deck.GeoJsonLayer({
      id: "lane-lines",
      data: laneLinesGeo,
      beforeId: labelLayerId,
      lineWidthUnits: "meters",
      getLineWidth: 0.3,
      lineWidthMinPixels: 0.8,
      lineWidthMaxPixels: 2,
      getLineColor: [242, 242, 236],
      pickable: false,
    });
  }
  const layers = [
    _casingLayer,
    // road surface — LOS colour when congested, else asphalt. losStamp (throttled)
    // is the trigger, so the 34k-edge recolour runs ~1.4x/s instead of every frame.
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
      updateTriggers: { getLineColor: [losStamp, showCongestion, asphaltColor] },
      pickable: true,                              // right-click inspector
    }),
    _laneLayer,
  ];

  // traffic lights, coloured live from SUMO. Open junctions get a mast-arm
  // ("ménsula") with the head hanging over the road; tight ones a straight pole.
  if (showTL && tlDefs.features.length) {
    // poles & arms are static — build once, reuse every frame
    if (!buildLayers._tlStatic) {
      buildLayers._tlStatic = [
        new deck.ColumnLayer({
          id: "tl-pole-mast", data: tlMast, diskResolution: 6, radius: 0.14, extruded: true,
          getPosition: poleBase, getFillColor: [30, 33, 40], getElevation: ARM_HEIGHT,
          parameters: TL_ON_TOP,
        }),
        new deck.PathLayer({
          id: "tl-arm", data: tlMast, getPath: signalArm, getColor: [30, 33, 40],
          getWidth: 0.22, widthUnits: "meters", widthMinPixels: 2, capRounded: true, jointRounded: true,
          parameters: TL_ON_TOP,
        }),
        new deck.ColumnLayer({
          id: "tl-pole-straight", data: tlStraight, diskResolution: 6, radius: 0.14, extruded: true,
          getPosition: poleBase, getFillColor: [30, 33, 40], getElevation: STRAIGHT_H,
          parameters: TL_ON_TOP,
        }),
      ];
    }
    layers.push(...buildLayers._tlStatic);
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
      pickable: true,                              // right-click inspector
      updateTriggers: { getFillColor: [tlState] },
    }));
  }

  layers.push(...makeVehiclesLayer());

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

// Capas animadas de mensajes V2X (replay): pulso radial en cada TX + arco
// TX->RX por recepción, cada uno desvaneciéndose en su TTL (msgTtl).
function makeMessageLayers(now) {
  // en pausa (modo paso a paso) los mensajes NO se desvanecen: quedan fijos
  // en pantalla para poder inspeccionarlos con clic
  const frozen = replayMode && paused;
  if (!frozen) msgEvents = msgEvents.filter((e) => now - e.wallT < msgTtl(e));
  if (!showMsgs || msgEvents.length === 0) return [];
  // re-anclar pulsos/arcos a la posición DIBUJADA del vehículo en este tick:
  // los eventos capturan la posición cruda del frame, pero los vehículos se
  // pintan interpolados (~medio período atrás) — sin esto los arcos iban
  // "adelantados" a los vehículos. Fallback: posición capturada (veh que salió).
  const drawn = {};
  for (const v of vehicles) if (v.station != null) drawn[v.station] = [v.lon, v.lat];
  const pulses = [], arcs = [];
  for (const e of msgEvents) {
    if (!msgFilterOn(e.type)) continue;
    if (selStation !== null && e.st !== selStation) continue;
    const prog = frozen ? 0.35 : (now - e.wallT) / msgTtl(e); // 0..1
    const c = MSG_COLORS[e.type] || [200, 200, 200];
    if (e.kind === "tx") {
      // expansión hasta el alcance medido; alfa alto y desvanecimiento suave
      // (solo cae al 30% al final) para que el anillo sea visible todo su ciclo
      const R = phyRangeM || PULSE_FALLBACK_M;
      pulses.push({ ...e, pos: drawn[e.st] || e.pos, radius: 8 + (R - 8) * prog,
                    color: [...c, Math.round(220 * (1 - 0.7 * prog))] });
    } else if (e.pos2) {
      arcs.push({ ...e, pos: drawn[e.st] || e.pos, pos2: drawn[e.st2] || e.pos2,
                  color: [...c, Math.round(150 * (1 - prog))] });
    }
  }
  const out = [];
  // etiquetas de distancia (y RSSI si hay signal*.csv) sobre cada enlace;
  // se muestran en modo paso/pausa o cuando hay pocos arcos (evita saturar)
  if (showArcLabels && arcs.length && (frozen || arcs.length <= 40)) {
    out.push(new deck.TextLayer({
      id: "v2x-arc-lbl",
      data: arcs,
      billboard: true,
      getPosition: (d) => [(d.pos[0] + d.pos2[0]) / 2,
                           (d.pos[1] + d.pos2[1]) / 2, 10],
      getText: (d) => `${d.dist} m` + (d.rssi != null ? ` · ${d.rssi} dBm` : ""),
      getSize: 11, sizeUnits: "pixels",
      getColor: (d) => [d.color[0], d.color[1], d.color[2], 235],
      background: true,
      getBackgroundColor: [10, 14, 20, 190],
      backgroundPadding: [3, 1],
      getTextAnchor: "middle", getAlignmentBaseline: "bottom",
      parameters: { depthTest: false },
    }));
  }
  if (arcs.length) out.push(new deck.ArcLayer({
    id: "v2x-arc", data: arcs,
    getSourcePosition: (d) => d.pos, getTargetPosition: (d) => d.pos2,
    getSourceColor: (d) => d.color, getTargetColor: (d) => d.color,
    getWidth: 2.5, getHeight: 0.6, greatCircle: false,
    pickable: true,                    // clic en el arco = contenido del mensaje
    parameters: { depthTest: false },
  }));
  if (pulses.length) out.push(new deck.ScatterplotLayer({
    id: "v2x-pulse", data: pulses, stroked: true, filled: false,
    getPosition: (d) => d.pos, getRadius: (d) => d.radius,
    radiusUnits: "meters", getLineColor: (d) => d.color,
    getLineWidth: 3.2, lineWidthUnits: "pixels",
    pickable: true,                    // clic = contenido del mensaje
    parameters: { depthTest: false },
  }));
  return out;
}

// anillo ámbar sobre el vehículo seleccionado en el filtro del replay: hace
// visible de un vistazo QUIÉN es el emisor cuyos mensajes se están mostrando
function makeHighlightLayer() {
  if (selStation === null) return [];
  const v = vehicles.find((x) => x.station === selStation);
  if (!v) return [];
  return [new deck.ScatterplotLayer({
    id: "veh-selected",
    data: [v],
    stroked: true, filled: true,
    getPosition: (d) => [d.lon, d.lat],
    getRadius: 6, radiusUnits: "meters", radiusMinPixels: 14,
    getFillColor: [255, 210, 63, 45],
    getLineColor: [255, 210, 63, 230],
    getLineWidth: 3, lineWidthUnits: "pixels",
    parameters: { depthTest: false },
  })];
}

const DYN_IDS = new Set(["vehicles", "veh-selected", "v2x-pulse", "v2x-arc", "v2x-arc-lbl",
                          "detect-box", "detect-label"]);
// las capas de modelos glTF llevan id dinámico "veh-glb-<clase>-<variante>"
const isDynLayer = (id) => DYN_IDS.has(id) || id.startsWith("veh-glb-");

function refreshLayers() {
  if (!overlay) return;
  refreshLayers._last = [...buildLayers(), ...makeHighlightLayer(),
                         ...makeMessageLayers(performance.now()), ...makeDetectionLayer()];
  overlay.setProps({ layers: refreshLayers._last });
  updateFollowCamera();
}

// Refresco barato para los ticks de animación: solo se reconstruyen las capas
// dinámicas (vehículos, mensajes y detección). El resto se reutiliza por identidad.
function refreshDynamicLayers() {
  if (!overlay) return;
  if (!refreshLayers._last) { refreshLayers(); return; }
  const statics = refreshLayers._last.filter((l) => l && !isDynLayer(l.id));
  refreshLayers._last = [...statics, ...makeVehiclesLayer(), ...makeHighlightLayer(),
                         ...makeMessageLayers(performance.now()), ...makeDetectionLayer()];
  overlay.setProps({ layers: refreshLayers._last });
  updateFollowCamera();
}

// --- right-click inspector: live SUMO stats for the picked object ------------
const TL_CODE = { G: "verde (prioridad)", g: "verde", y: "ámbar", Y: "ámbar",
                  u: "rojo-ámbar", r: "rojo", s: "rojo (stop)", o: "apagado" };
// vehículo bajo el pick, venga de la capa de cajas (pieza con .veh) o de una
// capa glTF (el objeto ES el vehículo)
function pickedVehicle(info) {
  if (info.layer.id === "vehicles" && info.object.veh) return info.object.veh;
  if (/^veh-glb-/.test(info.layer.id)) return info.object;
  return null;
}

function inspectorHtml(info) {
  const o = info.object;
  const veh = pickedVehicle(info);
  if (veh) {
    const d = veh;
    return `<h3>Vehículo ${d.id}</h3>` +
      `Tipo: <b>${d.type}</b><br>` +
      `Velocidad: <b>${Math.round(d.speed * 3.6)} km/h</b> · Rumbo: <b>${Math.round(d.angle)}°</b><br>` +
      `Dimensiones: <b>${d.len} × ${d.wid} m</b><br>` +
      `Calle (edge): <b>${d.edge}</b>` +
      `<button id="popup-follow" style="margin-top:8px;width:100%;pointer-events:auto">🎥 Seguir este vehículo</button>` +
      `<div id="popup-extra" style="margin-top:6px;border-top:1px solid #1c4370;padding-top:6px;opacity:.8">cargando estadísticas…</div>`;
  }
  if (info.layer.id === "network") {
    const p = o.properties, s = edgeStats[p.id];
    let html = `<h3>Calle ${p.id}</h3>` +
      `Carriles: <b>${p.lanes}</b> · Longitud: <b>${p.length} m</b><br>` +
      `Vel. máxima: <b>${Math.round(p.speed * 3.6)} km/h</b>`;
    if (s) {
      html += `<br>Vehículos ahora: <b>${s.n}</b> · LOS: <b>${s.los}</b><br>` +
        `Densidad: <b>${s.density} veh/km/carril</b><br>` +
        `Vel. media: <b>${Math.round(s.speed * 3.6)} km/h</b> · Ocupación: <b>${Math.round(s.occ * 100)}%</b>`;
    } else {
      html += `<br>Sin tráfico en este momento (flujo libre)`;
    }
    return html;
  }
  if (info.layer.id === "tl-head" || info.layer.id === "tl-housing") {
    const p = o.properties;
    const st = tlState[p.tls];
    const ch = st ? st[p.index] : "o";
    return `<h3>Semáforo</h3>` +
      `Intersección: <b>${p.tls}</b><br>` +
      `Estado: <b>${TL_CODE[ch] || ch}</b><br>` +
      `Tipo: <b>${p.mast ? "ménsula" : "poste"}</b> · Acceso: <b>${Math.round(p.angle)}°</b>`;
  }
  if (info.layer.id === "busy-head") {
    return `<h3>Calle concurrida</h3>Vehículos ahora: <b>${o.count}</b>`;
  }
  return null;
}

// --- historical panel (right side): time series over the simulation ---------
const HIST_MAX = 720;                 // kept points (~12 min at 1 pt/s)
const hist = { t: [], cars: [], buses: [], co2: [], tt: [], wait: [] };
let lastHistMs = 0;
const HIST_UPDATE_MS = 1000;          // sample + redraw at most once per second

function histPush(arr, v) { arr.push(v); if (arr.length > HIST_MAX) arr.shift(); }

function simHHMM(sec) {
  const h = Math.floor(sec / 3600) % 24, m = Math.floor((sec % 3600) / 60);
  return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
}

function drawSeries(canvasId, seriesArr, colors) {
  const c = document.getElementById(canvasId);
  if (!c) return;
  const ctx = c.getContext("2d");
  const W = c.width, H = c.height;
  const AX = 12;                         // reserved strip for the time axis (px)
  const plotH = H - AX;
  ctx.clearRect(0, 0, W, H);
  let max = 1;
  for (const s of seriesArr) for (const v of s) if (v != null && v > max) max = v;
  seriesArr.forEach((s, si) => {
    if (!s.length) return;
    ctx.strokeStyle = colors[si];
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    for (let i = 0; i < s.length; i++) {
      const x = 1 + (i / Math.max(s.length - 1, 1)) * (W - 2);
      const y = plotH - 2 - ((s[i] == null ? 0 : s[i]) / max) * (plotH - 7);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
  });
  // time axis (simulated clock) — start / middle / end of the visible window
  const ts = hist.t;
  if (ts.length > 1) {
    ctx.strokeStyle = "#1c4370";
    ctx.beginPath(); ctx.moveTo(0, plotH + 0.5); ctx.lineTo(W, plotH + 0.5); ctx.stroke();
    ctx.fillStyle = "#8b95a5";
    ctx.font = "9px system-ui, sans-serif";
    ctx.textBaseline = "top";
    ctx.textAlign = "left";
    ctx.fillText(simHHMM(ts[0]), 1, plotH + 2);
    ctx.textAlign = "center";
    ctx.fillText(simHHMM(ts[Math.floor(ts.length / 2)]), W / 2, plotH + 2);
    ctx.textAlign = "right";
    ctx.fillText(simHHMM(ts[ts.length - 1]), W - 1, plotH + 2);
  }
}

function updateHistory(stats, vehCount, simT) {
  const now = performance.now();
  if (now - lastHistMs < HIST_UPDATE_MS) return;
  lastHistMs = now;
  const types = stats.types || {};
  let buses = 0;
  for (const k in types) if (/bus|coach|tram/i.test(k)) buses += types[k];
  histPush(hist.t, simT || 0);
  histPush(hist.cars, vehCount - buses);
  histPush(hist.buses, buses);
  histPush(hist.co2, stats.co2 || 0);
  histPush(hist.tt, stats.tt);
  histPush(hist.wait, stats.wait || 0);
  drawSeries("h-veh", [hist.cars, hist.buses], ["#4da3ff", "#fa9614"]);
  drawSeries("h-co2", [hist.co2], ["#9aa7ff"]);
  drawSeries("h-tt", [hist.tt], ["#6ee7a0"]);
  drawSeries("h-wait", [hist.wait], ["#ff8a8a"]);
  const set = (id, txt) => { const el = document.getElementById(id); if (el) el.textContent = txt; };
  set("h-veh-now", String(vehCount));
  set("h-co2-now", `${(stats.co2 || 0).toFixed(1)} g/s`);
  set("h-tt-now", stats.tt != null ? `${Math.round(stats.tt)} s` : "– s");
  set("h-wait-now", `${Math.round(stats.wait || 0)} s (${stats.wait_n || 0} veh)`);
  // current breakdown by vehicle type (top 8 + otros)
  const el = document.getElementById("h-types");
  if (el) {
    const entries = Object.entries(types).sort((a, b) => b[1] - a[1]);
    const top = entries.slice(0, 8);
    const rest = entries.slice(8).reduce((s, e) => s + e[1], 0);
    el.innerHTML = top.map(([k, n]) => `${k}<b>${n}</b><br>`).join("")
      + (rest ? `otros<b>${rest}</b>` : "");
  }
}

let pendingInspect = null;   // vehicle id whose extended stats we're waiting for

function setupInspector() {
  const popup = document.getElementById("popup");
  const container = map.getCanvasContainer();
  container.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    if (!overlay) return;
    const rect = map.getCanvas().getBoundingClientRect();
    const info = overlay.pickObject({
      x: e.clientX - rect.left, y: e.clientY - rect.top, radius: 8,
    });
    const html = info && info.object ? inspectorHtml(info) : null;
    if (!html) { popup.style.display = "none"; pendingInspect = null; return; }
    popup.classList.remove("interactive");   // el inspector rápido no captura el ratón
    popup.innerHTML = html;
    popup.style.display = "block";
    popup.style.left = Math.min(e.clientX + 12, window.innerWidth - 290) + "px";
    popup.style.top = Math.min(e.clientY + 12, window.innerHeight - 230) + "px";
    // vehicle? -> ask the backend for the extended SUMO stats over the same WS
    const veh = pickedVehicle(info);
    if (veh) {
      pendingInspect = veh.id;
      send({ cmd: "inspect", id: pendingInspect });
      const fb = document.getElementById("popup-follow");
      if (fb) fb.onclick = () => { startFollow(veh.id); popup.style.display = "none"; };
    } else {
      pendingInspect = null;
    }
  });
  map.on("click", () => { popup.style.display = "none";
                          popup.classList.remove("interactive"); pendingInspect = null; });
  map.on("dragstart", () => { popup.style.display = "none";
                              popup.classList.remove("interactive"); pendingInspect = null; });
}

// fill in the popup when the extended stats arrive from the backend
function applyInspect(msg) {
  const extra = document.getElementById("popup-extra");
  if (!extra || msg.id !== pendingInspect) return;
  if (msg.gone) { extra.textContent = "El vehículo salió de la simulación"; return; }
  const f = (x, d = 1) => (x == null ? "–" : Number(x).toFixed(d));
  extra.style.opacity = "1";
  extra.innerHTML =
    `CO₂: <b>${f(msg.co2 / 1000, 2)} g/s</b> · Ruido: <b>${f(msg.noise)} dB</b><br>` +
    `Combustible: <b>${f(msg.fuel, 1)} mg/s</b><br>` +
    `Espera: <b>${f(msg.waiting, 0)} s</b> (acum. <b>${f(msg.waiting_acc, 0)} s</b>)<br>` +
    `Retraso vs. flujo libre: <b>${f(msg.timeloss, 0)} s</b><br>` +
    `Recorrido: <b>${f((msg.distance || 0) / 1000, 2)} km</b> · Carril: <b>${msg.lane || "–"}</b><br>` +
    `Ruta: tramo <b>${(msg.route_index ?? 0) + 1} / ${msg.route_edges ?? "–"}</b>`;
}

// --- WebSocket live stream --------------------------------------------------
// Each connection is tracked by identity (sock === ws). When we switch scenario
// or reset, the previous socket is superseded, so its late frames and its close
// event are ignored — this prevents two SUMO streams alternating (the flicker).
function connect() {
  const sock = new WebSocket(WS_URL + "?level=" + currentLevel +
                             (replayMode ? "&replay=1" : ""));
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
      // continuidad: el punto de partida de la interpolación es lo que está
      // dibujado AHORA (vehicles), no el frame crudo anterior
      interpPrev = new Map(vehicles.map((v) => [v.id, { lon: v.lon, lat: v.lat, angle: v.angle }]));
      const nowMs = performance.now();
      if (interpT0) {
        interpPeriod = Math.min(Math.max(0.8 * interpPeriod + 0.2 * (nowMs - interpT0), 30), 2000);
      }
      interpT0 = nowMs;
      frameVehicles = msg.vehicles;            // OBJETIVO de la interpolación
      // OJO: no asignar vehicles aquí — pintar ya la posición nueva provocaba
      // salto + retroceso en el primer tick. Los ticks deslizan hacia
      // frameVehicles; el refreshLayers() de abajo repinta LOS/semáforos con
      // los vehículos donde están dibujados ahora.
      // interp. desactivada con flotas enormes; y en modo paso (pausado) los
      // vehículos deben SALTAR a la posición del frame (la interp. no corre)
      if (paused || frameVehicles.length > INTERP_MAX_VEH) vehicles = frameVehicles;
      const now = performance.now();
      if (now - lastLosMs >= LOS_UPDATE_MS) {      // throttled LOS/count snapshot
        edgeColors = {}; edgeCounts = {}; edgeStats = {};
        for (const e of msg.edges) {
          edgeColors[e.id] = hexToRgb(e.color); edgeCounts[e.id] = e.n; edgeStats[e.id] = e;
        }
        losStamp++; lastLosMs = now;
      }
      tlState = msg.tls || {};
      // mensajes V2X en vivo: al llegar los primeros, revelar los controles
      // de mensajes y el panel PHY (estadísticas de la corrida EN CURSO,
      // refrescadas periódicamente vía /api/replay/info?live=1)
      if (!replayMode && msg.messages && !liveV2xSeen) {
        liveV2xSeen = true;
        els.rowMsgs.style.display = "";
        els.rowMsgFilter.style.display = "";
        els.rowVehFilter.style.display = "";
        const pp = document.getElementById("phy-panel");
        if (pp) { pp.style.display = ""; loadPhyStats(0, true); }
        if (liveV2xTimer) clearInterval(liveV2xTimer);
        liveV2xTimer = setInterval(() => { if (liveV2xSeen) loadPhyStats(0, true); }, 15000);
      }
      // filtro de emisor en vivo: poblar el selector con las estaciones que
      // van apareciendo (los vehículos traen station del backend), conservando
      // la selección actual si sigue existiendo
      if (!replayMode && liveV2xSeen) {
        let changed = false;
        for (const v of msg.vehicles) {
          if (v.station != null && !liveStations.has(v.station)) {
            liveStations.add(v.station); changed = true;
          }
        }
        if (changed) {
          const cur = els.vehFilter.value;
          els.vehFilter.innerHTML = '<option value="">All</option>' +
            [...liveStations].sort((a, b) => a - b)
              .map((s) => `<option value="${s}">veh${s}</option>`).join("");
          els.vehFilter.value = cur;
          if (els.vehFilter.value !== cur) { els.vehFilter.value = ""; selStation = null; }
        }
      }
      // eventos de mensajes V2X (replay y en vivo): capturar posiciones AHORA
      // para que pulso/arco queden anclados aunque el vehículo siga moviéndose
      if (msg.messages && showMsgs) {
        if (replayMode && paused) msgEvents = [];   // modo paso: solo ESTE paso
        const posOf = {};
        for (const v of msg.vehicles) posOf[v.station] = [v.lon, v.lat];
        const wall = performance.now();
        for (const e of msg.messages.tx) {
          if (posOf[e.tx]) msgEvents.push({ kind: "tx", type: e.type, simT: e.t,
            st: e.tx, pos: posOf[e.tx], wallT: wall });
        }
        for (const e of msg.messages.rx) {
          const pt = posOf[e.tx], pr = posOf[e.rx];
          if (pt && pr) {
            msgEvents.push({ kind: "rx", type: e.type, simT: e.t, st: e.tx,
              st2: e.rx, pos: pt, pos2: pr, wallT: wall, rssi: e.rssi,
              dist: Math.round(metersBetween(pt[0], pt[1], pr[0], pr[1])) });
          }
        }
        if (msgEvents.length > 3000) msgEvents = msgEvents.slice(-3000);
      }
      if (replayMode) {
        els.rpTimeCur.textContent = fmtClock(msg.t);
        if (!rpDragging) { els.rpSeek.value = msg.t; updateSeekFill(); }
      }
      if (msg.stats) updateHistory(msg.stats, frameVehicles.length, msg.t);
      els.vehCount.textContent = frameVehicles.length;
      els.simTime.textContent = msg.t;
      refreshLayers();
    } else if (msg.type === "meta") {
      const isReplay = msg.mode === "replay";
      liveV2xSeen = false;                       // nueva conexión: re-detectar V2X en vivo
      if (liveV2xTimer) { clearInterval(liveV2xTimer); liveV2xTimer = null; }
      liveStations = new Set();
      if (!isReplay) {                           // filtro emisor limpio para la corrida nueva
        els.vehFilter.innerHTML = '<option value="">All</option>';
        selStation = null;
      }
      els.replayBar.style.display = isReplay ? "" : "none";
      els.stepRow.style.display = isReplay ? "" : "none";
      els.rowVehFilter.style.display = isReplay ? "" : "none";
      els.rpSpeedRow.style.display = isReplay ? "" : "none";
      if (msg.step_length) metaStepLength = msg.step_length;
      if (isReplay) { rpMultIdx = 2; applyReplaySpeed(); }   // arrancar a 1× real
      els.rowMsgs.style.display = isReplay ? "" : "none";
      els.rowMsgFilter.style.display = isReplay ? "" : "none";
      if (isReplay) {
        replayT0 = msg.t0 || 0; replayT1 = msg.t1 || 0;
        els.rpSeek.min = replayT0; els.rpSeek.max = replayT1;
        els.rpSeek.value = replayT0;
        els.rpTimeCur.textContent = fmtClock(replayT0);
        els.rpTimeTotal.textContent = fmtClock(replayT1);
        updateSeekFill();
        els.conn.textContent = "replay pcap";
        // selector de vehículo emisor (stationID reales de la corrida)
        const st = (msg.replay && msg.replay.stations) || [];
        els.vehFilter.innerHTML = '<option value="">All</option>' +
          st.map((s) => `<option value="${s}">veh${s}</option>`).join("");
        selStation = null;
        // desplegar el panel Replay V2X al entrar en modo replay
        const rp = document.getElementById("replay-panel");
        if (rp) rp.classList.remove("collapsed");
        // panel PHY: estadísticas de capa física de la corrida
        const pp = document.getElementById("phy-panel");
        if (pp) { pp.style.display = ""; loadPhyStats(); }
      } else {
        const pp = document.getElementById("phy-panel");
        if (pp) pp.style.display = "none";
      }
    } else if (msg.type === "msg_detail") {
      showMsgDetail(msg);
    } else if (msg.type === "inspect") {
      applyInspect(msg);
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
// estado de pausa sincronizado entre el botón principal y el ⏸/▶ del panel Replay
function setPaused(v) {
  paused = v;
  els.play.textContent = v ? "▶ Reanudar" : "⏸ Pausar";
  if (els.rpPlay) els.rpPlay.textContent = v ? "▶" : "⏸";
}
els.play.onclick = () => {
  setPaused(!paused);
  send({ cmd: paused ? "pause" : "play" });
};
els.reset.onclick = () => {
  if (ws) ws.close();
  vehicles = []; frameVehicles = []; interpPrev = new Map(); interpT0 = 0;
  edgeColors = {}; refreshLayers();
  paused = false; els.play.textContent = "⏸ Pausar";
  connect();
};
els.fps.oninput = () => {
  els.fpsVal.textContent = `${els.fps.value} fps`;
  send({ cmd: "speed", fps: Number(els.fps.value) });
};

// --- replay: alternar modo, timeline y mensajes ------------------------------
els.replayBtn.onclick = () => {
  replayMode = !replayMode;
  els.replayBtn.textContent = replayMode ? "🔴 Volver al modo en vivo" : "🎞 Replay pcap";
  if (ws) ws.close();
  vehicles = []; frameVehicles = []; interpPrev = new Map(); interpT0 = 0;
  msgEvents = []; edgeColors = {}; tlState = {}; refreshLayers();
  paused = false; els.play.textContent = "⏸ Pausar";
  if (!replayMode) {
    els.replayBar.style.display = "none";
    els.stepRow.style.display = "none";
    els.rowVehFilter.style.display = "none";
    els.rpSpeedRow.style.display = "none";
    els.rowMsgs.style.display = "none";
    els.rowMsgFilter.style.display = "none";
    const pp = document.getElementById("phy-panel");
    if (pp) pp.style.display = "none";
  }
  connect();
};
// paso a paso: avanza APP_STEP_LENGTH seg simulados y pausa; los mensajes del
// paso quedan congelados en pantalla (clic en un pulso = contenido)
els.rpPlay.onclick = () => {
  setPaused(!paused);
  send({ cmd: paused ? "pause" : "play" });
};
els.rpStep.onclick = () => {
  setPaused(true);
  send({ cmd: "step" });
};
els.rpBack.onclick = () => {
  setPaused(true);
  interpPrev = new Map(); interpT0 = 0;   // sin interpolar el salto hacia atrás
  send({ cmd: "step_back" });
};
els.vehFilter.onchange = () => {
  selStation = els.vehFilter.value === "" ? null : Number(els.vehFilter.value);
  refreshLayers();
};
// velocidad de reproducción del replay: multiplicador sobre TIEMPO REAL.
// Cadencia de render FIJA a 10 fps (como el modo vivo, que se ve fluido) y lo
// que varía es el avance simulado por frame: mult/10 s (1× -> 0.1 s/frame).
// El primer intento (2 fps × 0.5 s a 1×) era matemáticamente correcto pero
// interpolar tramos de 500 ms se veía robótico y con micro-pausas; las
// trayectorias del pcap son continuas, así que el backend puede muestrear a
// cualquier paso (cmd "speed" acepta "step" — requiere backend reconstruido).
const RP_RENDER_FPS = 10;
const RP_MULTS = [0.25, 0.5, 1, 2, 4];
let rpMultIdx = 2;                                 // 1× por defecto
let metaStepLength = 0.5;                          // se actualiza con el meta del WS
function applyReplaySpeed() {
  const m = RP_MULTS[rpMultIdx];
  els.rpSpeed.textContent = (m < 1 ? String(m).replace("0.", ".") : m) + "×";
  send({ cmd: "speed", fps: RP_RENDER_FPS, step: m / RP_RENDER_FPS });
  // sembrar el período esperado: la media móvil tardaba ~10 frames en
  // converger tras cada cambio de cadencia y mientras tanto tartamudeaba
  interpPeriod = 1000 / RP_RENDER_FPS;
}
els.rpSlow.onclick = () => { if (rpMultIdx > 0) { rpMultIdx--; applyReplaySpeed(); } };
els.rpFast.onclick = () => { if (rpMultIdx < RP_MULTS.length - 1) { rpMultIdx++; applyReplaySpeed(); } };
els.rpSeek.oninput = () => {
  rpDragging = true;
  els.rpTimeCur.textContent = fmtClock(els.rpSeek.value);
  updateSeekFill();
  send({ cmd: "seek", t: Number(els.rpSeek.value) });
  interpPrev = new Map(); interpT0 = 0; msgEvents = [];   // sin interpolar el salto
};
els.rpSeek.onchange = () => {
  rpDragging = false;
  if (paused) { setPaused(false); send({ cmd: "play" }); }
};
els.msgsToggle.onchange = () => {
  showMsgs = els.msgsToggle.checked;
  if (!showMsgs) msgEvents = [];
  refreshLayers();
};
els.arcLbl.onchange = () => {
  showArcLabels = els.arcLbl.checked;
  refreshLayers();
};

// clic izquierdo sobre un pulso de transmisión -> contenido ASN.1 del mensaje
document.getElementById("map").addEventListener("click", (e) => {
  if (!replayMode || !overlay) return;
  const rect = e.currentTarget.getBoundingClientRect();
  const info = overlay.pickObject({ x: e.clientX - rect.left, y: e.clientY - rect.top,
                                    layerIds: ["v2x-pulse", "v2x-arc"], radius: 10 });
  if (!info || !info.object) return;
  send({ cmd: "inspect", station: info.object.st, t: info.object.simT,
         mtype: info.object.type });
  const popup = document.getElementById("popup");
  popup.classList.add("interactive");        // permitir ratón: scroll del contenido
  popup.style.display = "block";
  popup.style.left = Math.min(e.clientX + 14, window.innerWidth - 420) + "px";
  popup.style.top = Math.min(e.clientY + 14, window.innerHeight - 420) + "px";
  popup.innerHTML = `<h3>Mensaje ${info.object.type} · veh${info.object.st}</h3>cargando contenido…`;
});

// panel PHY 802.11p: métricas de la corrida derivadas de los pcap.
// Con reintentos: justo tras un build/up el backend puede tardar unos
// segundos (nginx devuelve su página HTML de error 502 mientras tanto).
async function loadPhyStats(attempt = 0, live = false) {
  const body = document.getElementById("phy-body");
  if (!body) return;
  const retry = () => { body.textContent = `cargando estadísticas… (intento ${attempt + 2})`;
                        setTimeout(() => loadPhyStats(attempt + 1, live), 2500); };
  try {
    const res = await fetch(API + "/api/replay/info" + (live ? "?live=1" : ""),
                            { cache: "no-store" });
    const ct = res.headers.get("content-type") || "";
    if (res.status === 404) {
      body.textContent = "el backend no tiene /api/replay/info: reconstrúyelo " +
        "(docker compose --profile visor build backend && ... up -d)";
      return;
    }
    if (!res.ok || !ct.includes("json")) {
      if (attempt < 6) return retry();
      body.textContent = `backend no disponible (HTTP ${res.status}): revisa ` +
        `docker compose logs backend`;
      return;
    }
    const info = await res.json();
    if (info.error) { body.textContent = "replay sin cargar: " + info.error; return; }
    const p = info.phy || {};
    const f = (x, d = 1) => (x == null ? "–" : Number(x).toFixed(d));
    const c = p.config || {}, l = p.latency_ms || {}, r = p.range_m || {};
    phyRangeM = r.p95 || r.max || null;   // el pulso TX se expande hasta aquí
    const pdrOne = (o) => { const k = Object.keys(o || {})[0];
                            return k ? `${k.replace("->", "→")}: ${(o[k] * 100).toFixed(1)}%` : "–"; };
    const rates = Object.entries(p.rates_per_s || {})
      .map(([k, v]) => `${k} ${f(v)}/s`).join(" · ") || "–";
    body.innerHTML =
      `<b>Banda:</b> ${c.banda || "–"}<br>` +
      `<b>Canal:</b> ${c.canal || "–"} · <b>BW:</b> ${c.bw || "–"}<br>` +
      `<b>Modulación:</b> ${c.modulacion || "–"}<br>` +
      `<b>Potencia TX:</b> ${c.tx_power || "–"}<br>` +
      `<div class="phy-sep"></div>` +
      (p.rssi_dbm
        ? `<b>RSSI:</b> media ${f(p.rssi_dbm.mean)} · p50 ${f(p.rssi_dbm.p50)} · ` +
          `mín ${f(p.rssi_dbm.min)} dBm <span style="opacity:.6">(${p.rssi_dbm.n} muestras)</span><br>`
        : "") +
      `<b>Latencia TX→RX:</b> media ${f(l.mean)} · p95 ${f(l.p95)} · máx ${f(l.max)} ms<br>` +
      `<b>Cobertura observada:</b> p50 ${f(r.p50, 0)} · p95 ${f(r.p95, 0)} · máx ${f(r.max, 0)} m<br>` +
      `<b>PER global:</b> ${p.per == null ? "–" : (p.per * 100).toFixed(1) + "%"}` +
      ` <span style="opacity:.6">(${p.got_rx ?? "?"}/${p.expected_rx ?? "?"} RX)</span><br>` +
      `<b>PDR mejor par:</b> ${pdrOne(p.pdr_best)}<br>` +
      `<b>PDR peor par:</b> ${pdrOne(p.pdr_worst)}<br>` +
      `<b>Tasas TX:</b> ${rates}<br>` +
      `<b>Canal ocupado:</b> ~${p.channel_util == null ? "–" : (p.channel_util * 100).toFixed(1)}%` +
      ` · <b>Tramas:</b> ${p.frames ? p.frames.tx + " TX / " + p.frames.rx + " RX" : "–"}<br>` +
      `<span style="opacity:.55;font-size:11px">${c.nota_rx || ""}</span>`;
  } catch (e) {
    if (attempt < 6) return retry();
    body.textContent = "estadísticas PHY no disponibles: " + e.message;
  }
}

function showMsgDetail(d) {
  const popup = document.getElementById("popup");
  popup.classList.add("interactive");        // scroll con la rueda dentro del popup
  popup.style.display = "block";
  const rec = (d.receivers_info && d.receivers_info.length)
    ? d.receivers_info.map((i) =>
        `veh${i.rx} (${i.dist_m != null ? i.dist_m + " m" : "?"}` +
        `${i.rssi != null ? ", " + i.rssi + " dBm" : ""})`).join(" · ")
    : ((d.receivers || []).map((r) => "veh" + r).join(", ") || "ninguno");
  let html = `<h3><span class="popup-close" title="Cerrar">✕</span>` +
             `${d.mtype} · veh${d.tx} · t = ${(d.t ?? 0).toFixed(3)} s</h3>` +
             `Receptores: <b>${rec}</b> · ${d.bytes ?? "?"} bytes ASN.1 UPER`;
  // disección por capas, estilo Wireshark: secciones plegables
  const sec = (title, obj, open) => obj ?
    `<details${open ? " open" : ""}><summary>${title}</summary>` +
    `<pre>${JSON.stringify(obj, null, 1)}</pre></details>` : "";
  const L = d.layers || {};
  html += sec("IEEE 802.11", L.ieee80211, false) +
          sec("GeoNetworking · SHB", L.geonetworking, false) +
          sec("BTP-B", L.btp, false);
  if (d.content) {
    html += sec(`ITS · ${d.mtype} (ASN.1)`, d.content, true);
  } else if (d.decode_error) {
    html += `<br><span style="opacity:.7">sin decodificar: ${d.decode_error}</span>`;
  } else if (d.error) {
    html += `<br><span style="opacity:.7">${d.error}</span>`;
  }
  popup.innerHTML = html;
  const x = popup.querySelector(".popup-close");
  if (x) x.onclick = () => { popup.style.display = "none";
                             popup.classList.remove("interactive"); };
}
els.buildings.onchange = () => setBuildingsVisible(els.buildings.checked);
els.congestion.onchange = () => { showCongestion = els.congestion.checked; refreshLayers(); };
els.tl.onchange = () => { showTL = els.tl.checked; refreshLayers(); };
els.poi.onchange = () => setPoiVisible(els.poi.checked);
els.busy.onchange = () => { showBusy = els.busy.checked; refreshLayers(); };
els.busyMin.oninput = () => { busyMin = Number(els.busyMin.value); els.busyVal.textContent = busyMin; refreshLayers(); };
els.detect.onchange = () => { showDetection = els.detect.checked; refreshLayers(); };
els.sensorMode.onchange = () => applySensorMode(els.sensorMode.value);

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
    stopOrbit(); stopFollow();
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
  stopFollow();
  els.orbit.classList.add("active");            // start a slow continuous orbit
  const spin = () => { map.setBearing(map.getBearing() + 0.15); orbitRAF = requestAnimationFrame(spin); };
  orbitRAF = requestAnimationFrame(spin);
};

// --- cámara "cockpit" (inspirado en gods-eye-view: ride inside a tracked
// vehicle) — sigue a un vehículo por id, orientada a su rumbo real (SUMO usa
// la misma convención 0=N horario que el bearing de MapLibre: sin conversión).
let followId = null, followPrevCam = null;
function startFollow(id) {
  if (!map) return;
  if (!followPrevCam) {
    followPrevCam = { center: map.getCenter(), zoom: map.getZoom(),
                       bearing: map.getBearing(), pitch: map.getPitch() };
  }
  stopOrbit();
  followId = id;
  els.followBadge.style.display = "flex";
  els.followVeh.textContent = id;
}
function stopFollow() {
  if (followId === null) return;
  followId = null;
  els.followBadge.style.display = "none";
  if (followPrevCam) { map.easeTo({ ...followPrevCam, duration: 500 }); followPrevCam = null; }
}
els.followExit.onclick = stopFollow;
// Se llama en cada tick de interpolación (misma cadencia que el movimiento de
// los vehículos) para que la cámara vaya tan fluida como el propio tráfico.
function updateFollowCamera() {
  if (followId === null || !map) return;
  const v = vehicles.find((x) => x.id === followId);
  if (!v) { stopFollow(); return; }              // el vehículo salió de la simulación
  map.jumpTo({ center: [v.lon, v.lat], bearing: v.angle || 0, pitch: 76, zoom: 19.3 });
  els.followHud.textContent = `· rumbo ${Math.round(v.angle || 0)}° · ${Math.round((v.speed || 0) * 3.6)} km/h`;
}

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

// --- auto-hide panels: collapse to their title bar when the pointer leaves,
//     expand on hover (shown briefly on load so the controls are found).
//     El 📌 fija el panel (no se auto-colapsa); se recuerda en localStorage.
for (const id of ["panel", "histpanel", "replay-panel", "phy-panel"]) {
  const p = document.getElementById(id);
  if (!p) continue;
  let pinned = false;
  try { pinned = localStorage.getItem("pin-" + id) === "1"; } catch (_) {}
  const pin = document.createElement("span");
  pin.className = "pin";
  pin.textContent = "📌";
  pin.title = "Fijar este panel (no auto-ocultar)";
  const apply = () => {
    pin.classList.toggle("on", pinned);
    if (pinned) p.classList.remove("collapsed");
  };
  pin.onclick = (e) => {
    e.stopPropagation();
    pinned = !pinned;
    try { localStorage.setItem("pin-" + id, pinned ? "1" : "0"); } catch (_) {}
    apply();
  };
  const h = p.querySelector("h1");
  if (h) h.appendChild(pin);
  apply();
  let t = null;
  p.addEventListener("mouseenter", () => { clearTimeout(t); p.classList.remove("collapsed"); });
  p.addEventListener("mouseleave", () => {
    clearTimeout(t);
    if (!pinned) t = setTimeout(() => p.classList.add("collapsed"), 600);
  });
  setTimeout(() => { if (!pinned) p.classList.add("collapsed"); }, 2500);
}

boot().catch((e) => { els.conn.textContent = "error: " + e.message; });
