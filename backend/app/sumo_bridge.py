"""Thin wrapper around a SUMO simulation via TraCI (or libsumo).

Modes (see :mod:`app.config`):

* ``managed`` - launch SUMO as a subprocess with :func:`traci.start`.
* ``remote``  - connect to a running SUMO TraCI server with :func:`traci.init`.

libsumo (in-process, faster, no socket) is used automatically when
``use_libsumo`` is set and the module is importable; it exposes the same API.
"""
from __future__ import annotations

import itertools

import traci.constants as tc

from .config import settings

_CONN_COUNTER = itertools.count()   # unique traci connection labels

# Per-vehicle variables streamed via TraCI subscriptions: the whole fleet arrives
# in ONE round trip per step instead of ~6 socket calls per vehicle. This is what
# makes thousands of concurrent vehicles feasible.
_SUB_VARS = (tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_TYPE,
             tc.VAR_ROAD_ID, tc.VAR_CO2EMISSION, tc.VAR_WAITING_TIME)


class SumoBridge:
    def __init__(self):
        self.conn = None          # the (labeled) traci connection or libsumo module
        self.label = None
        self.running = False
        self._libsumo = False
        self._dims: dict = {}     # typeID -> (length, width) metres, cached
        # aggregates for the historical panel
        self._depart_t: dict = {}                 # vid -> departure sim-time
        from collections import deque
        self._tt = deque(maxlen=300)              # travel times of recent arrivals (s)
        self._arrived_total = 0
        self.last_co2 = 0.0                       # fleet total, mg/s (from subscriptions)
        self.last_wait_mean = 0.0                 # mean waiting of stopped vehicles (s)
        self.last_wait_n = 0                      # how many vehicles are waiting

    def _import_client(self):
        if settings.use_libsumo:
            try:
                import libsumo  # type: ignore
                self._libsumo = True
                return libsumo
            except ImportError:
                pass
        import traci
        return traci

    def start(self, config: str | None = None, begin: float | None = None) -> None:
        client = self._import_client()
        if settings.sumo_mode == "remote" and not self._libsumo:
            # Connect to an already-running SUMO server (your existing container).
            # Con sumo_port_scan > 0 se prueban también los puertos siguientes:
            # el TraCI de ns-3 (GetFreePort) corre el puerto si el base está
            # ocupado o en TIME_WAIT tras la corrida anterior.
            last_exc: Exception | None = None
            for port in range(settings.sumo_port,
                              settings.sumo_port + max(settings.sumo_port_scan, 0) + 1):
                try:
                    client.init(host=settings.sumo_host, port=port, numRetries=1)
                    if port != settings.sumo_port:
                        print(f"[sumo_bridge] SUMO respondió en {port} "
                              f"(base {settings.sumo_port})", flush=True)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 - probar el siguiente puerto
                    last_exc = exc
            if last_exc is not None:
                raise last_exc
            if settings.sumo_order:
                # SUMO multi-cliente (--num-clients N): cada cliente debe declarar
                # su orden antes del primer simulationStep (VaN3Twin es el 1).
                client.setOrder(settings.sumo_order)
            self.conn = client
        else:
            from sumolib import checkBinary
            binary = checkBinary(settings.sumo_binary)
            args = [
                binary,
                "-c", config or settings.sumo_config,
                "--step-length", str(settings.step_length),
                "--start", "--quit-on-end",
            ]
            if begin is not None:
                args += ["--begin", str(begin)]
            if self._libsumo:
                client.start(args)
                self.conn = client
            else:
                # unique label so reconnecting WebSockets don't clash on traci's
                # single global 'default' connection
                self.label = f"ws{next(_CONN_COUNTER)}"
                client.start(args, label=self.label)
                self.conn = client.getConnection(self.label)
        self.running = True
        try:                                    # vehicles already in the run (mid-day start)
            for vid in self.conn.vehicle.getIDList():
                self.conn.vehicle.subscribe(vid, _SUB_VARS)
        except Exception:
            pass

    def step(self) -> float:
        if settings.sumo_mode == "remote" and settings.sumo_order:
            # Multi-cliente con ns-3 (VaN3Twin): pedir un objetivo ABSOLUTO grueso
            # (t_actual + step_length). ns-3 avanza con sus pasos finos y marca el
            # ritmo; si pidiéramos "un paso" por frame, ns-3 quedaría esclavo del
            # visor (validado empíricamente — ver GUIA_INTEGRACION_SUMO_GEO.md).
            self.conn.simulationStep(
                self.conn.simulation.getTime() + settings.step_length)
        else:
            self.conn.simulationStep()
        now = self.conn.simulation.getTime()
        try:                                    # keep the subscription set complete
            for vid in self.conn.simulation.getDepartedIDList():
                self.conn.vehicle.subscribe(vid, _SUB_VARS)
                self._depart_t[vid] = now       # for travel-time stats
            for vid in self.conn.simulation.getArrivedIDList():
                t0 = self._depart_t.pop(vid, None)
                if t0 is not None:
                    self._tt.append(now - t0)
                    self._arrived_total += 1
        except Exception:
            pass
        return now

    def _type_dims(self, type_id: str) -> tuple[float, float]:
        """(length, width) in metres for a vType, queried once and cached so the
        frontend can draw each vehicle at its true footprint (car vs. bus)."""
        d = self._dims.get(type_id)
        if d is None:
            try:
                d = (round(self.conn.vehicletype.getLength(type_id), 2),
                     round(self.conn.vehicletype.getWidth(type_id), 2))
            except Exception:
                d = (4.5, 1.8)          # sensible passenger-car fallback
            self._dims[type_id] = d
        return d

    def vehicles(self, netgeo) -> list[dict]:
        conn = self.conn
        res = {}
        try:
            res = conn.vehicle.getAllSubscriptionResults()   # whole fleet, 1 round trip
        except Exception:
            res = {}
        if not res and conn.vehicle.getIDCount() > 0:
            return self._vehicles_polled(netgeo)             # safety fallback
        out = []
        co2_total = 0.0
        wait_sum, wait_n = 0.0, 0
        for vid, r in res.items():
            x, y = r[tc.VAR_POSITION]
            lon, lat = netgeo.xy_to_lonlat(x, y)
            vtype = r[tc.VAR_TYPE]
            length, width = self._type_dims(vtype)
            co2_total += r.get(tc.VAR_CO2EMISSION, 0.0)
            w = r.get(tc.VAR_WAITING_TIME, 0.0)
            if w > 0:                            # stopped (mostly at signals/queues)
                wait_sum += w
                wait_n += 1
            out.append({
                "id": vid,
                "lon": lon,
                "lat": lat,
                "angle": round(r[tc.VAR_ANGLE], 1),
                "speed": round(r[tc.VAR_SPEED], 2),
                "type": vtype,
                "len": length,
                "wid": width,
                "edge": r[tc.VAR_ROAD_ID],
            })
        self.last_co2 = co2_total
        self.last_wait_mean = (wait_sum / wait_n) if wait_n else 0.0
        self.last_wait_n = wait_n
        return out

    def frame_stats(self) -> dict:
        """Aggregates for the historical panel (computed during vehicles())."""
        tt_mean = (sum(self._tt) / len(self._tt)) if self._tt else None
        return {
            "co2": round(self.last_co2 / 1000.0, 2),        # g/s fleet total
            "wait": round(self.last_wait_mean, 1),          # s, mean of waiting vehicles
            "wait_n": self.last_wait_n,                     # vehicles currently waiting
            "tt": round(tt_mean, 1) if tt_mean is not None else None,   # s, recent arrivals
            "arrived": self._arrived_total,
        }

    def _vehicles_polled(self, netgeo) -> list[dict]:
        """Old per-vehicle polling path (used only if subscriptions are empty)."""
        conn = self.conn
        out = []
        for vid in conn.vehicle.getIDList():
            x, y = conn.vehicle.getPosition(vid)
            lon, lat = netgeo.xy_to_lonlat(x, y)
            vtype = conn.vehicle.getTypeID(vid)
            length, width = self._type_dims(vtype)
            out.append({
                "id": vid, "lon": lon, "lat": lat,
                "angle": round(conn.vehicle.getAngle(vid), 1),
                "speed": round(conn.vehicle.getSpeed(vid), 2),
                "type": vtype, "len": length, "wid": width,
                "edge": conn.vehicle.getRoadID(vid),
            })
        return out

    def vehicle_details(self, vid: str) -> dict:
        """Extended per-vehicle stats for the right-click inspector. Each query
        is guarded — not every measure exists in every SUMO build/vehicle."""
        v = self.conn.vehicle
        out: dict = {"id": vid}
        probes = {
            "co2": lambda: v.getCO2Emission(vid),              # mg/s
            "fuel": lambda: v.getFuelConsumption(vid),         # mg/s (ml/s in old SUMO)
            "noise": lambda: v.getNoiseEmission(vid),          # dB(A)
            "waiting": lambda: v.getWaitingTime(vid),          # s stopped (current)
            "waiting_acc": lambda: v.getAccumulatedWaitingTime(vid),
            "timeloss": lambda: v.getTimeLoss(vid),            # s lost vs. free flow
            "distance": lambda: v.getDistance(vid),            # m driven since depart
            "lane": lambda: v.getLaneID(vid),
            "route_index": lambda: v.getRouteIndex(vid),
            "route_edges": lambda: len(v.getRoute(vid)),
        }
        for key, fn in probes.items():
            try:
                out[key] = fn()
            except Exception:
                pass
        out["gone"] = len(out) <= 2   # only id (+gone) -> vehicle left the run
        return out

    def trafficlights(self) -> dict:
        """Current signal-state string per traffic light (SUMO r/y/g/G/u/o codes)."""
        conn = self.conn
        return {tid: conn.trafficlight.getRedYellowGreenState(tid)
                for tid in conn.trafficlight.getIDList()}

    def min_expected_number(self) -> int:
        """0 when no vehicles remain and none are scheduled -> simulation done."""
        return self.conn.simulation.getMinExpectedNumber()

    def close(self) -> None:
        if self.conn and self.running:
            try:
                self.conn.close()
            except Exception:
                pass
        self.running = False
