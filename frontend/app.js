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
  buildings: document.getElementById("toggle-buildings"),
  congestion: document.getElementById("toggle-congestion"),
  tl: document.getElementById("toggle-tl"),
  rotLeft: document.getElementById("rot-left"),
  rotRight: document.getElementById("rot-right"),
  rotNorth: document.getElementById("rot-north"),
  orbit: document.getElementById("orbit"),
  viewTop: document.getElementById("view-top"),
  zoomIn: document.getElementById("zoom-in"),
  zoomOut: document.getElementById("zoom-out"),
  vehCount: document.getElementById("veh-count"),
  simTime: document.getElementById("sim-time"),
  conn: document.getElementById("conn"),
};

let map, overlay, ws;
let networkGeo = { type: "FeatureCollection", features: [] };
let edgeColors = {};      // edgeId -> [r,g,b]
let vehicles = [];        // latest frame vehicles
let tlDefs = { type: "FeatureCollection", features: [] };  // static signal positions
let tlState = {};         // tlsID -> live state string ("GrGr...")
let paused = false;
let showCongestion = true;
let showTL = true;

// speed (m/s) -> colour ramp (red slow → green fast)
function speedColor(v) {
  const t = Math.max(0, Math.min(1, v / 14));     // ~50 km/h reference
  return [Math.round(230 * (1 - t)) + 20, Math.round(200 * t) + 40, 70];
}
function hexToRgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
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
    bearing: -18,
    antialias: true,
  });
  map.on("dragstart", stopOrbit);       // any manual pan/rotate cancels auto-orbit
  map.on("rotatestart", stopOrbit);
  map.on("moveend", syncFromPitch);     // keep the 2D/3D label in sync with gestures

  map.on("load", async () => {
    // --- extruded building polygons (native MapLibre fill-extrusion) --------
    const buildings = await fetchJSON("/api/buildings");
    map.addSource("buildings", { type: "geojson", data: buildings });
    map.addLayer({
      id: "buildings-3d",
      type: "fill-extrusion",
      source: "buildings",
      paint: {
        "fill-extrusion-color": [
          "interpolate", ["linear"], ["get", "height"],
          0, "#7f8fa6", 20, "#9aa7b8", 45, "#c3ccd6",
        ],
        "fill-extrusion-height": ["get", "height"],
        "fill-extrusion-opacity": 0.85,
      },
    });

    // --- road network (deck.gl overlay, recoloured by congestion) ----------
    networkGeo = await fetchJSON("/api/network");
    tlDefs = await fetchJSON("/api/trafficlights");
    splitTrafficLights();
    overlay = new deck.MapboxOverlay({ interleaved: true, layers: buildLayers() });
    map.addControl(overlay);

    connect();
  });
}

function buildLayers() {
  const layers = [
    new deck.GeoJsonLayer({
      id: "network",
      data: networkGeo,
      lineWidthUnits: "meters",
      getLineWidth: 3,
      lineWidthMinPixels: 1.5,
      getLineColor: (f) =>
        (showCongestion && edgeColors[f.properties.id]) || [120, 130, 145],
      updateTriggers: { getLineColor: [edgeColors, showCongestion] },
      pickable: false,
    }),
  ];

  // traffic lights, coloured live from SUMO. Open junctions get a mast-arm
  // ("ménsula") with the head hanging over the road; tight ones a straight pole.
  if (showTL && tlDefs.features.length) {
    layers.push(new deck.ColumnLayer({
      id: "tl-pole-mast", data: tlMast, diskResolution: 6, radius: 0.14, extruded: true,
      getPosition: poleBase, getFillColor: [30, 33, 40], getElevation: ARM_HEIGHT,
    }));
    layers.push(new deck.PathLayer({
      id: "tl-arm", data: tlMast, getPath: signalArm, getColor: [30, 33, 40],
      getWidth: 0.22, widthUnits: "meters", widthMinPixels: 2, capRounded: true, jointRounded: true,
    }));
    layers.push(new deck.IconLayer({
      id: "tl-head-mast", data: tlMast, billboard: true,
      getIcon: (f) => SIGNAL_ICON_HANG[signalKey(f)], getPosition: signalPosition,
      getSize: 44, sizeUnits: "pixels", sizeMinPixels: 12, sizeMaxPixels: 66,
      updateTriggers: { getIcon: [tlState] },
    }));
    layers.push(new deck.ColumnLayer({
      id: "tl-pole-straight", data: tlStraight, diskResolution: 6, radius: 0.14, extruded: true,
      getPosition: poleBase, getFillColor: [30, 33, 40], getElevation: STRAIGHT_H,
    }));
    layers.push(new deck.IconLayer({
      id: "tl-head-straight", data: tlStraight, billboard: true,
      getIcon: (f) => SIGNAL_ICON_TOP[signalKey(f)], getPosition: signalHeadTop,
      getSize: 40, sizeUnits: "pixels", sizeMinPixels: 10, sizeMaxPixels: 60,
      updateTriggers: { getIcon: [tlState] },
    }));
  }

  layers.push(new deck.ScatterplotLayer({
    id: "vehicles",
    data: vehicles,
    getPosition: (d) => [d.lon, d.lat],
    getFillColor: (d) => speedColor(d.speed),
    getRadius: 5,
    radiusUnits: "meters",
    radiusMinPixels: 2.5,
    stroked: true,
    getLineColor: [10, 12, 18],
    lineWidthMinPixels: 0.5,
    pickable: true,
  }));

  return layers;
}

function refreshLayers() {
  if (overlay) overlay.setProps({ layers: buildLayers() });
}

// --- WebSocket live stream --------------------------------------------------
let manualClose = false;
function connect() {
  manualClose = false;
  ws = new WebSocket(WS_URL);
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); if (!manualClose) setTimeout(connect, 2000); };
  ws.onerror = () => setConn(false);
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "frame") {
      vehicles = msg.vehicles;
      edgeColors = {};
      for (const e of msg.edges) edgeColors[e.id] = hexToRgb(e.color);
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
  if (ws) { manualClose = true; ws.close(); }
  vehicles = []; edgeColors = {}; refreshLayers();
  paused = false; els.play.textContent = "⏸ Pausar";
  connect();
};
els.fps.oninput = () => {
  els.fpsVal.textContent = `${els.fps.value} fps`;
  send({ cmd: "speed", fps: Number(els.fps.value) });
};
els.buildings.onchange = () => {
  if (map.getLayer("buildings-3d"))
    map.setLayoutProperty("buildings-3d", "visibility", els.buildings.checked ? "visible" : "none");
};
els.congestion.onchange = () => { showCongestion = els.congestion.checked; refreshLayers(); };
els.tl.onchange = () => { showTL = els.tl.checked; refreshLayers(); };

// --- map rotation ---------------------------------------------------------
els.rotLeft.onclick = () => map && map.easeTo({ bearing: map.getBearing() - 30, duration: 300 });
els.rotRight.onclick = () => map && map.easeTo({ bearing: map.getBearing() + 30, duration: 300 });
els.rotNorth.onclick = () => map && map.easeTo({ bearing: 0, duration: 400 });

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

// --- top-down (bird's-eye) view toggle ------------------------------------
let topView = false;                              // false = 3D perspective, true = looking straight down
function setViewLabel(top) {
  topView = top;
  els.viewTop.textContent = top ? "3D" : "2D";    // label = the view the next click gives you
  els.viewTop.classList.toggle("active", top);
}
els.viewTop.onclick = () => {
  if (!map) return;
  const goTop = !topView;
  map.easeTo({ pitch: goTop ? 0 : 55, duration: 500 });
  setViewLabel(goTop);                            // optimistic; moveend confirms from real pitch
};
function syncFromPitch() { if (map) setViewLabel(map.getPitch() <= 5); }

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

boot().catch((e) => { els.conn.textContent = "error: " + e.message; });
