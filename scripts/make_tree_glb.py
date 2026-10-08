#!/usr/bin/env python3
"""Genera modelos glTF binarios (.glb) low-poly de árboles para el visor
(deck.gl ScenegraphLayer), sin dependencias: tronco (cilindro) + copa
(esfera facetada o conos apilados). Convención del visor: Z arriba, suelo z=0,
unidades en metros. Normales planas por cara para el sombreado PBR.

    python3 scripts/make_tree_glb.py frontend/models/
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys


def _cylinder(r: float, z0: float, z1: float, n: int = 8, r_top: float | None = None):
    r_top = r if r_top is None else r_top
    tris = []
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p00 = (r * math.cos(a0), r * math.sin(a0), z0)
        p01 = (r * math.cos(a1), r * math.sin(a1), z0)
        p10 = (r_top * math.cos(a0), r_top * math.sin(a0), z1)
        p11 = (r_top * math.cos(a1), r_top * math.sin(a1), z1)
        tris.append((p00, p01, p11))
        tris.append((p00, p11, p10))
    return tris


def _sphere(cx: float, cy: float, cz: float, r: float, rings: int = 5, segs: int = 8,
            squash: float = 1.0):
    pts = []
    for i in range(rings + 1):
        th = math.pi * i / rings
        row = []
        for j in range(segs):
            ph = 2 * math.pi * j / segs + (0.3 if i % 2 else 0.0)
            row.append((cx + r * math.sin(th) * math.cos(ph),
                        cy + r * math.sin(th) * math.sin(ph),
                        cz + r * squash * math.cos(th)))
        pts.append(row)
    tris = []
    for i in range(rings):
        for j in range(segs):
            a, b = pts[i][j], pts[i][(j + 1) % segs]
            c, d = pts[i + 1][j], pts[i + 1][(j + 1) % segs]
            if i > 0:
                tris.append((a, c, b))
            if i < rings - 1:
                tris.append((b, c, d))
    return tris


def _cone(cz: float, h: float, r: float, n: int = 8):
    tris = []
    apex = (0.0, 0.0, cz + h)
    for i in range(n):
        a0, a1 = 2 * math.pi * i / n, 2 * math.pi * (i + 1) / n
        p0 = (r * math.cos(a0), r * math.sin(a0), cz)
        p1 = (r * math.cos(a1), r * math.sin(a1), cz)
        tris.append((p0, p1, apex))
        tris.append((p1, p0, (0.0, 0.0, cz)))     # base
    return tris


def _flat(tris):
    """triángulos -> (positions, normals) planas, un vértice por esquina."""
    pos, nrm = [], []
    for a, b, c in tris:
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        ln = math.sqrt(sum(x * x for x in n)) or 1.0
        n = (n[0] / ln, n[1] / ln, n[2] / ln)
        for p in (a, b, c):
            pos.extend(p)
            nrm.extend(n)
    return pos, nrm


def write_glb(path: str, parts: list[tuple[list, tuple]]) -> None:
    """parts: [(triángulos, (r,g,b)), ...] -> un primitive por parte."""
    bin_chunks = bytearray()
    buffer_views, accessors, materials, prims = [], [], [], []
    for tris, color in parts:
        pos, nrm = _flat(tris)
        count = len(pos) // 3
        for data, acc_type in ((pos, "POSITION"), (nrm, "NORMAL")):
            raw = struct.pack("<%df" % len(data), *data)
            off = len(bin_chunks)
            bin_chunks.extend(raw)
            while len(bin_chunks) % 4:
                bin_chunks.append(0)
            buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": len(raw), "target": 34962})
            acc = {"bufferView": len(buffer_views) - 1, "componentType": 5126,
                   "count": count, "type": "VEC3"}
            if acc_type == "POSITION":
                xs, ys, zs = data[0::3], data[1::3], data[2::3]
                acc["min"] = [min(xs), min(ys), min(zs)]
                acc["max"] = [max(xs), max(ys), max(zs)]
            accessors.append(acc)
        # índices explícitos (0..n-1): el cargador glTF de deck.gl/luma.gl exige
        # primitivas indexadas (sin ellas falla con "getVertexCount not implemented")
        idx_fmt, idx_ct = ("<%dH" % count, 5123) if count < 65536 else ("<%dI" % count, 5125)
        raw = struct.pack(idx_fmt, *range(count))
        off = len(bin_chunks)
        bin_chunks.extend(raw)
        while len(bin_chunks) % 4:
            bin_chunks.append(0)
        buffer_views.append({"buffer": 0, "byteOffset": off, "byteLength": len(raw),
                             "target": 34963})
        accessors.append({"bufferView": len(buffer_views) - 1, "componentType": idx_ct,
                          "count": count, "type": "SCALAR"})
        materials.append({"pbrMetallicRoughness": {
            "baseColorFactor": [color[0], color[1], color[2], 1.0],
            "metallicFactor": 0.0, "roughnessFactor": 0.95}})
        prims.append({"attributes": {"POSITION": len(accessors) - 3,
                                     "NORMAL": len(accessors) - 2},
                      "indices": len(accessors) - 1,
                      "material": len(materials) - 1})
    gltf = {
        "asset": {"version": "2.0", "generator": "SUMO-GEO make_tree_glb.py"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": prims}], "materials": materials,
        "accessors": accessors, "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_chunks)}],
    }
    js = json.dumps(gltf, separators=(",", ":")).encode()
    while len(js) % 4:
        js += b" "
    total = 12 + 8 + len(js) + 8 + len(bin_chunks)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", 0x46546C67, 2, total))
        fh.write(struct.pack("<II", len(js), 0x4E4F534A) + js)
        fh.write(struct.pack("<II", len(bin_chunks), 0x004E4942) + bytes(bin_chunks))


def main() -> None:
    out = sys.argv[1] if len(sys.argv) > 1 else "frontend/models"
    os.makedirs(out, exist_ok=True)
    trunk = (0.38, 0.26, 0.15)
    # árbol de copa redonda (~7 m): tronco 2.2 m + dos esferas facetadas
    write_glb(os.path.join(out, "tree.glb"), [
        (_cylinder(0.22, 0.0, 2.4, 7, 0.16), trunk),
        (_sphere(0, 0, 4.4, 2.3, 5, 8, 0.9), (0.30, 0.56, 0.24)),
        (_sphere(0.6, -0.4, 5.6, 1.5, 4, 7, 0.85), (0.36, 0.64, 0.28)),
    ])
    # árbol tipo pino (~9 m): tronco + tres conos
    write_glb(os.path.join(out, "tree-pine.glb"), [
        (_cylinder(0.2, 0.0, 3.0, 7, 0.14), trunk),
        (_cone(2.4, 3.2, 2.2, 8), (0.22, 0.46, 0.24)),
        (_cone(4.6, 2.8, 1.7, 8), (0.25, 0.50, 0.26)),
        (_cone(6.6, 2.4, 1.2, 8), (0.28, 0.54, 0.28)),
    ])
    for f in ("tree.glb", "tree-pine.glb"):
        print(f, os.path.getsize(os.path.join(out, f)), "bytes")


if __name__ == "__main__":
    main()
