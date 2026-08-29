"""Geometry helpers: SUMO network / polygons -> GeoJSON in WGS84.

Two coordinate paths are supported:

* Networks with a real projection (OSM imports) are converted with SUMO's own
  ``net.convertXY2LonLat`` so vehicles land on their true coordinates.
* Synthetic networks (e.g. ``netgenerate`` grids) have no projection, so local
  (x, y) metres are anchored to a WGS84 origin using an ENU (equirectangular)
  approximation. This is accurate to well under a metre over a city-block scale
  demo and keeps the whole thing dependency-free.
"""
from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Optional

import sumolib

from .config import settings

_R = 6378137.0  # WGS84 mean Earth radius (m)


def enu_to_lonlat(x: float, y: float,
                  lon0: Optional[float] = None,
                  lat0: Optional[float] = None) -> list[float]:
    """Local ENU metres -> [lon, lat] anchored at (lon0, lat0)."""
    lon0 = settings.origin_lon if lon0 is None else lon0
    lat0 = settings.origin_lat if lat0 is None else lat0
    lat = lat0 + math.degrees(y / _R)
    lon = lon0 + math.degrees(x / (_R * math.cos(math.radians(lat0))))
    return [lon, lat]


def cfg_paths(cfg_path: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve the net-file and first additional-file from a .sumocfg."""
    base = os.path.dirname(os.path.abspath(cfg_path))
    root = ET.parse(cfg_path).getroot()

    def value(tag: str) -> Optional[str]:
        el = root.find(f".//{tag}")
        return el.get("value") if el is not None else None

    net = value("net-file")
    add = value("additional-files")
    net_path = os.path.join(base, net) if net else None
    poly_path = os.path.join(base, add.split(",")[0].strip()) if add else None
    return net_path, poly_path


class NetworkGeo:
    """Loads a SUMO network once and exposes GeoJSON + coordinate helpers."""

    def __init__(self, net_path: str):
        self.net = sumolib.net.readNet(net_path)
        # No usar net.hasGeoProj(): no existe en sumolib <= 1.12 (backend
        # van3twin) y el except lo convertía en False aunque la red tuviera
        # proyección real (síntoma: red de Turín dibujada sobre Cuenca).
        # Probar la conversión directamente cubre todas las versiones.
        try:
            self.net.convertXY2LonLat(0.0, 0.0)
            self.has_geo = True
        except Exception:
            self.has_geo = False

    # --- coordinate conversion ------------------------------------------
    def xy_to_lonlat(self, x: float, y: float) -> list[float]:
        if self.has_geo:
            lon, lat = self.net.convertXY2LonLat(x, y)
            return [lon, lat]
        return enu_to_lonlat(x, y)

    # --- GeoJSON --------------------------------------------------------
    def edges_geojson(self) -> dict:
        """Road edges as LineString features (drops internal junction edges)."""
        features = []
        for edge in self.net.getEdges():
            if edge.isSpecial():
                continue
            coords = [self.xy_to_lonlat(x, y) for x, y in edge.getShape()]
            features.append({
                "type": "Feature",
                "geometry": {"type": "LineString", "coordinates": coords},
                "properties": {
                    "id": edge.getID(),
                    "lanes": edge.getLaneNumber(),
                    "speed": round(edge.getSpeed(), 2),
                    "length": round(edge.getLength(), 1),
                },
            })
        return {"type": "FeatureCollection", "features": features}

    def bounds_center(self) -> dict:
        xmin, ymin, xmax, ymax = self.net.getBoundary()
        cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
        sw = self.xy_to_lonlat(xmin, ymin)
        ne = self.xy_to_lonlat(xmax, ymax)
        return {
            "center": self.xy_to_lonlat(cx, cy),
            "bounds": [sw, ne],  # [[west, south], [east, north]]
            "has_geo": self.has_geo,
        }


def buildings_geojson(poly_path: Optional[str], netgeo: NetworkGeo) -> dict:
    """Parse a SUMO polygon file into extrudable building polygons.

    Reads the ``height`` param when present (falls back to a default), which the
    frontend uses for ``fill-extrusion-height``.
    """
    features: list[dict] = []
    if not poly_path or not os.path.exists(poly_path):
        return {"type": "FeatureCollection", "features": features}

    root = ET.parse(poly_path).getroot()
    for poly in root.iter("poly"):
        ptype = poly.get("type", "") or ""
        # accept "building" and richer subtypes like "building.commercial"
        if ptype and not ptype.startswith("building"):
            continue
        shape = poly.get("shape", "")
        ring: list[list[float]] = []
        for pair in shape.split():
            parts = pair.split(",")
            if len(parts) < 2:
                continue
            ring.append(netgeo.xy_to_lonlat(float(parts[0]), float(parts[1])))
        if len(ring) < 3:
            continue
        if ring[0] != ring[-1]:
            ring.append(ring[0])

        height = 12.0
        for prm in poly.findall("param"):
            if prm.get("key") == "height":
                try:
                    height = float(prm.get("value"))
                except (TypeError, ValueError):
                    pass
        features.append({
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [ring]},
            "properties": {"id": poly.get("id"), "height": height},
        })
    return {"type": "FeatureCollection", "features": features}


def building_vertices_local(poly_path: Optional[str]) -> list:
    """All building-polygon vertices in SUMO local (x, y) metres."""
    verts: list = []
    if not poly_path or not os.path.exists(poly_path):
        return verts
    root = ET.parse(poly_path).getroot()
    for poly in root.iter("poly"):
        ptype = poly.get("type", "") or ""
        if ptype and not ptype.startswith("building"):
            continue
        for pair in poly.get("shape", "").split():
            parts = pair.split(",")
            if len(parts) >= 2:
                verts.append((float(parts[0]), float(parts[1])))
    return verts


def trafficlights_geojson(netgeo: NetworkGeo, building_verts: Optional[list] = None,
                          clearance: float = 9.0) -> dict:
    """One signal per traffic light and incoming edge (approach), at the stop
    line. Each feature carries the tls id, a representative link index (for the
    live colour), the approach bearing, and ``mast``: True where the junction is
    open (no building within ``clearance`` m) so the frontend draws a mast-arm;
    False elsewhere, where it draws a straight roadside pole.
    """
    c2 = clearance * clearance
    features: list[dict] = []
    for tls in netgeo.net.getTrafficLights():
        tid = tls.getID()
        by_edge: dict = defaultdict(list)          # edgeID -> [(inLane, linkIndex), ...]
        for conn in tls.getConnections():
            in_lane, _out_lane, link_index = conn[0], conn[1], conn[2]
            by_edge[in_lane.getEdge().getID()].append((in_lane, link_index))

        for eid, items in by_edge.items():
            ends = [ln.getShape()[-1] for ln, _ in items if len(ln.getShape()) >= 2]
            if not ends:
                continue
            cx = sum(p[0] for p in ends) / len(ends)   # centre of the approach stop line
            cy = sum(p[1] for p in ends) / len(ends)
            rep_lane = sorted(items, key=lambda it: it[0].getID())[len(items) // 2][0]
            rs = rep_lane.getShape()
            (x1, y1), (x0, y0) = rs[-1], rs[-2]
            length = math.hypot(x1 - x0, y1 - y0) or 1.0
            ux, uy = (x1 - x0) / length, (y1 - y0) / length
            bearing = (math.degrees(math.atan2(ux, uy))) % 360
            rep_index = sorted(i for _l, i in items)[len(items) // 2]
            mast = True
            if building_verts:
                for vx, vy in building_verts:
                    if (cx - vx) ** 2 + (cy - vy) ** 2 < c2:
                        mast = False       # a building is close -> straight pole
                        break
            lon, lat = netgeo.xy_to_lonlat(cx - ux * 2.5, cy - uy * 2.5)
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": {"id": f"{tid}.{eid}", "tls": tid, "index": rep_index,
                               "angle": round(bearing, 1), "mast": mast},
            })
    return {"type": "FeatureCollection", "features": features}
