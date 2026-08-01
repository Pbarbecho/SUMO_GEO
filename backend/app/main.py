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


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    await ws.accept()
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

            if paused:
                await asyncio.sleep(0.05)
                continue

            t = bridge.step()
            vehs = bridge.vehicles(netgeo)
            await ws.send_json({
                "type": "frame",
                "t": round(t, 1),
                "vehicles": vehs,
                "edges": edge_estimation(bridge.conn, netgeo,
                                         (v["edge"] for v in vehs)),
                "tls": bridge.trafficlights(),
            })

            if bridge.min_expected_number() <= 0:
                await ws.send_json({"type": "end", "t": round(t, 1)})
                break
            await asyncio.sleep(period)
    except WebSocketDisconnect:
        pass
    finally:
        bridge.close()
