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
             tc.VAR_ROAD_ID)


class SumoBridge:
    def __init__(self):
        self.conn = None          # the (labeled) traci connection or libsumo module
        self.label = None
        self.running = False
        self._libsumo = False
        self._dims: dict = {}     # typeID -> (length, width) metres, cached

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
            client.init(host=settings.sumo_host, port=settings.sumo_port)
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
        self.conn.simulationStep()
        try:                                    # keep the subscription set complete
            for vid in self.conn.simulation.getDepartedIDList():
                self.conn.vehicle.subscribe(vid, _SUB_VARS)
        except Exception:
            pass
        return self.conn.simulation.getTime()

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
        for vid, r in res.items():
            x, y = r[tc.VAR_POSITION]
            lon, lat = netgeo.xy_to_lonlat(x, y)
            vtype = r[tc.VAR_TYPE]
            length, width = self._type_dims(vtype)
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
        return out

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
