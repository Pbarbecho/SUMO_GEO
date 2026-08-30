"""FastAPI application: static geometry over REST, live state over WebSocket.

Endpoints
---------
GET  /api/health      -> liveness + active SUMO mode
GET  /api/meta        -> map center, bounds, origin, step length
GET  /api/network     -> road edges as GeoJSON (cached)
GET  /api/buildings   -> building polygons as GeoJSON with height (cached)
WS   /ws/live         -> per-step frames: vehicles + per-edge congestion

Each WebSocket connection drives its own SUMO run, so several viewers can watch
independent simulations. Client -> server control messages:
``{"cmd":"pause"}`` / ``{"cmd":"play"}`` / ``{"cmd":"speed","fps":20}``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .geo import (NetworkGeo, buildings_geojson, building_vertices_local,
                  cfg_paths, trafficlights_geojson)
from .sumo_bridge import SumoBridge
from .traffic import edge_estimation

_state: dict = {}


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
    _state["network"] = netgeo.edges_geojson()
    _state["buildings"] = buildings_geojson(poly_file, netgeo)
    _state["trafficlights"] = trafficlights_geojson(
        netgeo, building_vertices_local(poly_file))
    meta = {
        **netgeo.bounds_center(),
        "origin": [settings.origin_lon, settings.origin_lat],
        "step_length": settings.step_length,
        "mode": settings.sumo_mode,
    }
    if settings.view_lon is not None and settings.view_lat is not None:
        meta["center"] = [settings.view_lon, settings.view_lat]   # open on the demand area
    _state["meta"] = meta
    yield
    _state.clear()


app = FastAPI(title="SUMO-GEO API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.cors_origins == "*"
    else [o.strip() for o in settings.cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": settings.sumo_mode}


@app.get("/api/meta")
async def meta():
    return _state["meta"]


@app.get("/api/network")
async def network():
    return JSONResponse(_state["network"])


@app.get("/api/buildings")
async def buildings():
    return JSONResponse(_state["buildings"])


@app.get("/api/trafficlights")
async def trafficlights():
    return JSONResponse(_state["trafficlights"])


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
    parsea un índice nuevo (modo live=ligero). Si toca, calcula también las
    stats PHY AQUÍ (nunca en la petición del panel: su coste O(eventos) cada
    10 s era una de las pausas periódicas del visor). Devuelve
    (rep, fp, dur_s, phy|None) o None si no hay nada nuevo."""
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
        rep = ReplayData(settings.live_pcap_dir, settings.asn_dir,
                         "v2v-*.pcap", live=True)
        phy = rep.phy_stats() if want_phy else None
        return rep, fp, _time.monotonic() - t0, phy
    except Exception:  # noqa: BLE001
        return None


async def _live_refresh_bg():
    """Tarea de fondo: reconstruye el índice y lo intercambia al terminar.
    El throttle se adapta al coste real del parse (crece con el pcap) para que
    el refresco nunca domine la CPU en corridas largas."""
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
    si toca refrescar, lanza la reconstrucción como tarea de fondo. (La versión
    anterior hacía `await` del re-parse en el bucle de frames: cada ~2 s el
    stream se congelaba el tiempo del parse — las pausas periódicas del visor.)"""
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
        await ws.send_json({"type": "error", "message": f"replay: {exc}"})
        await ws.close()
        return

    t = rep.t0
    paused = False
    single = False                     # modo paso a paso: un frame y re-pausa
    period = 1.0 / max(settings.max_fps, 0.1)
    step = settings.step_length
    await ws.send_json({"type": "meta", **_state["meta"], "mode": "replay",
                        "t0": round(rep.t0, 2), "t1": round(rep.t1, 2),
                        "replay": rep.stats})
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
                        await ws.send_json({"type": "msg_detail", **det})
                    else:                       # vehículo: cinemática del replay
                        vid = str(cmd.get("id", ""))
                        v = next((x for x in rep.positions(t) if x["id"] == vid), None)
                        await ws.send_json({"type": "inspect",
                                            **({"id": vid, "gone": True} if v is None
                                               else {"id": vid, "lane": "",
                                                     "distance": None})})
            if paused:
                await asyncio.sleep(0.05)
                continue

            t_next = min(t + step, rep.t1)
            vehs = rep.positions(t_next)
            win = rep.window(t, t_next)
            await ws.send_json({
                "type": "frame",
                "t": round(t_next, 2),
                "vehicles": vehs,
                "edges": [],
                "tls": {},
                "messages": win,
                "stats": {"co2": 0, "wait": 0, "wait_n": 0, "tt": None,
                          "arrived": 0,
                          "types": dict(Counter(v["type"] for v in vehs))},
            })
            if single:
                paused, single = True, False     # paso dado: re-pausar
            if t_next >= rep.t1:
                await ws.send_json({"type": "end", "t": round(t_next, 2)})
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
        bridge.start(config=cfg, begin=begin)
    except Exception as exc:  # noqa: BLE001 - surface the reason to the client + logs
        import traceback
        traceback.print_exc()  # real cause visible in `docker compose logs backend`
        for attempt in (
            lambda: ws.send_json({"type": "error", "message": f"SUMO start failed: {exc}"}),
            lambda: ws.close(),
        ):
            try:
                await attempt()
            except Exception:
                pass
        return

    paused = False
    period = 1.0 / max(settings.max_fps, 0.1)
    sent_v2x: set = set()      # dedup de eventos V2X ya enviados (modo en vivo)
    await ws.send_json({"type": "meta", **_state["meta"]})

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
                        details = bridge.vehicle_details(vid)
                    except Exception:
                        details = {"id": vid, "gone": True}
                    await ws.send_json({"type": "inspect", **details})

            if paused:
                await asyncio.sleep(0.05)
                continue

            t = bridge.step()
            vehs = bridge.vehicles(netgeo)
            stats = bridge.frame_stats()
            stats["types"] = dict(Counter(v["type"] for v in vehs))
            frame = {
                "type": "frame",
                "t": round(t, 1),
                "vehicles": vehs,
                "edges": edge_estimation(bridge.conn, netgeo,
                                         (v["edge"] for v in vehs)),
                "tls": bridge.trafficlights(),
                "stats": stats,
            }
            # --- mensajes V2X en vivo: leer los pcap que ns-3 escribe durante
            # la corrida (montados RO en live_pcap_dir) y adjuntar los eventos
            # aún no enviados. station = nº del id SUMO (== stationID ETSI en
            # el mapeo del replay); el visor ancla pulsos/arcos por station.
            if settings.live_pcap_dir:
                for v in vehs:
                    m = re.search(r"(\d+)", v["id"])
                    if m:
                        v["station"] = int(m.group(1))
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
            await ws.send_json(frame)

            if bridge.min_expected_number() <= 0:
                await ws.send_json({"type": "end", "t": round(t, 1)})
                break
            await asyncio.sleep(period)
    except WebSocketDisconnect:
        pass
    finally:
        bridge.close()
