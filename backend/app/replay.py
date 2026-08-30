"""Replay offline del intercambio de mensajes V2X desde los .pcap de VaN3Twin.

Cada nodo ns-3 escribe un pcap (``v2v-EVA-<n>-0.pcap``) con TODO lo que su
interfaz 802.11p vio: sus transmisiones y sus recepciones. De ahí se
reconstruye, sin SUMO ni TraCI:

* **Eventos TX** (una transmisión por mensaje, desde el pcap del emisor) y
  **eventos RX** (una arista tx→rx por cada receptor que lo capturó).
* **Trayectorias**: el encabezado GeoNetworking (SO PV) lleva lat/lon del
  emisor en cada paquete — movilidad a ~10 Hz sin decodificar ASN.1.
* **Estadísticas**: tasas por tipo, PDR por par (rx/tx dentro de la corrida).
* **Contenido**: la decodificación ASN.1 (CAM/CPM/DENM, UPER) es perezosa —
  solo al inspeccionar un mensaje — usando los .asn oficiales del árbol de
  VaN3Twin (``asn_dir``).

Formatos: pcap clásico ns-3 DLT 105 (802.11 sin radiotap), LLC/SNAP 0x8947,
GN Basic(4)+Common(8)+SO PV(24)+reserved(4), BTP-B(4). Validado contra los
pcaps reales del ejemplo EVA (SUMO 1.12 / VaN3Twin ago-2026).
"""
from __future__ import annotations

import glob
import math
import os
import re
import struct
from bisect import bisect_left, bisect_right
from collections import defaultdict

BTP_TYPES = {2001: "CAM", 2002: "DENM", 2009: "CPM", 2006: "IVIM", 2018: "VAM"}


def _meters(p1, p2):
    """Distancia aproximada en metros entre dos (lat, lon)."""
    kx = 111320.0 * math.cos(math.radians(p1[0]))
    return math.hypot((p2[1] - p1[1]) * kx, (p2[0] - p1[0]) * 110540.0)

# ficheros .asn por tipo, relativos a asn_dir (= carpeta ASN1 del árbol).
# OJO: para CPM usar full-v1-v2/CPM-all.asn (autocontenido, compila en <1 s);
# la variante TR103562+ISO_TS_19091 tarda MINUTOS en compilar con asn1tools.
_ASN_SETS = {
    "CAM": ["asn1-v2/EN302637-2v141-CAM.asn", "asn1-v2/TS102894-2v131-CDD.asn"],
    "CPM": ["full-v1-v2/CPM-all.asn"],
    "DENM": ["asn1-v2/DENM-PDU-Descriptions-1.asn",
             "asn1-v2/TS102894-2v131-CDD.asn"],
}
_ASN_TOP = {"CAM": "CAM", "CPM": "CollectivePerceptionMessage", "DENM": "DENM"}


# Specs ASN.1 compiladas, compartidas entre TODAS las instancias del proceso.
# Compilar la spec CAM tarda ~0.3 s: hacerlo por instancia significaba pagarlo
# en cada recarga del índice en vivo (ráfaga de GIL → micro-pausas del stream).
_SPEC_CACHE: dict = {}


class ReplayData:
    """Índice en memoria de una corrida: eventos, trayectorias y stats.

    ``live=True`` = modo ligero para el índice EN VIVO: el mapeo nodo→station
    decodifica solo el ÚLTIMO CAM propio de cada nodo (el mapeo vigente) en
    vez de todos (miles de decodes asn1tools que crecían con la corrida y
    pausaban el visor por contención de GIL). Suficiente para anclar los
    eventos recientes que muestra el modo en vivo."""

    def __init__(self, pcap_dir: str, asn_dir: str | None = None,
                 pattern: str = "*.pcap", live: bool = False):
        self.pcap_dir = pcap_dir
        self.asn_dir = asn_dir
        self.pattern = pattern
        self.live = live
        self.stations: list[int] = []          # ids de estación presentes
        self.tx: list[dict] = []               # eventos TX ordenados por t
        self.rx: list[dict] = []               # eventos RX ordenados por t
        self._tx_t: list[float] = []
        self._rx_t: list[float] = []
        self.traj: dict[int, list] = {}        # station -> [(t,lat,lon,speed,heading)]
        self.t0 = 0.0
        self.t1 = 0.0
        self.stats: dict = {}
        self._specs: dict = {}                 # tipo -> spec asn1tools compilada
        self._payloads: dict = {}              # (station, round(t*1e6)) -> bytes
        self.load()

    # ------------------------------------------------------------------ carga
    @staticmethod
    def _node_of(path: str) -> int | None:
        m = re.search(r"-(\d+)-\d+\.pcap$", os.path.basename(path))
        return int(m.group(1)) if m else None

    def load(self) -> None:
        files = sorted(glob.glob(os.path.join(self.pcap_dir, self.pattern)))
        files = [f for f in files if self._node_of(f) is not None]
        if not files:
            raise FileNotFoundError(
                f"sin pcaps '{self.pattern}' en {self.pcap_dir}")
        per_node = {self._node_of(f): f for f in files}
        self.stations = sorted(per_node)
        tx_seen: set = set()
        counts = defaultdict(int)
        pair_rx = defaultdict(int)
        traj = defaultdict(list)

        for node, path in per_node.items():
            own_mac = node + 1                  # ns-3: nodo i -> 00:...:00:(i+1)
            for p in self._packets(path):
                t, src, port, lat, lon, speed, heading, payload, life, hop = p
                mtype = BTP_TYPES.get(port, f"BTP{port}")
                station = src - 1               # MAC -> stationID (== índice de nodo)
                if src == own_mac:
                    key = (station, round(t * 1e6))
                    if key in tx_seen:
                        continue
                    tx_seen.add(key)
                    self.tx.append({"t": t, "tx": station, "type": mtype})
                    # payload + metadatos de las capas inferiores (disección
                    # estilo Wireshark en el popup del visor)
                    self._payloads[key] = (payload, {
                        "mac": "00:00:00:00:00:%02x" % src, "port": port,
                        "lat": lat, "lon": lon, "speed": speed,
                        "heading": heading, "lifetime": life, "hop": hop})
                    counts["tx_" + mtype] += 1
                    traj[station].append((t, lat, lon, speed, heading))
                else:
                    self.rx.append({"t": t, "tx": station, "rx": node,
                                    "type": mtype})
                    counts["rx_" + mtype] += 1
                    pair_rx[(station, node)] += 1
                    # posición del emisor también visible en recepciones
                    traj[station].append((t, lat, lon, speed, heading))

        # --- mapear nodo ns-3 -> stationID ETSI real (== nº de vehículo SUMO).
        # OJO: el pool de nodos del EVA SE REUTILIZA — cuando un vehículo sale
        # de SUMO, su nodo (MAC y pcap) pasa al siguiente que entra. Un mismo
        # pcap contiene varios stationID a lo largo del tiempo, así que el
        # mapeo es por SEGMENTOS temporales, decodificando los CAM propios de
        # cada nodo. Sin .asn disponibles, se usa el índice de nodo tal cual.
        segments, self.node_stype = self._station_segments()
        if segments:
            def sid_at(node: int, t: float) -> int:
                seg = segments.get(node)
                if not seg:
                    return node
                i = bisect_right([s[0] for s in seg], t) - 1
                return seg[max(i, 0)][1]

            new_traj: dict[int, list] = defaultdict(list)
            for e in self.tx:
                e["tx"] = sid_at(e["tx"], e["t"])
            for e in self.rx:
                e["tx"] = sid_at(e["tx"], e["t"])
                e["rx"] = sid_at(e["rx"], e["t"])
            for n, pts in traj.items():
                for p in pts:
                    new_traj[sid_at(n, p[0])].append(p)
            traj = new_traj
            self._payloads = {(sid_at(n, k / 1e6), k): v
                              for (n, k), v in self._payloads.items()}
            self.stations = sorted({s for seg in segments.values()
                                    for _, s in seg})

        self.tx.sort(key=lambda e: e["t"])
        self.rx.sort(key=lambda e: e["t"])
        self._tx_t = [e["t"] for e in self.tx]
        self._rx_t = [e["t"] for e in self.rx]
        for st, pts in traj.items():
            pts.sort()
            dedup = []
            for p in pts:                       # una muestra por instante
                if not dedup or p[0] > dedup[-1][0]:
                    dedup.append(p)
            self.traj[st] = dedup
        ts = [e["t"] for e in self.tx] or [0.0]
        self.t0, self.t1 = min(ts), max(max(ts), max(self._rx_t or [0.0]))

        tx_by_station = defaultdict(int)
        for e in self.tx:
            tx_by_station[e["tx"]] += 1
        pair_final = defaultdict(int)           # recuento tras el remapeo
        for e in self.rx:
            pair_final[(e["tx"], e["rx"])] += 1
        pdr = {}
        for (a, b), n in pair_final.items():
            if tx_by_station[a]:
                # clamp a 1.0: en los bordes de la reutilización del pool de
                # nodos puede duplicarse alguna recepción y superar el 100 %
                pdr[f"{a}->{b}"] = round(min(n / tx_by_station[a], 1.0), 3)
        self.stats = {
            "counts": dict(counts),
            "stations": self.stations,
            "duration": round(self.t1 - self.t0, 3),
            "pdr_pairs": pdr,
        }
        self._load_signal()                     # potencia RX opcional (CSV)

    def _station_segments(self) -> tuple[dict, dict]:
        """Por nodo, línea temporal [(t_inicio, stationID), …] decodificando
        TODOS sus CAM propios (el pool de nodos se reutiliza entre vehículos).
        También stationID -> stationType. ({}, {}) si no hay spec ASN.1."""
        spec = self._spec("CAM")
        if spec is None:
            return {}, {}
        segments: dict[int, list] = defaultdict(list)
        stype: dict[int, int] = {}
        if self.live:
            # modo vivo: solo el último CAM de cada nodo (mapeo vigente) —
            # un decode por nodo por recarga en vez de miles
            last: dict[int, dict] = {}
            for e in self.tx:
                if e["type"] == "CAM":
                    last[e["tx"]] = e
            for node, e in last.items():
                entry = self._payloads.get((node, round(e["t"] * 1e6)))
                if not entry:
                    continue
                try:
                    d = spec.decode("CAM", entry[0])
                    sid = int(d["header"]["stationID"])
                except Exception:  # noqa: BLE001
                    continue
                segments[node] = [(0.0, sid)]
                stype[sid] = int(d["cam"]["camParameters"]
                                 ["basicContainer"]["stationType"])
            return dict(segments), stype
        for e in self.tx:                       # aún etiquetados por nodo
            if e["type"] != "CAM":
                continue
            entry = self._payloads.get((e["tx"], round(e["t"] * 1e6)))
            if not entry:
                continue
            try:
                d = spec.decode("CAM", entry[0])
                sid = int(d["header"]["stationID"])
            except Exception:  # noqa: BLE001
                continue
            seg = segments[e["tx"]]
            if not seg or seg[-1][1] != sid:
                seg.append((e["t"], sid))
                stype[sid] = int(d["cam"]["camParameters"]
                                 ["basicContainer"]["stationType"])
        return dict(segments), stype

    def _packets(self, path: str):
        """Genera (t, src_low_byte, btp_port, lat, lon, speed, heading, payload)."""
        with open(path, "rb") as fh:
            data = fh.read()
        if len(data) < 24 or struct.unpack("<I", data[:4])[0] != 0xA1B2C3D4:
            return
        off = 24
        n = len(data)
        while off + 16 <= n:
            ts, tus, incl, _ = struct.unpack("<IIII", data[off:off + 16])
            off += 16
            pkt = data[off:off + incl]
            off += incl
            if len(pkt) < 44:
                continue
            fc = pkt[0] | (pkt[1] << 8)
            h = 24 + (2 if (fc & 0x00F0) == 0x0080 else 0)   # +2 si QoS Data
            if pkt[h + 6:h + 8] != b"\x89\x47":
                continue
            gn = pkt[h + 8:]
            if len(gn) < 44:
                continue
            pl = (gn[8] << 8) | gn[9]
            if pl == 0:                                       # GN Beacon
                continue
            src = pkt[15]                                     # byte bajo de addr2
            lat, lon = struct.unpack(">ii", gn[24:32])
            speed = ((gn[32] << 8 | gn[33]) & 0x7FFF) / 100.0  # PAI(1)+speed(15)
            heading = ((gn[34] << 8) | gn[35]) / 10.0
            port = (gn[40] << 8) | gn[41]
            yield (ts + tus / 1e6, src, port, lat / 1e7, lon / 1e7,
                   speed, heading, bytes(gn[44:44 + pl - 4]),
                   gn[2], gn[3])                              # lifetime, hopLimit

    # ------------------------------------------------------- consultas por t
    def window(self, t_from: float, t_to: float, max_events: int = 400) -> dict:
        """Eventos TX y RX con t en (t_from, t_to] (recortados si son muchos)."""
        txs = self.tx[bisect_right(self._tx_t, t_from):
                      bisect_right(self._tx_t, t_to)]
        rxs = self.rx[bisect_right(self._rx_t, t_from):
                      bisect_right(self._rx_t, t_to)]
        if len(rxs) > max_events:                # muestreo uniforme
            step = len(rxs) / max_events
            rxs = [rxs[int(i * step)] for i in range(max_events)]
        return {"tx": txs, "rx": rxs}

    def positions(self, t: float) -> list[dict]:
        """Posición interpolada de cada estación en el instante t."""
        out = []
        for st, pts in self.traj.items():
            ts = [p[0] for p in pts]
            i = bisect_left(ts, t)
            if i == 0:
                if t < pts[0][0] - 2.0:
                    continue                     # aún no ha aparecido
                p = pts[0]
                lat, lon, speed, heading = p[1], p[2], p[3], p[4]
            elif i >= len(pts):
                if t > pts[-1][0] + 2.0:
                    continue                     # ya salió
                p = pts[-1]
                lat, lon, speed, heading = p[1], p[2], p[3], p[4]
            else:
                a, b = pts[i - 1], pts[i]
                f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
                lat = a[1] + (b[1] - a[1]) * f
                lon = a[2] + (b[2] - a[2]) * f
                speed = a[3] + (b[3] - a[3]) * f
                da = ((b[4] - a[4] + 540) % 360) - 180
                heading = (a[4] + da * f) % 360
            emerg = self.node_stype.get(st) == 10      # ETSI specialVehicle
            out.append({"id": f"veh{st}", "station": st,
                        "lon": round(lon, 7), "lat": round(lat, 7),
                        "angle": round(heading, 1), "speed": round(speed, 2),
                        "type": "emergency" if emerg else "passenger",
                        "len": 6.5 if emerg else 4.5,
                        "wid": 2.0 if emerg else 1.8,
                        "edge": ""})
        return out

    # ------------------------------------------ potencia RX (signal*.csv, opc.)
    def _load_signal(self) -> None:
        """Ingesta opcional de potencia de recepción por mensaje, volcada por
        la app de VaN3Twin (callbacks extendidos con SignalInfo) a un CSV en
        la misma carpeta que los pcap. Formato (cabecera, orden libre):
        ``rx,tx,t_ms,rssi[,snr]`` — rx/tx = stationID (admite "veh5"),
        t en ms de simulación (o s: se detecta), rssi en dBm."""
        import csv
        self._sig = {}
        self.signal_samples = 0
        rows_all: list[tuple] = []
        for path in sorted(glob.glob(os.path.join(self.pcap_dir,
                                                  "signal*.csv"))):
            try:
                with open(path, newline="") as fh:
                    for row in csv.DictReader(fh):
                        r = {k.strip().lower(): (v or "").strip()
                             for k, v in row.items() if k}

                        def num(*names):
                            for n in names:
                                if n in r and r[n] != "":
                                    return float(re.sub(r"[^0-9.+-]", "",
                                                        r[n]) or "nan")
                            return None
                        rx = num("rx", "rx_id", "receiver")
                        tx = num("tx", "tx_id", "stationid", "camid", "sender")
                        t = num("t_ms", "timestamp", "time_ms", "t", "time")
                        rssi = num("rssi", "rssi_dbm", "power")
                        snr = num("snr", "snr_db", "sinr")
                        if None in (rx, tx, t, rssi) or math.isnan(rssi):
                            continue
                        rows_all.append((int(tx), int(rx), t, rssi, snr))
            except Exception:  # noqa: BLE001 - fichero opcional
                continue
        if not rows_all:
            return
        ms = max(x[2] for x in rows_all) > 1e4      # heurística ms vs s
        sig = defaultdict(list)
        for tx, rx, t, rssi, snr in rows_all:
            sig[(tx, rx)].append((t / 1000.0 if ms else t, rssi, snr))
        for v in sig.values():
            v.sort()
        self._sig = dict(sig)
        # asociar cada evento RX a la muestra más cercana (±50 ms)
        for e in self.rx:
            seq = self._sig.get((e["tx"], e["rx"]))
            if not seq:
                continue
            ts = [s[0] for s in seq]
            i = bisect_left(ts, e["t"])
            for j in (i, i - 1):
                if 0 <= j < len(seq) and abs(seq[j][0] - e["t"]) < 0.05:
                    e["rssi"] = round(seq[j][1], 1)
                    if seq[j][2] is not None and not math.isnan(seq[j][2]):
                        e["snr"] = round(seq[j][2], 1)
                    self.signal_samples += 1
                    break

    # ---------------------------------------------- estadísticas de capa física
    def _pos_at(self, st: int, t: float):
        pts = self.traj.get(st)
        if not pts:
            return None
        ts = [p[0] for p in pts]
        i = bisect_left(ts, t)
        if i == 0:
            return (pts[0][1], pts[0][2]) if t > pts[0][0] - 2.0 else None
        if i >= len(pts):
            return (pts[-1][1], pts[-1][2]) if t < pts[-1][0] + 2.0 else None
        a, b = pts[i - 1], pts[i]
        f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
        return (a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)

    def phy_stats(self) -> dict:
        """Métricas PHY/MAC derivadas de los pcap (sin radiotap no hay potencia
        RX por trama; el resto se mide de eventos y trayectorias reales)."""
        if getattr(self, "_phy", None):
            return self._phy

        meters = _meters

        def pct(v, q):
            if not v:
                return None
            s = sorted(v)
            return s[min(int(len(s) * q), len(s) - 1)]

        by_st: dict[int, list] = defaultdict(list)
        for e in self.tx:
            by_st[e["tx"]].append(e)
        lat, dist = [], []
        for r in self.rx:                       # emparejar RX con su TX
            seq = by_st.get(r["tx"])
            if not seq:
                continue
            ts = [e["t"] for e in seq]
            i = bisect_right(ts, r["t"])
            best = None
            for j in (i - 1, i - 2):
                if (0 <= j < len(seq) and seq[j]["type"] == r["type"]
                        and 0.0 <= r["t"] - seq[j]["t"] < 0.03):
                    best = seq[j]
                    break
            if not best:
                continue
            lat.append((r["t"] - best["t"]) * 1000.0)
            p1 = self._pos_at(r["tx"], best["t"])
            p2 = self._pos_at(r["rx"], r["t"])
            if p1 and p2:
                dist.append(meters(p1, p2))

        # RX esperadas: por cada TX, estaciones coexistentes (traj) menos el emisor
        life = {st: (pts[0][0], pts[-1][0]) for st, pts in self.traj.items()}
        expected = 0
        for e in self.tx:
            alive = sum(1 for st, (a, b) in life.items()
                        if a - 2.0 <= e["t"] <= b + 2.0)
            expected += max(alive - 1, 0)
        per = (1.0 - len(self.rx) / expected) if expected else None

        # utilización de canal: preámbulo 40 µs + (payload + ~80 B de cabeceras)
        # a 12 Mbit/s, sobre la duración de la corrida
        dur = max(self.t1 - self.t0, 1e-6)
        air = sum(40e-6 + ((len(v[0]) + 80) * 8) / 12e6
                  for v in self._payloads.values())
        pdr = self.stats.get("pdr_pairs", {})
        best_pair = max(pdr, key=pdr.get) if pdr else None
        worst_pair = min(pdr, key=pdr.get) if pdr else None
        n_tx = len(self.tx)
        rssi_v = [e["rssi"] for e in self.rx if "rssi" in e]
        nota_rx = (f"RSSI por mensaje: {len(rssi_v)} muestras de signal*.csv"
                   if rssi_v else
                   "potencia RX por trama no disponible: pcap ns-3 sin "
                   "radiotap. Genera signal-rx.csv con los callbacks "
                   "SignalInfo del EVA (ver manual)")
        self._phy = {
            "config": {                          # constantes del ejemplo EVA
                "banda": "5,9 GHz (ITS-G5)",
                "canal": "CCH 178 · 5,890 GHz",
                "bw": "10 MHz",
                "modulacion": "OFDM 12 Mbit/s (16-QAM 1/2 @ 10 MHz)",
                "tx_power": "30 dBm (flag --tx-power del EVA)",
                "nota_rx": nota_rx,
            },
            "rssi_dbm": ({"mean": round(sum(rssi_v) / len(rssi_v), 1),
                          "p50": round(pct(rssi_v, 0.5), 1),
                          "min": round(min(rssi_v), 1),
                          "max": round(max(rssi_v), 1),
                          "n": len(rssi_v)} if rssi_v else None),
            "latency_ms": {"mean": round(sum(lat) / len(lat), 2) if lat else None,
                           "p50": round(pct(lat, 0.5), 2) if lat else None,
                           "p95": round(pct(lat, 0.95), 2) if lat else None,
                           "max": round(max(lat), 2) if lat else None,
                           "n": len(lat)},
            "range_m": {"p50": round(pct(dist, 0.5)) if dist else None,
                        "p95": round(pct(dist, 0.95)) if dist else None,
                        "max": round(max(dist)) if dist else None},
            "per": round(per, 4) if per is not None else None,
            "expected_rx": expected,
            "got_rx": len(self.rx),
            "pdr_best": {best_pair: pdr[best_pair]} if best_pair else {},
            "pdr_worst": {worst_pair: pdr[worst_pair]} if worst_pair else {},
            "rates_per_s": {k.replace("tx_", ""): round(v / dur, 2)
                            for k, v in self.stats["counts"].items()
                            if k.startswith("tx_")},
            "channel_util": round(air / dur, 4),
            "frames": {"tx": n_tx, "rx": len(self.rx)},
        }
        return self._phy

    # ------------------------------------------------- contenido (decode lazy)
    def _spec(self, mtype: str):
        if mtype in self._specs:
            return self._specs[mtype]
        key = (self.asn_dir, mtype)
        if key in _SPEC_CACHE:                  # compartida entre instancias
            self._specs[mtype] = _SPEC_CACHE[key]
            return self._specs[mtype]
        if not self.asn_dir or mtype not in _ASN_SETS:
            self._specs[mtype] = None
            return None
        import asn1tools
        try:
            files = [os.path.join(self.asn_dir, f) for f in _ASN_SETS[mtype]]
            files = [f for f in files if os.path.exists(f)]
            self._specs[mtype] = asn1tools.compile_files(files, "uper")
        except Exception:
            self._specs[mtype] = None
        _SPEC_CACHE[key] = self._specs[mtype]
        return self._specs[mtype]

    def decode(self, station: int, t: float, mtype_hint: str | None = None) -> dict:
        """Contenido decodificado del mensaje TX de `station` más cercano a t.

        Tolerante en tiempo (±20 ms): un clic sobre un ARCO llega con el
        instante de RECEPCIÓN, unos µs después del TX. `mtype_hint` (CAM/CPM/
        DENM) desambigua si la estación transmitió dos tipos muy seguidos.
        """
        cand = [e for e in self.tx
                if e["tx"] == station and abs(e["t"] - t) < 0.02
                and (mtype_hint is None or e["type"] == mtype_hint)]
        ev = min(cand, key=lambda e: abs(e["t"] - t)) if cand else None
        entry = (self._payloads.get((station, round(ev["t"] * 1e6)))
                 if ev else None)
        if entry is None or ev is None:
            return {"error": "mensaje no encontrado", "tx": station,
                    "mtype": mtype_hint or "?", "t": round(t, 6)}
        payload, meta = entry
        t = ev["t"]
        mtype = ev["type"]
        # OJO: la clave del tipo de mensaje se llama "mtype" (no "type") para
        # no pisar el "type":"msg_detail" del sobre WebSocket al hacer **det.
        out = {"t": round(t, 6), "tx": station, "mtype": mtype,
               "bytes": len(payload),
               # los RX se registran hasta ~16 ms tras el TX (cola + backoff
               # 802.11p en ns-3): ventana de 30 ms para no perder receptores
               "receivers": sorted({r["rx"] for r in self.rx
                                    if r["tx"] == station
                                    and abs(r["t"] - t) < 3e-2}),
               # por receptor: distancia real (trayectorias) y RSSI si hay CSV
               "receivers_info": (lambda p_tx: [
                   {"rx": r["rx"],
                    "dist_m": (round(_meters(p_tx, p_rx))
                               if p_tx and (p_rx := self._pos_at(r["rx"], r["t"]))
                               else None),
                    "rssi": r.get("rssi"), "snr": r.get("snr")}
                   for r in sorted(
                       {r["rx"]: r for r in self.rx
                        if r["tx"] == station
                        and abs(r["t"] - t) < 3e-2}.values(),
                       key=lambda x: x["rx"])
               ])(self._pos_at(station, t)),
               # disección por capas, estilo Wireshark
               "layers": {
                   "ieee80211": {
                       "origen (MAC)": meta["mac"],
                       "destino": "ff:ff:ff:ff:ff:ff (broadcast OCB)",
                       "banda": "5,9 GHz · canal 10 MHz · 802.11p"},
                   "geonetworking": {
                       "cabecera": "SHB (Single Hop Broadcast) · ethertype 0x8947",
                       "posición del emisor (SO PV)": [round(meta["lat"], 7),
                                                       round(meta["lon"], 7)],
                       "velocidad": f"{meta['speed']} m/s",
                       "rumbo": f"{meta['heading']}°",
                       "lifetime (raw)": meta["lifetime"],
                       "hopLimit": meta["hop"]},
                   "btp": {
                       "puerto destino": meta["port"],
                       "servicio": mtype}}}
        spec = self._spec(mtype)
        if spec is not None:
            try:
                out["content"] = _sanitize(
                    spec.decode(_ASN_TOP.get(mtype, mtype), payload))
            except Exception as exc:  # noqa: BLE001
                out["decode_error"] = str(exc)[:200]
        return out


def _sanitize(obj, depth: int = 0):
    """dict decodificado -> JSON-serializable (bytes/tuplas/enum de asn1tools)."""
    if depth > 12:
        return "…"
    if isinstance(obj, dict):
        return {k: _sanitize(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v, depth + 1) for v in obj]
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
