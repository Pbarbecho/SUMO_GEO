"""Real-time traffic estimation from the live SUMO connection.

Per-edge state is derived every step from TraCI/libsumo edge measures:
vehicle count, occupancy and mean speed. Density (veh/km/lane) is mapped to a
simplified HCM-style Level of Service (A-F) with a colour ramp for the map.
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


def level_of_service(density: float) -> tuple[str, str]:
    for upper, label, color in LOS_BINS:
        if density < upper:
            return label, color
    return "F", "#d73027"


def edge_estimation(conn, netgeo, active_edges=None) -> list[dict]:
    """Return per-edge congestion state for edges that currently hold vehicles.

    ``active_edges`` (the edge ids from the current vehicle list) restricts the
    TraCI queries to O(vehicles) instead of O(edges) — on a metropolitan net
    (34k+ edges) polling every edge each step dominates the frame time.
    Empty edges are omitted to keep the WebSocket payload small; the frontend
    renders them at free-flow by default.
    """
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
