#!/usr/bin/env python3
"""Inject <param key="height"> into building polygons from OSM tags.

height = OSM `height` tag, else `building:levels` x 3 m, else a default.
polyconvert keeps the OSM way id as the poly id, so matching is by id.

Usage: python enrich_heights.py <osm_file> <poly_file>  (edits poly_file in place)
"""
import sys
import xml.etree.ElementTree as ET

LEVEL_HEIGHT_M = 3.0
DEFAULT_HEIGHT_M = 10.0


def main(osm_path: str, poly_path: str) -> None:
    heights: dict[str, float] = {}
    for way in ET.parse(osm_path).getroot().iter("way"):
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        if "building" not in tags and "building:part" not in tags:
            continue
        h = None
        if tags.get("height"):
            try:
                h = float(str(tags["height"]).split()[0].replace(",", "."))
            except ValueError:
                pass
        if h is None and tags.get("building:levels"):
            try:
                h = float(tags["building:levels"]) * LEVEL_HEIGHT_M
            except ValueError:
                pass
        heights[way.get("id")] = h if h else DEFAULT_HEIGHT_M

    tree = ET.parse(poly_path)
    count = 0
    for poly in tree.getroot().iter("poly"):
        if not (poly.get("type", "") or "").startswith("building"):
            continue
        h = heights.get(poly.get("id"), DEFAULT_HEIGHT_M)
        existing = [p for p in poly.findall("param") if p.get("key") == "height"]
        if existing:
            existing[0].set("value", str(h))
        else:
            p = ET.SubElement(poly, "param")
            p.set("key", "height")
            p.set("value", str(h))
        count += 1
    tree.write(poly_path, encoding="UTF-8", xml_declaration=True)
    print(f"heights set on {count} buildings")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python enrich_heights.py <osm_file> <poly_file>")
    main(sys.argv[1], sys.argv[2])
