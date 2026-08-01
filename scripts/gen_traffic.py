#!/usr/bin/env python3
"""Generate 24 h traffic demand for a SUMO network with a realistic daily
(diurnal) profile, car types from a vTypes file, plus buses and a tram line.

For each level (low / mid / high) it samples random trips and routes them with
``duarouter``, which DROPS any trip with no valid route so the simulation emits no
"no route for vehicle" warnings, then writes a ready-to-run ``.sumocfg``. A share
of the trips are buses. Pass ``--no-duarouter`` for the old fast (raw trips) mode.

Usage:
  gen_traffic.py --net NET.net.xml --types vtypes.add.xml --transit transit.add.xml \
      --outdir DIR --prefix cuenca --low 3000 --mid 8000 --high 18000 \
      --bus-share 0.05 [--tram --tram-period 600] [--end 86400] [--seed 42] \
      [--center-bbox="minLon,minLat,maxLon,maxLat"]

Pass --center-bbox to keep ALL trips inside one area (e.g. only the city centre of
a large metropolitan network). Note the `=` and quotes so the leading minus of the
west longitude isn't parsed as a flag.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import subprocess

import sumolib

# Weekday demand share per hour (0..23) — nightly valley, AM & PM rush peaks.
HOURLY = [0.6, 0.4, 0.3, 0.3, 0.5, 1.2, 3.0, 6.5, 7.5, 5.5, 4.8, 4.8,
          5.5, 5.5, 4.8, 4.8, 5.5, 7.5, 8.0, 5.5, 3.5, 2.5, 1.6, 1.0]


def read_vtype_ids(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        ids = re.findall(r'<vType\s+id="([^"]+)"', f.read())
    if not ids:
        raise SystemExit(f"No <vType id=...> found in {path}")
    return ids


def candidate_edges(net, bbox=None) -> list[str]:
    """Usable edges. If ``bbox`` = (minLon, minLat, maxLon, maxLat) is given, keep
    only edges whose midpoint falls inside it — this concentrates all trip origins
    and destinations in that area (e.g. only the city centre)."""
    geo = False
    try:
        geo = net.hasGeoProj()
    except Exception:
        geo = False
    out = []
    for e in net.getEdges():
        if e.isSpecial():
            continue
        try:
            usable = (e.allows("evehicle") or e.allows("passenger")) and e.getOutgoing()
        except Exception:
            usable = bool(e.getOutgoing())
        if not (usable and e.getLength() >= 15):
            continue
        if bbox is not None:
            shp = e.getShape()
            x, y = shp[len(shp) // 2]
            lon, lat = net.convertXY2LonLat(x, y) if geo else (x, y)
            if not (bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]):
                continue
        out.append(e.getID())
    return out


def sample_depart(rng: random.Random, end: float) -> float:
    hours = min(24, max(1, int(end // 3600)))
    hour = rng.choices(range(hours), weights=HOURLY[:hours], k=1)[0]
    return min(end - 0.1, hour * 3600 + rng.uniform(0, 3600))


def tram_corridor(net, edge_ids: list[str]) -> tuple[list[str], list[str]]:
    """A long cross-town route (both directions) for the tram, along roads."""
    xmin, ymin, xmax, ymax = net.getBoundary()
    ymid = (ymin + ymax) / 2.0

    def nearest(tx, ty):
        best, bd = None, 1e30
        for eid in edge_ids:
            shp = net.getEdge(eid).getShape()
            x, y = shp[len(shp) // 2]
            d = (x - tx) ** 2 + (y - ty) ** 2
            if d < bd:
                bd, best = d, net.getEdge(eid)
        return best

    a = nearest(xmin + (xmax - xmin) * 0.12, ymid)
    b = nearest(xmin + (xmax - xmin) * 0.88, ymid)
    fwd, _ = net.getShortestPath(a, b, vClass="bus")
    bwd, _ = net.getShortestPath(b, a, vClass="bus")
    fwd_ids = [e.getID() for e in fwd] if fwd else []
    bwd_ids = [e.getID() for e in bwd] if bwd else []
    return fwd_ids, bwd_ids


def route_with_duarouter(net_path, trips_file, add_paths, rou_file):
    """Precompute & validate routes with duarouter, DROPPING trips that have no
    route (``--ignore-errors``) so the simulation emits no 'no route for vehicle'
    warnings. Returns the number of routed vehicles."""
    duarouter = sumolib.checkBinary("duarouter")
    subprocess.run(
        [duarouter, "-n", net_path, "-r", trips_file, "-a", add_paths,
         "-o", rou_file, "--ignore-errors", "true", "--repair", "true",
         "--no-warnings", "true", "--no-step-log", "true", "--routing-threads", "4"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for junk in (trips_file, rou_file[:-8] + ".rou.alt.xml"):  # drop trips + alt file
        try:
            os.remove(junk)
        except OSError:
            pass
    try:
        with open(rou_file, encoding="utf-8") as f:
            return f.read().count("<vehicle ")
    except OSError:
        return 0


def write_level(name, count, edges, car_types, end, net_file, net_path, refs,
                add_paths, outdir, prefix, seed, bus_share, tram_fwd, tram_bwd,
                tram_period, route):
    rng = random.Random(seed)
    trips = []
    for _ in range(count):
        a = rng.choice(edges)
        b = rng.choice(edges)
        if a == b:
            b = rng.choice(edges)
        vt = "bus" if rng.random() < bus_share else rng.choice(car_types)
        trips.append((sample_depart(rng, end), a, b, vt))
    trips.sort(key=lambda t: t[0])

    base = os.path.join(outdir, f"{prefix}_{name}")
    rou = base + ".rou.xml"
    src = base + ".trips.xml" if route else rou   # duarouter reads trips -> writes rou
    with open(src, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<routes>\n')
        # tram line (fixed corridor, both ways, all day)
        if tram_fwd:
            f.write(f'    <route id="tram_fwd" edges="{" ".join(tram_fwd)}"/>\n')
            f.write(f'    <flow id="tramF" type="tram" route="tram_fwd" '
                    f'begin="0" end="{int(end)}" period="{tram_period}"/>\n')
        if tram_bwd:
            f.write(f'    <route id="tram_bwd" edges="{" ".join(tram_bwd)}"/>\n')
            f.write(f'    <flow id="tramB" type="tram" route="tram_bwd" '
                    f'begin="{tram_period // 2}" end="{int(end)}" period="{tram_period}"/>\n')
        for i, (dep, a, b, vt) in enumerate(trips):
            f.write(f'    <trip id="{name}_{i}" type="{vt}" depart="{dep:.1f}" '
                    f'from="{a}" to="{b}"/>\n')
        f.write('</routes>\n')

    routed = route_with_duarouter(net_path, src, add_paths, rou) if route else None

    # duarouter embeds the used vTypes in the routed file, so reference only it
    # (avoids "duplicate vType"); raw-trip mode still needs the vtypes/transit refs.
    route_files = f"{prefix}_{name}.rou.xml" if route else f"{refs},{prefix}_{name}.rou.xml"
    cfg = base + ".sumocfg"
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <input>
        <net-file value="{net_file}"/>
        <route-files value="{route_files}"/>
    </input>
    <time><begin value="0"/><end value="{int(end)}"/><step-length value="1.0"/></time>
    <processing>
        <ignore-route-errors value="true"/>
        <time-to-teleport value="120"/>
    </processing>
    <report><no-step-log value="true"/></report>
</configuration>
""")
    n_bus = sum(1 for _d, _a, _b, vt in trips if vt == "bus")
    extra = " + tram" if tram_fwd else ""
    kept = f" -> {routed} con ruta (duarouter)" if routed is not None else ""
    print(f"  {name:4}: {len(trips):>7} trips ({n_bus} buses){extra}{kept} -> {os.path.basename(rou)}")
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--net", required=True)
    ap.add_argument("--types", required=True)
    ap.add_argument("--transit", required=True, help="file with bus/tram vTypes")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--low", type=int, default=3000)
    ap.add_argument("--mid", type=int, default=8000)
    ap.add_argument("--high", type=int, default=18000)
    ap.add_argument("--bus-share", type=float, default=0.05)
    ap.add_argument("--tram", action="store_true", help="also add a fixed tram line")
    ap.add_argument("--tram-period", type=int, default=600)
    ap.add_argument("--end", type=float, default=86400.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--center-bbox", default=None,
                    help="minLon,minLat,maxLon,maxLat: restrict all trips to this area")
    ap.add_argument("--no-duarouter", action="store_true",
                    help="skip duarouter (write raw trips; faster but may warn 'no route')")
    ap.add_argument("--levels", default="low,mid,high",
                    help="comma-separated subset of levels to (re)generate")
    args = ap.parse_args()

    refs = f"{os.path.basename(args.types)},{os.path.basename(args.transit)}"
    net_file = os.path.basename(args.net)
    add_paths = f"{args.types},{args.transit}"      # full paths for duarouter -a
    route = not args.no_duarouter
    print(f"Loading {args.net} ...")
    net = sumolib.net.readNet(args.net)
    bbox = tuple(float(v) for v in args.center_bbox.split(",")) if args.center_bbox else None
    edges = candidate_edges(net, bbox)
    if not edges:
        raise SystemExit("No candidate edges (check --center-bbox covers roads in the net).")
    car_types = read_vtype_ids(args.types)
    fwd, bwd = tram_corridor(net, edges) if args.tram else ([], [])
    area = f" | zona centro {args.center_bbox}" if bbox else ""
    print(f"  candidate edges: {len(edges)} | car types: {len(car_types)}"
          + (f" | tram corridor: {len(fwd)}+{len(bwd)} edges" if args.tram else "") + area)
    os.makedirs(args.outdir, exist_ok=True)

    want = {s.strip() for s in args.levels.split(",")}
    for name, count in (("low", args.low), ("mid", args.mid), ("high", args.high)):
        if name not in want:
            continue
        write_level(name, count, edges, car_types, args.end, net_file, args.net, refs,
                    add_paths, args.outdir, args.prefix, args.seed + hash(name) % 1000,
                    args.bus_share, fwd, bwd, args.tram_period, route)
    print("Done.")


if __name__ == "__main__":
    main()
