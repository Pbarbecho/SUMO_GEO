"""Thin wrapper around a SUMO simulation via TraCI (or libsumo).

Modes (see :mod:`app.config`):

* ``managed`` - launch SUMO as a subprocess with :func:`traci.start`.
* ``remote``  - connect to a running SUMO TraCI server with :func:`traci.init`.

libsumo (in-process, faster, no socket) is used automatically when
``use_libsumo`` is set and the module is importable; it exposes the same API.
"""
from __future__ import annotations

from .config import settings


class SumoBridge:
    def __init__(self):
        self.conn = None          # traci or libsumo module (same interface)
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

    def start(self) -> None:
        client = self._import_client()
        if settings.sumo_mode == "remote" and not self._libsumo:
            # Connect to an already-running SUMO server (your existing container).
            client.init(host=settings.sumo_host, port=settings.sumo_port)
        else:
            from sumolib import checkBinary
            binary = checkBinary(settings.sumo_binary)
            client.start([
                binary,
                "-c", settings.sumo_config,
                "--step-length", str(settings.step_length),
                "--start", "--quit-on-end",
            ])
        self.conn = client
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
