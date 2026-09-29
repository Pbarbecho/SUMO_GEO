"""Construcción y codificación de los frames del WebSocket (protocolo v2).

Por qué un protocolo compacto
-----------------------------
Con el formato original cada vehículo viajaba como un dict con 9 claves y
floats de 15 dígitos: ~200 bytes por vehículo y frame. A 10 fps y 1 500
vehículos son ~3 MB/s por visor, y en el navegador JSON.parse + creación de
1 500 objetos por frame. Ahora:

* ``v``    filas planas ``[id, lon, lat, angle, speed]`` (5 valores, lon/lat
           con 6 decimales) para TODOS los vehículos del frame;
* ``vnew`` ``{id: [type, len, wid, station]}`` SOLO para los vehículos que
           aparecen por primera vez (atributos estáticos: viajan una vez);
* ``snapshot: true`` cuando ``vnew`` contiene toda la flota (primer frame que
           recibe un visor recién conectado);
* ``edges`` filas ``[id, n, occ, speed, density, los]`` y solo cada
           ``los_every`` frames (el visor las recolorea a ~1.4 Hz de todas formas);
* ``tls``  solo cuando cambia algún estado.

Resultado medido: 119 KB -> ~24 KB por frame con 590 vehículos (5×), y
281 KB -> ~57 KB con 1 540. La serialización usa orjson (10× json).
"""
from __future__ import annotations

from collections import Counter

try:
    import orjson

    def encode(msg: dict) -> str:
        return orjson.dumps(msg).decode()
except ImportError:                          # pragma: no cover - fallback
    import json

    def encode(msg: dict) -> str:
        return json.dumps(msg, separators=(",", ":"))

from .sumo_bridge import station_of
from .traffic import EdgeIndex, edge_estimation_from_fleet


class FrameBuilder:
    """Estado incremental de una corrida (qué vehículos conoce ya el visor)."""

    def __init__(self, netgeo, los_every: int = 7):
        self.edge_index = EdgeIndex(netgeo)
        self.attrs: dict[str, list] = {}         # id -> [type, len, wid, station]
        self.los_every = max(int(los_every), 1)
        self._n = 0
        self.last_edges: list = []
        self.last_tls: dict = {}

    def reset(self) -> None:
        self.attrs = {}
        self._n = 0
        self.last_edges = []
        self.last_tls = {}

    def build(self, t: float, vehs: list[dict], stats: dict,
              edge_agg: dict | None, tls_changed: dict | None,
              with_station: bool) -> dict:
        """Frame v2 a partir de la lista de dicts del puente (o del replay)."""
        rows: list = []
        vnew: dict = {}
        seen = set()
        for v in vehs:
            vid = v["id"]
            seen.add(vid)
            rows.extend((vid, v["lon"], v["lat"], v["angle"], v["speed"]))
            if vid not in self.attrs:
                st = v.get("station")
                if st is None and with_station:
                    st = station_of(vid)
                self.attrs[vid] = [v.get("type", ""), v.get("len", 4.5),
                                   v.get("wid", 1.8), st]
                vnew[vid] = self.attrs[vid]
        gone = [vid for vid in self.attrs if vid not in seen]
        for vid in gone:
            del self.attrs[vid]
        stats = dict(stats)
        stats["types"] = dict(Counter(a[0] for a in self.attrs.values()))
        frame = {"type": "frame", "t": round(t, 2), "n": len(vehs), "v": rows,
                 "stats": stats}
        if vnew:
            frame["vnew"] = vnew
        if gone:
            frame["vgone"] = gone
        if edge_agg is not None and self._n % self.los_every == 0:
            self.last_edges = edge_estimation_from_fleet(self.edge_index, edge_agg,
                                                         compact=True)
            frame["edges"] = self.last_edges
        if tls_changed is not None:
            self.last_tls = tls_changed
            frame["tls"] = tls_changed
        self._n += 1
        return frame

    def snapshot(self, frame: dict) -> dict:
        """Copia del último frame con TODO lo que un visor recién llegado
        necesita: atributos de toda la flota, último LOS y todos los semáforos."""
        snap = dict(frame)
        snap["vnew"] = dict(self.attrs)
        snap["snapshot"] = True
        snap.pop("vgone", None)
        if self.last_edges:
            snap["edges"] = self.last_edges
        if self.last_tls:
            snap["tls"] = self.last_tls
        return snap
