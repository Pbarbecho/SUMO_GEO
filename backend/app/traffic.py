"""Real-time traffic estimation from the live SUMO connection.

Per-edge state is derived every step from the vehicle subscription that the
bridge already receives (count, mean speed and occupancy per edge), so the
estimation costs ZERO extra TraCI round trips. Density (veh/km/lane) is mapped
to a simplified HCM-style Level of Service (A-F) with a colour ramp for the map.
"""
from __future__ import annotations

# (upper density bound veh/km/lane, LOS label, colour)
LOS_BINS = [
    (8.0,  "A", "#1a9850"),
    (16.0, "B", "#66bd63"),
    (24.0, "C", "#d9ef8b"),
    (32.0, "D", "#fee08b"),
    (40.0, "E", "#fc8d59"),
    (float("inf"), "F", "#d73027"),
]
LOS_COLORS = {label: color for _u, label, color in LOS_BINS}


def level_of_service(density: float) -> tuple[str, str]:
    for upper, label, color in LOS_BINS:
        if density < upper:
            return label, color
    return "F", "#d73027"


class EdgeIndex:
    """(length_km, lanes) por arista, resuelto una vez: ``net.getEdge`` en
    sumolib es un dict lookup + varias llamadas; con cientos de aristas
    activas por frame merece la caché."""

    def __init__(self, netgeo):
        self._net = netgeo.net
        self._cache: dict = {}

    def get(self, eid: str):
        v = self._cache.get(eid)
        if v is None:
            try:
                e = self._net.getEdge(eid)
                v = (max(e.getLength() / 1000.0, 1e-6), e.getLaneNumber() or 1,
                     max(e.getLength(), 1e-3))
            except Exception:
                v = False
            self._cache[eid] = v
        return v or None


def edge_estimation_from_fleet(edge_index: EdgeIndex, agg: dict,
                               compact: bool = False) -> list:
    """Per-edge congestion from the fleet aggregation that
    :meth:`SumoBridge.vehicles` builds (edge -> [n, speed_sum, len_sum]).

    * ``density``   veh/km/lane
    * ``speed``     mean speed of the vehicles on the edge (m/s)
    * ``occ``       fraction of carriageway length covered by vehicles (0-1),
                    equivalente a ``getLastStepOccupancy`` (allí en %)

    ``compact=True`` devuelve filas ``[id, n, occ, speed, density, los]``
    (menos bytes en el WebSocket; el color lo deriva el visor del LOS).
    """
    out: list = []
    for eid, (n, speed_sum, len_sum) in agg.items():
        info = edge_index.get(eid)
        if info is None:
            continue
        length_km, lanes, length_m = info
        density = n / (length_km * lanes)
        speed = speed_sum / n if n else 0.0
        occ = min(len_sum / (length_m * lanes), 1.0)
        los, color = level_of_service(density)
        if compact:
            out.append([eid, n, round(occ, 3), round(speed, 1), round(density, 1), los])
        else:
            out.append({"id": eid, "n": n, "occ": round(occ, 3),
                        "speed": round(speed, 2), "density": round(density, 1),
                        "los": los, "color": color})
    return out


def edge_estimation(conn, netgeo, active_edges=None) -> list[dict]:
    """Versión antigua (consulta TraCI por arista). Se conserva como referencia y
    para el banco de medida; el backend ya no la usa en el camino de frames."""
    if active_edges is not None:
        edges = []
        for eid in set(active_edges):
            if not eid or eid.startswith(":"):     # skip internal junction edges
                continue
            try:
                edges.append(netgeo.net.getEdge(eid))
            except Exception:
                continue
    else:
        edges = [e for e in netgeo.net.getEdges() if not e.isSpecial()]

    out: list[dict] = []
    for edge in edges:
        eid = edge.getID()
        n = conn.edge.getLastStepVehicleNumber(eid)
        if n == 0:
            continue
        occ = conn.edge.getLastStepOccupancy(eid)
        speed = conn.edge.getLastStepMeanSpeed(eid)
        lanes = edge.getLaneNumber() or 1
        length_km = max(edge.getLength() / 1000.0, 1e-6)
        density = n / (length_km * lanes)
        los, color = level_of_service(density)
        out.append({
            "id": eid,
            "n": n,
            "occ": round(occ, 3),
            "speed": round(speed, 2),
            "density": round(density, 1),
            "los": los,
            "color": color,
        })
    return out
