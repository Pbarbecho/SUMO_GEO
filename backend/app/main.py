"""FastAPI application: static geometry over REST, live state over WebSocket.

Endpoints
---------
GET  /api/health      -> liveness + active SUMO mode (+ coste por frame del hub)
GET  /api/meta        -> map center, bounds, origin, step length
GET  /api/network     -> road edges as GeoJSON (cached, pre-gzipped)
GET  /api/buildings   -> building polygons as GeoJSON with height (cached)
WS   /ws/live         -> per-step frames (protocolo v2, ver frames.py)

Modo ``remote`` (VaN3Twin): todos los WebSockets se suscriben a la conexión
TraCI persistente de ``live_hub.py``. Modo ``managed``: cada WebSocket lanza y
controla su propio SUMO. Client -> server control messages:
``{"cmd":"pause"}`` / ``{"cmd":"play"}`` / ``{"cmd":"speed","fps":20}``.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .frames import FrameBuilder, encode
from .geo import (NetworkGeo, buildings_geojson, building_vertices_local,
                  cfg_paths, landuse_geojson, trafficlights_geojson,
                  trees_geojson, walkways_geojson)
from .sumo_bridge import SumoBridge

_state: dict = {}


class _Static:
    """GeoJSON estático serializado (orjson) y comprimido UNA vez al arrancar:
    la red metropolitana (34k aristas) son varios MB por recarga de página y
    comprimirlos en cada petición costaba ~100 ms de CPU. Con ETag el
    navegador ni siquiera vuelve a descargarlos si no cambiaron."""

    def __init__(self, obj: dict):
        self.raw = encode(obj).encode()
        self.gz = gzip.compress(self.raw, compresslevel=6)
        self.etag = '"' + hashlib.sha1(self.raw).hexdigest()[:16] + '"'

    def response(self, request: Request) -> Response:
        if request.headers.get("if-none-match") == self.etag:
            return Response(status_code=304, headers={"ETag": self.etag})
        headers = {"ETag": self.etag, "Cache-Control": "no-cache",
                   "Vary": "Accept-Encoding"}
        if "gzip" in request.headers.get("accept-encoding", ""):
            headers["Content-Encoding"] = "gzip"
            return Response(self.gz, media_type="application/json", headers=headers)
        return Response(self.raw, media_type="application/json", headers=headers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    net_file = settings.net_file
    poly_file = settings.poly_file
    if not net_file or not poly_file:
        cfg_net, cfg_poly = cfg_paths(settings.sumo_config)
        net_file = net_file or cfg_net
        poly_file = poly_file or cfg_poly
    netgeo = NetworkGeo(net_file)
    _state["netgeo"] = netgeo
    _state["network"] = _Static(netgeo.edges_geojson())
    _state["buildings"] = _Static(buildings_geojson(poly_file, netgeo))
    _state["trafficlights"] = _Static(trafficlights_geojson(
        netgeo, building_vertices_local(poly_file)))
    # entorno: zonas verdes/agua/parkings, árboles y aceras/cruces (si la red
    # se generó con --sidewalks.guess --crossings.guess)
    landuse = landuse_geojson(poly_file, netgeo)
    trees = trees_geojson(poly_file, netgeo)
    walkways = walkways_geojson(net_file, netgeo)
    _state["landuse"] = _Static(landuse)
    _state["trees"] = _Static(trees)
    _state["walkways"] = _Static(walkways)
    meta = {
        **netgeo.bounds_center(),
        "origin": [settings.origin_lon, settings.origin_lat],
        "step_length": settings.step_length,
        "mode": settings.sumo_mode,
        "proto": 2,
        "landuse": len(landuse["features"]),
        "trees": len(trees["features"]),
        "sidewalks": sum(1 for f in walkways["features"]
                         if f["properties"]["kind"] == "sidewalk"),
        "crossings": sum(1 for f in walkways["features"]
                         if f["properties"]["kind"] == "crossing"),
    }
    if settings.view_lon is not None and settings.view_lat is not None:
        meta["center"] = [settings.view_lon, settings.view_lat]   # open on the demand area
    _state["meta"] = meta
    hub = None
    if settings.sumo_mode == "remote":
        # conexión TraCI persistente compartida por todos los visores (ver
        # live_hub.py: SUMO multi-cliente no readmite clientes tras arrancar)
        from .live_hub import LiveHub
        hub = LiveHub(_state, _live_rep_nowait)
        hub.start()
        _state["hub"] = hub
    yield
    if hub is not None:
        await hub.stop()
    _state.clear()


app = FastAPI(title="SUMO-GEO API", version="0.2.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.cors_origins == "*"
    else [o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    out = {"status": "ok", "mode": settings.sumo_mode, "proto": 2}
    hub = _state.get("hub")
    if hub is not None:
        out["sumo"] = hub.status_msg()          # waiting | running | ended
    return out


@app.get("/api/meta")
async def meta():
    return _state["meta"]


@app.get("/api/network")
async def network(request: Request):
    return _state["network"].response(request)


@app.get("/api/buildings")
async def buildings(request: Request):
    return _state["buildings"].response(request)


@app.get("/api/trafficlights")
async def trafficlights(request: Request):
    return _state["trafficlights"].response(request)


@app.get("/api/landuse")
async def landuse(request: Request):
    return _state["landuse"].response(request)


@app.get("/api/trees")
async def trees(request: Request):
    return _state["trees"].response(request)


@app.get("/api/walkways")
async def walkways(request: Request):
    return _state["walkways"].response(request)


def _replay_fingerprint():
    """Huella del contenido del replay_dir (pcaps + signal*.csv): si cambia,
    hay una corrida nueva y el índice cacheado debe recargarse."""
    import glob as _g
    files = sorted(
        _g.glob(os.path.join(settings.replay_dir, settings.replay_pattern)) +
        _g.glob(os.path.join(settings.replay_dir, "signal*.csv")))
    return tuple((f, os.path.getmtime(f), os.path.getsize(f)) for f in files)


def _get_replay():
    """Índice de replay cacheado, con recarga automática si los ficheros de
    replay_dir cambiaron (ya no hace falta reiniciar el backend al copiar
    una corrida nueva a ~/results)."""
    fp = _replay_fingerprint()
    rep = _state.get("replay")
    if rep is None or getattr(rep, "_fp", None) != fp:
        from .replay import ReplayData
        rep = ReplayData(settings.replay_dir, settings.asn_dir,
                         settings.replay_pattern)
        rep._fp = fp
        _state["replay"] = rep
    return rep


_live: dict = {"rep": None, "fp": None, "next": 0.0, "busy": False,
               "phy": None, "phy_t": 0.0}
_LIVE_PHY_EVERY_S = 12.0       # recálculo PHY como mucho cada tanto (es O(eventos))


def _live_build(want_phy: bool):
    """Corre EN UN THREAD: escanea live_pcap_dir y, si la huella cambió,
    actualiza el índice en vivo de forma INCREMENTAL (solo lee los bytes que
    cada pcap ha añadido desde la última vez, ver ReplayData.refresh). Si toca,
    calcula también las stats PHY AQUÍ (nunca en la petición del panel).
    Devuelve (rep, fp, dur_s, phy|None) o None si no hay nada nuevo."""
    import glob as _g
    import time as _time
    files = sorted(
        _g.glob(os.path.join(settings.live_pcap_dir, "v2v-*.pcap")) +
        _g.glob(os.path.join(settings.live_pcap_dir, "signal*.csv")))
    if not files:
        return None
    try:
        fp = tuple((f, os.path.getmtime(f), os.path.getsize(f)) for f in files)
    except OSError:
        return None
    if fp == _live["fp"] and _live["rep"] is not None:
        return None
    try:
        from .replay import ReplayData
        t0 = _time.monotonic()
        rep = _live["rep"]
        if rep is not None and rep.same_run(files):
            rep.refresh()                        # incremental: solo bytes nuevos
        else:
            rep = ReplayData(settings.live_pcap_dir, settings.asn_dir,
                             "v2v-*.pcap", live=True)
        phy = rep.phy_stats(force=True) if want_phy else None
        return rep, fp, _time.monotonic() - t0, phy
    except Exception:  # noqa: BLE001
        return None


async def _live_refresh_bg():
    """Tarea de fondo: reconstruye el índice y lo intercambia al terminar.
    El throttle se adapta al coste real del parse para que el refresco nunca
    domine la CPU en corridas largas."""
    import time as _time
    try:
        now = _time.monotonic()
        want_phy = now - _live["phy_t"] > _LIVE_PHY_EVERY_S
        res = await asyncio.to_thread(_live_build, want_phy)
        if res is not None:
            _live["rep"], _live["fp"] = res[0], res[1]
            _live["next"] = _time.monotonic() + max(settings.live_refresh_s,
                                                    3.0 * res[2])
            if res[3] is not None:
                _live["phy"], _live["phy_t"] = res[3], _time.monotonic()
    finally:
        _live["busy"] = False


def _live_rep_nowait():
    """Índice V2X en vivo SIN bloquear jamás: devuelve el caché al instante y,
    si toca refrescar, lanza la reconstrucción como tarea de fondo."""
    import time as _time
    if not settings.live_pcap_dir:
        return None
    now = _time.monotonic()
    if now >= _live["next"] and not _live["busy"]:
        _live["busy"] = True
        if _live["next"] == 0.0:               # primer chequeo: fijar throttle base
            _live["next"] = now + max(settings.live_refresh_s, 0.5)
        asyncio.get_running_loop().create_task(_live_refresh_bg())
    return _live["rep"]


@app.get("/api/replay/info")
async def replay_info(live: int = 0):
    try:
        r = _live_rep_nowait() if live else await asyncio.to_thread(_get_replay)
        if r is None:
            raise FileNotFoundError("sin pcaps en vivo todavía")
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=404)
    if live:
        # solo caché: las PHY en vivo se calculan en la tarea de fondo con
        # throttle — nunca en el camino de la petición (pausaba el stream)
        phy = _live["phy"] or {}
    else:
        phy = await asyncio.to_thread(r.phy_stats)   # 1ª llamada: costosa
    return {"t0": r.t0, "t1": r.t1, "stations": r.stations, **r.stats,
            "phy": phy}


async def _ws_replay(ws: WebSocket):
    """Reproductor offline: misma cadencia/protocolo que el modo vivo, más
    `messages` (TX/RX) por frame, `seek`, e `inspect` de contenido ASN.1."""
    try:
        rep = _get_replay()
    except Exception as exc:  # noqa: BLE001
        await ws.send_text(encode({"type": "error", "message": f"replay: {exc}"}))
        await ws.close()
        return

    t = rep.t0
    paused = False
    single = False                     # modo paso a paso: un frame y re-pausa
    period = 1.0 / max(settings.max_fps, 0.1)
    step = settings.step_length
    builder = FrameBuilder(_state["netgeo"], los_every=1)
    await ws.send_text(encode({"type": "meta", **_state["meta"], "mode": "replay",
                               "t0": round(rep.t0, 2), "t1": round(rep.t1, 2),
                               "replay": rep.stats}))
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=0.001)
                cmd = json.loads(raw)
            except (asyncio.TimeoutError, json.JSONDecodeError):
                cmd = None
            except WebSocketDisconnect:
                return
            if cmd:
                action = cmd.get("cmd")
                if action == "pause":
                    paused = True
                elif action == "play":
                    paused = False
                elif action == "step":          # avanzar UN frame y pausar
                    paused, single = False, True
                elif action == "step_back":     # retroceder UN frame y pausar
                    t = max(rep.t0 - step, t - 2 * step)
                    paused, single = False, True
                elif action == "speed":
                    period = 1.0 / max(float(cmd.get("fps", settings.max_fps)), 0.1)
                    # avance simulado por frame opcional: permite al visor pedir
                    # muchos frames pequeños (10 fps × 0.1 s = 1× tiempo real,
                    # suave) en vez de pocos grandes (2 fps × 0.5 s, robótico)
                    if "step" in cmd:
                        step = min(max(float(cmd["step"]), 0.01), 10.0)
                elif action == "seek":
                    t = min(max(float(cmd.get("t", rep.t0)), rep.t0), rep.t1)
                elif action == "inspect":
                    # mensaje concreto: {"cmd":"inspect","station":N,"t":x}
                    # decode en thread: la 1a vez compila la spec ASN.1 y no
                    # debe congelar el stream de frames
                    if "station" in cmd:
                        det = await asyncio.to_thread(
                            rep.decode, int(cmd["station"]), float(cmd["t"]),
                            cmd.get("mtype"))
                        await ws.send_text(encode({"type": "msg_detail", **det}))
                    else:                       # vehículo: cinemática del replay
                        vid = str(cmd.get("id", ""))
                        v = next((x for x in rep.positions(t) if x["id"] == vid), None)
                        await ws.send_text(encode(
                            {"type": "inspect",
                             **({"id": vid, "gone": True} if v is None
                                else {"id": vid, "lane": "", "distance": None})}))
            if paused:
                await asyncio.sleep(0.05)
                continue

            t_next = min(t + step, rep.t1)
            vehs = rep.positions(t_next)
            win = rep.window(t, t_next)
            frame = builder.build(t_next, vehs,
                                  {"co2": 0, "wait": 0, "wait_n": 0, "tt": None,
                                   "arrived": 0},
                                  None, None, with_station=False)
            frame["messages"] = win
            await ws.send_text(encode(frame))
            if single:
                paused, single = True, False     # paso dado: re-pausar
            if t_next >= rep.t1:
                await ws.send_text(encode({"type": "end", "t": round(t_next, 2)}))
                paused = True                    # permitir seek hacia atrás
            t = t_next
            await asyncio.sleep(period)
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
    if ws.query_params.get("replay"):
        await _ws_replay(ws)
        return
    hub = _state.get("hub")
    if hub is not None:                # modo remote: suscribirse al hub
        await hub.serve(ws)
        return
    # modo managed: cada WebSocket lanza y controla su propio SUMO
    netgeo = _state["netgeo"]
    bridge = SumoBridge()

    # traffic level (low/mid/high) selected in the app -> its own scenario file
    level = (ws.query_params.get("level") or "").lower()
    cfg, begin = None, None
    if level in ("low", "mid", "high"):
        candidate = settings.sumo_config_template.format(level=level)
        if os.path.exists(candidate):
            cfg, begin = candidate, settings.sim_begin

    try:
        await asyncio.to_thread(bridge.start, cfg, begin)
    except Exception as exc:  # noqa: BLE001 - surface the reason to the client + logs
        import traceback
        traceback.print_exc()  # real cause visible in `docker compose logs backend`
        for attempt in (
            lambda: ws.send_text(encode({"type": "error",
                                         "message": f"SUMO start failed: {exc}"})),
            lambda: ws.close(),
        ):
            try:
                await attempt()
            except Exception:
                pass
        return

    paused = False
    period = 1.0 / max(settings.max_fps, 0.1)
    builder = FrameBuilder(netgeo, settings.los_every)
    sent_v2x: set = set()      # dedup de eventos V2X ya enviados (modo en vivo)
    await ws.send_text(encode({"type": "meta", **_state["meta"]}))

    async def read_command():
        """Non-blocking drain of a pending client control message."""
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=0.001)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def collect():
        """En un hilo: el paso TraCI no bloquea el event loop (otros WS)."""
        t = bridge.step()
        vehs = bridge.vehicles(netgeo)
        frame = builder.build(t, vehs, bridge.frame_stats(), bridge.edge_agg,
                              bridge.trafficlights_if_changed(),
                              with_station=bool(settings.live_pcap_dir))
        return t, frame

    loop = asyncio.get_running_loop()
    try:
        while True:
            cmd = await read_command()
            if cmd:
                action = cmd.get("cmd")
                if action == "pause":
                    paused = True
                elif action == "play":
                    paused = False
                elif action == "speed":
                    period = 1.0 / max(float(cmd.get("fps", settings.max_fps)), 0.1)
                elif action == "inspect":
                    # extended SUMO stats for one vehicle (right-click inspector)
                    vid = str(cmd.get("id", ""))
                    try:
                        details = await asyncio.to_thread(bridge.vehicle_details, vid)
                    except Exception:
                        details = {"id": vid, "gone": True}
                    await ws.send_text(encode({"type": "inspect", **details}))

            if paused:
                await asyncio.sleep(0.05)
                continue

            t0 = loop.time()
            t, frame = await asyncio.to_thread(collect)
            # --- mensajes V2X en vivo: leer los pcap que ns-3 escribe durante
            # la corrida (montados RO en live_pcap_dir) y adjuntar los eventos
            # aún no enviados. station = nº del id SUMO (== stationID ETSI en
            # el mapeo del replay); el visor ancla pulsos/arcos por station.
            if settings.live_pcap_dir:
                rep = _live_rep_nowait()       # nunca bloquea el stream de frames
                if rep is not None:
                    win = rep.window(t - 6.0, t)   # margen: ns-3 vuelca con retraso
                    msgs = {"tx": [], "rx": []}
                    for kind in ("tx", "rx"):
                        for e in win[kind]:
                            key = (kind, e["tx"], e.get("rx"),
                                   round(e["t"], 4), e["type"])
                            if key in sent_v2x:
                                continue
                            sent_v2x.add(key)
                            msgs[kind].append(e)
                    if len(sent_v2x) > 20000:      # poda: solo claves recientes
                        sent_v2x = {k for k in sent_v2x if k[3] > t - 12.0}
                    if msgs["tx"] or msgs["rx"]:
                        frame["messages"] = msgs
            await ws.send_text(encode(frame))

            if bridge.min_expected_number() <= 0:
                await ws.send_text(encode({"type": "end", "t": round(t, 1)}))
                break
            await asyncio.sleep(max(period - (loop.time() - t0), 0.0))
    except WebSocketDisconnect:
        pass
    finally:
        bridge.close()
