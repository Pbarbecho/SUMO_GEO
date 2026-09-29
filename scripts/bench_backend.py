#!/usr/bin/env python3
"""Banco de medida del backend: coste por frame contra un SUMO real.

Lanza SUMO (modo managed) con el escenario indicado, avanza hasta que haya
al menos --min-veh vehículos y mide, durante --frames frames, cuánto tarda
cada parte del frame que el backend manda por WebSocket:

  step        simulationStep + bookkeeping (sumo_bridge.step)
  vehicles    lectura de la flota (suscripciones) + proyección a lon/lat
  edges       estimación LOS (desde la flota: 0 llamadas TraCI)
  tls         estado de semáforos (suscripción)
  build+enc   FrameBuilder.build (protocolo v2) + orjson, y bytes por frame

Con --legacy mide además el camino antiguo (edge_estimation con 3 llamadas
TraCI por arista, getRedYellowGreenState por semáforo, json.dumps del frame
v1) para comparar.

Uso (desde la raíz del repo, con eclipse-sumo instalado):
  APP_SUMO_CONFIG=sumo/cuenca_high.sumocfg python3 scripts/bench_backend.py \
      --begin 28800 --min-veh 150 --frames 60 [--legacy]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.config import settings                      # noqa: E402
from app.frames import FrameBuilder, encode          # noqa: E402
from app.geo import NetworkGeo, cfg_paths            # noqa: E402
from app.sumo_bridge import SumoBridge               # noqa: E402
from app.traffic import edge_estimation              # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--begin", type=float, default=None)
    ap.add_argument("--min-veh", type=int, default=300)
    ap.add_argument("--frames", type=int, default=60)
    ap.add_argument("--legacy", action="store_true", help="medir también el camino v1")
    ap.add_argument("--json", default="", help="fichero de salida con las cifras")
    args = ap.parse_args()

    net_file, _ = cfg_paths(settings.sumo_config)
    netgeo = NetworkGeo(settings.net_file or net_file)
    bridge = SumoBridge()
    bridge.start(begin=args.begin)

    # calentar hasta tener flota suficiente
    t0 = time.monotonic()
    while bridge.conn.vehicle.getIDCount() < args.min_veh:
        bridge.step()
        if bridge.min_expected_number() <= 0 or time.monotonic() - t0 > 600:
            print("no se alcanzó la flota mínima", file=sys.stderr)
            break
    print(f"flota: {bridge.conn.vehicle.getIDCount()} vehículos, "
          f"t={bridge.conn.simulation.getTime():.0f}s")

    builder = FrameBuilder(netgeo, settings.los_every)
    parts = {k: [] for k in ("step", "vehicles", "edges", "tls", "build_enc",
                             "legacy_edges", "legacy_tls", "legacy_json")}
    sizes, sizes_legacy, nveh = [], [], []
    for _ in range(args.frames):
        a = time.perf_counter()
        t = bridge.step()
        b = time.perf_counter()
        vehs = bridge.vehicles(netgeo)
        c = time.perf_counter()
        tls = bridge.trafficlights_if_changed()
        d = time.perf_counter()
        frame = builder.build(t, vehs, bridge.frame_stats(), bridge.edge_agg, tls,
                              with_station=False)
        raw = encode(frame)
        e = time.perf_counter()
        parts["step"].append(b - a); parts["vehicles"].append(c - b)
        parts["tls"].append(d - c); parts["build_enc"].append(e - d)
        parts["edges"].append(0.0)          # incluido en build_enc (sin TraCI)
        sizes.append(len(raw)); nveh.append(len(vehs))
        if args.legacy:
            f = time.perf_counter()
            edges = edge_estimation(bridge.conn, netgeo, (v["edge"] for v in vehs))
            g = time.perf_counter()
            tls_all = {tid: bridge.conn.trafficlight.getRedYellowGreenState(tid)
                       for tid in bridge.conn.trafficlight.getIDList()}
            h = time.perf_counter()
            raw1 = json.dumps({"type": "frame", "t": round(t, 1), "vehicles": vehs,
                               "edges": edges, "tls": tls_all,
                               "stats": bridge.frame_stats()})
            i = time.perf_counter()
            parts["legacy_edges"].append(g - f); parts["legacy_tls"].append(h - g)
            parts["legacy_json"].append(i - h); sizes_legacy.append(len(raw1))
    bridge.close()

    out = {"frames": args.frames, "veh_mean": round(statistics.mean(nveh)),
           "bytes_v2_mean": round(statistics.mean(sizes))}
    total = 0.0
    for k in ("step", "vehicles", "tls", "build_enc"):
        ms = statistics.mean(parts[k]) * 1000
        out[k + "_ms"] = round(ms, 2)
        total += ms
    out["frame_v2_ms"] = round(total, 2)
    if args.legacy:
        base = statistics.mean(parts["step"]) + statistics.mean(parts["vehicles"])
        leg = sum(statistics.mean(parts[k]) for k in ("legacy_edges", "legacy_tls",
                                                       "legacy_json"))
        out["bytes_v1_mean"] = round(statistics.mean(sizes_legacy))
        out["legacy_edges_ms"] = round(statistics.mean(parts["legacy_edges"]) * 1000, 2)
        out["legacy_tls_ms"] = round(statistics.mean(parts["legacy_tls"]) * 1000, 2)
        out["legacy_json_ms"] = round(statistics.mean(parts["legacy_json"]) * 1000, 2)
        out["frame_v1_ms"] = round((base + leg) * 1000, 2)
    print(json.dumps(out, indent=1))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
