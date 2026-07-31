"""Thin wrapper around a SUMO simulation via TraCI (or libsumo).

Modes (see :mod:`app.config`):

* ``managed`` - launch SUMO as a subprocess with :func:`traci.start`.
* ``remote``  - connect to a running SUMO TraCI server with :func:`traci.init`.

libsumo (in-process, faster, no socket) is used automatically when
``use_libsumo`` is set and the module is importable; it exposes the same API.
"""
from __future__ import annotations

import itertools

from .config import settings

_CONN_COUNTER = itertools.count()   # unique traci connection labels


class SumoBridge:
    def __init__(self):
        self.conn = None          # the (labeled) traci connection or libsumo module
        self.label = None
        self.running = False
        self._libsumo = False

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

    def step(self) -> float:
        self.conn.simulationStep()
        return self.conn.simulation.getTime()

    def vehicles(self, netgeo) -> list[dict]:
        conn = self.conn
        out = []
        for vid in conn.vehicle.getIDList():
            x, y = conn.vehicle.getPosition(vid)
            lon, lat = netgeo.xy_to_lonlat(x, y)
            out.append({
                "id": vid,
                "lon": lon,
                "lat": lat,
                "angle": round(conn.vehicle.getAngle(vid), 1),
                "speed": round(conn.vehicle.getSpeed(vid), 2),
                "type": conn.vehicle.getTypeID(vid),
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
