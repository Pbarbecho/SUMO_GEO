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

Escalado (2026-09-29)
---------------------
Con cientos de vehículos cada pcap recibe las tramas de TODOS los vecinos: el
volumen total crece ~N² y el índice en vivo, que releía y reparseaba todos
los ficheros enteros cada 2 s, dominaba la CPU del backend. Ahora:

* ``refresh()`` es INCREMENTAL: recuerda el offset consumido de cada pcap y
  del CSV de señal y solo parsea los bytes añadidos desde la última vez;
* en modo vivo se conserva una ventana deslizante (``live_keep_s``) de
  eventos/trayectorias/payloads; los totales (recuentos, PDR) se acumulan en
  contadores, no se recalculan de las listas;
* todas las búsquedas por tiempo usan listas de tiempos precomputadas por
  estación/par (``bisect``): ``positions()``, ``phy_stats()``, la asociación
  RSSI y ``decode()`` dejan de ser O(eventos × estaciones).
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
_PCAP_MAGIC = 0xA1B2C3D4


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
    decodifica solo el ÚLTIMO CAM propio de cada nodo por lote (el mapeo
    vigente) en vez de todos, y se conserva solo una ventana de eventos."""

    def __init__(self, pcap_dir: str, asn_dir: str | None = None,
                 pattern: str = "*.pcap", live: bool = False,
                 live_keep_s: float = 120.0, live_rx_max: int = 150_000):
        self.pcap_dir = pcap_dir
        self.asn_dir = asn_dir
        self.pattern = pattern
        self.live = live
        self.live_keep_s = live_keep_s
        # modo vivo: nº máximo de eventos RX ALMACENADOS en la ventana. Con N
        # vehículos que se oyen entre sí las recepciones crecen ~N² (60 nodos
        # a 10 Hz = 35k RX/s): se guardan 1 de cada k (k adaptativo) para los
        # arcos del visor y las muestras PHY; los recuentos/PDR/PER se llevan
        # en contadores exactos sobre TODOS los eventos.
        self.live_rx_max = live_rx_max
        self._rx_keep = 1                      # k: se almacena 1 de cada k RX
        self._rx_seq = 0
        self.rx_total = 0                      # RX reales (no diezmadas)
        self.stations: list[int] = []          # ids de estación presentes
        self.tx: list[dict] = []               # eventos TX ordenados por t
        self.rx: list[dict] = []               # eventos RX ordenados por t
        self._tx_t: list[float] = []
        self._rx_t: list[float] = []
        self.traj: dict[int, list] = {}        # station -> [(t,lat,lon,speed,heading)]
        self._traj_t: dict[int, list] = {}     # station -> [t, ...] (bisect)
        self.t0 = 0.0
        self.t1 = 0.0
        self.stats: dict = {}
        self.node_stype: dict = {}
        self.signal_samples = 0
        self._specs: dict = {}                 # tipo -> spec asn1tools compilada
        self._payloads: dict = {}              # (station, round(t*1e6)) -> bytes
        # --- estado incremental
        self._files: dict[int, str] = {}       # node -> path
        self._offsets: dict[str, int] = {}     # path -> bytes ya consumidos
        self._first_rec: dict[str, bytes] = {} # path -> cabecera+1er registro (huella)
        self._segments: dict[int, list] = defaultdict(list)   # node -> [(t, sid)]
        self._seg_t: dict[int, list] = {}
        self._tx_seen: set = set()
        self._counts: dict = defaultdict(int)
        self._tx_by_station: dict = defaultdict(int)
        self._pair_rx: dict = defaultdict(int)
        self._sig: dict = {}                   # (tx, rx) -> [(t, rssi, snr)]
        self._sig_t: dict = {}                 # (tx, rx) -> [t]
        self._sig_offsets: dict[str, int] = {}
        self._sig_ms: bool | None = None
        self._phy = None
        self._t_first: float | None = None
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
        self._files = {self._node_of(f): f for f in files}
        self._offsets = {f: 0 for f in files}
        self._ingest()
        self._finalize()

    def same_run(self, files) -> bool:
        """¿Los ficheros de disco siguen siendo ESTA corrida? (mismos pcaps,
        ninguno truncado y la misma cabecera + primer registro). Si ns-3 ha
        relanzado la corrida, los pcap se reescriben desde cero y hay que
        construir un índice nuevo."""
        pcaps = {f for f in files if f.endswith(".pcap")}
        if pcaps != set(self._files.values()):
            return False
        for path in pcaps:
            try:
                size = os.path.getsize(path)
            except OSError:
                return False
            if size < self._offsets.get(path, 0):
                return False
            fr = self._first_rec.get(path)
            if fr:
                try:
                    with open(path, "rb") as fh:
                        if fh.read(len(fr)) != fr:
                            return False
                except OSError:
                    return False
        return True

    def refresh(self) -> None:
        """Ingesta incremental: solo los bytes nuevos de cada fichero."""
        # pcaps nuevos (nodos que aparecen tarde)
        for f in sorted(glob.glob(os.path.join(self.pcap_dir, self.pattern))):
            n = self._node_of(f)
            if n is not None and n not in self._files:
                self._files[n] = f
                self._offsets[f] = 0
        self._ingest()
        self._finalize()

    def _ingest(self) -> None:
        """Parsea los bytes nuevos de todos los pcaps, resuelve el mapeo
        nodo→stationID del lote y añade los eventos a las listas."""
        batch_tx: list = []                     # (t, node, mtype, key, payload, meta)
        batch_rx: list = []                     # (t, node_tx, node_rx, mtype)
        batch_traj: list = []                   # (node, t, lat, lon, speed, heading)
        for node, path in self._files.items():
            own_mac = node + 1                  # ns-3: nodo i -> 00:...:00:(i+1)
            for p in self._packets(path):
                t, src, port, lat, lon, speed, heading, payload, life, hop = p
                mtype = BTP_TYPES.get(port, f"BTP{port}")
                station = src - 1               # MAC -> nodo emisor
                if src == own_mac:
                    key = (station, round(t * 1e6))
                    if key in self._tx_seen:
                        continue
                    self._tx_seen.add(key)
                    batch_tx.append((t, station, mtype, key[1], payload, {
                        "mac": "00:00:00:00:00:%02x" % src, "port": port,
                        "lat": lat, "lon": lon, "speed": speed,
                        "heading": heading, "lifetime": life, "hop": hop}))
                else:
                    batch_rx.append((t, station, node, mtype))
                batch_traj.append((station, t, lat, lon, speed, heading))
        if not batch_tx and not batch_rx:
            return

        # --- mapear nodo ns-3 -> stationID ETSI real (== nº de vehículo SUMO).
        # OJO: el pool de nodos del EVA SE REUTILIZA — cuando un vehículo sale
        # de SUMO, su nodo (MAC y pcap) pasa al siguiente que entra. Un mismo
        # pcap contiene varios stationID a lo largo del tiempo, así que el
        # mapeo es por SEGMENTOS temporales, decodificando los CAM propios de
        # cada nodo. Sin .asn disponibles, se usa el índice de nodo tal cual.
        batch_tx.sort(key=lambda e: e[0])
        self._update_segments(batch_tx)

        def sid_at(node: int, t: float) -> int:
            seg = self._segments.get(node)
            if not seg:
                return node
            i = bisect_right(self._seg_t[node], t) - 1
            return seg[max(i, 0)][1]

        new_tx = []
        for t, node, mtype, kus, payload, meta in batch_tx:
            sid = sid_at(node, t)
            new_tx.append({"t": t, "tx": sid, "type": mtype})
            self._payloads[(sid, kus)] = (payload, meta)
            self._counts["tx_" + mtype] += 1
            self._tx_by_station[sid] += 1
        self._merge(self.tx, new_tx)
        new_rx = []
        keep = self._rx_keep if self.live else 1
        for t, ntx, nrx, mtype in batch_rx:
            stx, srx = sid_at(ntx, t), sid_at(nrx, t)
            self._counts["rx_" + mtype] += 1
            self._pair_rx[(stx, srx)] += 1
            self.rx_total += 1
            self._rx_seq += 1
            if keep > 1 and self._rx_seq % keep:
                continue                        # diezmado (solo almacenamiento)
            new_rx.append({"t": t, "tx": stx, "rx": srx, "type": mtype})
        new_rx.sort(key=lambda e: e["t"])
        self._merge(self.rx, new_rx)
        for node, t, lat, lon, speed, heading in batch_traj:
            self.traj.setdefault(sid_at(node, t), []).append(
                (t, lat, lon, speed, heading))
        self._load_signal(new_rx)               # potencia RX opcional (CSV)

    @staticmethod
    def _merge(dst: list, new: list) -> None:
        """Añade ``new`` (ordenado por t) a ``dst`` (ordenado) reordenando solo
        la cola que se solapa: los pcaps se escriben concurrentemente, así que
        un lote puede traer eventos algo anteriores al último ya almacenado,
        pero nunca mucho. Evita re-sortear millones de eventos por refresco."""
        if not new:
            return
        if not dst or new[0]["t"] >= dst[-1]["t"]:
            dst.extend(new)
            return
        t_min = new[0]["t"]
        i = len(dst)
        while i > 0 and dst[i - 1]["t"] > t_min:
            i -= 1
        tail = dst[i:]
        del dst[i:]
        tail.extend(new)
        tail.sort(key=lambda e: e["t"])
        dst.extend(tail)

    def _update_segments(self, batch_tx: list) -> None:
        """Actualiza la línea temporal [(t_inicio, stationID), …] de cada nodo
        decodificando sus CAM propios del lote (todos en modo offline; solo el
        último por nodo en modo vivo). También stationID -> stationType."""
        spec = self._spec("CAM")
        if spec is None:
            return
        cams: dict[int, list] = defaultdict(list)
        for t, node, mtype, kus, payload, meta in batch_tx:
            if mtype == "CAM":
                cams[node].append((t, payload))
        for node, lst in cams.items():
            todo = lst[-1:] if self.live else lst
            for t, payload in todo:
                try:
                    d = spec.decode("CAM", payload)
                    sid = int(d["header"]["stationID"])
                except Exception:  # noqa: BLE001
                    continue
                seg = self._segments[node]
                if not seg or seg[-1][1] != sid:
                    # en vivo el primer segmento arranca en 0 (cubre eventos
                    # anteriores al CAM decodificado)
                    seg.append((0.0 if (self.live and not seg) else t, sid))
                    self._seg_t[node] = [s[0] for s in seg]
                    self.node_stype[sid] = int(d["cam"]["camParameters"]
                                               ["basicContainer"]["stationType"])

    def _finalize(self) -> None:
        """Poda (modo vivo) y recalcula índices y estadísticas. Las listas ya
        llegan ordenadas de ``_merge``."""
        if self.tx and self._t_first is None:
            self._t_first = self.tx[0]["t"]
        t_last = max(self.tx[-1]["t"] if self.tx else 0.0,
                     self.rx[-1]["t"] if self.rx else 0.0)
        if self.live and self.live_keep_s > 0:
            # corte referido al último TX (nunca diezmado): determinista
            cut = (self.tx[-1]["t"] if self.tx else t_last) - self.live_keep_s
            if self.tx and self.tx[0]["t"] < cut:
                i = bisect_left([e["t"] for e in self.tx], cut)
                del self.tx[:i]
            if self.rx and self.rx[0]["t"] < cut:
                i = bisect_left([e["t"] for e in self.rx], cut)
                del self.rx[:i]
            if self._payloads and min(self._payloads)[1] / 1e6 < cut:
                self._payloads = {k: v for k, v in self._payloads.items()
                                  if k[1] / 1e6 >= cut}
                self._tx_seen = {k for k in self._tx_seen if k[1] / 1e6 >= cut}
            # k adaptativo: tasa RX real observada × ventana / máximo almacenado
            span = max(t_last - (self.tx[0]["t"] if self.tx else t_last), 1.0)
            rate = self.rx_total / max(self.t1 - self.t0, span, 1.0)
            self._rx_keep = max(1, int(math.ceil(rate * self.live_keep_s
                                                 / max(self.live_rx_max, 1))))
        self._tx_t = [e["t"] for e in self.tx]
        self._rx_t = [e["t"] for e in self.rx]
        cut = (t_last - self.live_keep_s) if (self.live and self.live_keep_s > 0) else None
        for st, pts in list(self.traj.items()):
            pts.sort()
            dedup = []
            for p in pts:                       # una muestra por instante
                if not dedup or p[0] > dedup[-1][0]:
                    if cut is None or p[0] >= cut:
                        dedup.append(p)
            if dedup:
                self.traj[st] = dedup
                self._traj_t[st] = [p[0] for p in dedup]
            else:
                del self.traj[st]
                self._traj_t.pop(st, None)
        self.stations = sorted(set(self._tx_by_station) | set(self.traj))
        self.t0 = self._t_first if self._t_first is not None else 0.0
        self.t1 = max(t_last, self.t0)
        pdr = {}
        for (a, b), n in self._pair_rx.items():
            if self._tx_by_station[a]:
                # clamp a 1.0: en los bordes de la reutilización del pool de
                # nodos puede duplicarse alguna recepción y superar el 100 %
                pdr[f"{a}->{b}"] = round(min(n / self._tx_by_station[a], 1.0), 3)
        self.stats = {
            "counts": dict(self._counts),
            "stations": self.stations,
            "duration": round(self.t1 - self.t0, 3),
            "pdr_pairs": pdr,
            "rx_stored": len(self.rx),
            "rx_decimation": self._rx_keep if self.live else 1,
        }
        self._phy = None                        # invalidar caché PHY

    def _packets(self, path: str):
        """Genera (t, src_low_byte, btp_port, lat, lon, speed, heading, payload,
        lifetime, hopLimit) de los registros NUEVOS del pcap (desde el offset
        consumido). Solo consume registros completos: un registro a medio
        escribir por ns-3 se lee en la siguiente pasada."""
        off = self._offsets.get(path, 0)
        try:
            with open(path, "rb") as fh:
                fh.seek(off)
                data = fh.read()
        except OSError:
            return
        pos = 0
        n = len(data)
        if off == 0:
            if n < 24 or struct.unpack("<I", data[:4])[0] != _PCAP_MAGIC:
                return
            pos = 24
            if n >= 40:
                self._first_rec[path] = bytes(data[:40])
        while pos + 16 <= n:
            ts, tus, incl, _ = struct.unpack("<IIII", data[pos:pos + 16])
            if pos + 16 + incl > n:
                break                                         # registro incompleto
            pkt = data[pos + 16:pos + 16 + incl]
            pos += 16 + incl
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
        self._offsets[path] = off + pos

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
            ts = self._traj_t[st]
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
                        "lon": round(lon, 6), "lat": round(lat, 6),
                        "angle": round(heading, 1), "speed": round(speed, 2),
                        "type": "emergency" if emerg else "passenger",
                        "len": 6.5 if emerg else 4.5,
                        "wid": 2.0 if emerg else 1.8,
                        "edge": ""})
        return out

    # ------------------------------------------ potencia RX (signal*.csv, opc.)
    def _load_signal(self, new_rx: list) -> None:
        """Ingesta opcional (e incremental) de potencia de recepción por
        mensaje, volcada por la app de VaN3Twin (callbacks extendidos con
        SignalInfo) a un CSV en la misma carpeta que los pcap. Formato
        (cabecera, orden libre): ``rx,tx,t_ms,rssi[,snr]`` — rx/tx = stationID
        (admite "veh5"), t en ms de simulación (o s: se detecta), rssi en dBm.
        Solo los eventos RX nuevos se asocian a su muestra (±50 ms)."""
        import csv
        import io
        rows_all: list[tuple] = []
        for path in sorted(glob.glob(os.path.join(self.pcap_dir, "signal*.csv"))):
            off = self._sig_offsets.get(path, 0)
            try:
                with open(path, "rb") as fh:
                    if off == 0:
                        header = fh.readline()
                        off = len(header)
                        self._sig_header = header.decode("utf-8", "replace")
                    fh.seek(off)
                    chunk = fh.read()
            except OSError:
                continue
            if not chunk:
                continue
            nl = chunk.rfind(b"\n")             # solo líneas completas
            if nl < 0:
                continue
            chunk = chunk[:nl + 1]
            self._sig_offsets[path] = off + len(chunk)
            header = getattr(self, "_sig_header", "")
            try:
                reader = csv.DictReader(io.StringIO(header + chunk.decode("utf-8",
                                                                          "replace")))
                for row in reader:
                    r = {k.strip().lower(): (v or "").strip()
                         for k, v in row.items() if k}

                    def num(*names):
                        for n in names:
                            if n in r and r[n] != "":
                                return float(re.sub(r"[^0-9.+-]", "", r[n]) or "nan")
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
        if rows_all:
            if self._sig_ms is None:
                self._sig_ms = max(x[2] for x in rows_all) > 1e4   # heurística ms vs s
            touched = set()
            for tx, rx, t, rssi, snr in rows_all:
                k = (tx, rx)
                self._sig.setdefault(k, []).append((t / 1000.0 if self._sig_ms else t,
                                                    rssi, snr))
                touched.add(k)
            for k in touched:
                self._sig[k].sort()
                self._sig_t[k] = [s[0] for s in self._sig[k]]
        if not self._sig:
            return
        for e in new_rx:                        # asociar RX a la muestra más cercana
            k = (e["tx"], e["rx"])
            seq = self._sig.get(k)
            if not seq:
                continue
            ts = self._sig_t[k]
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
        ts = self._traj_t[st]
        i = bisect_left(ts, t)
        if i == 0:
            return (pts[0][1], pts[0][2]) if t > pts[0][0] - 2.0 else None
        if i >= len(pts):
            return (pts[-1][1], pts[-1][2]) if t < pts[-1][0] + 2.0 else None
        a, b = pts[i - 1], pts[i]
        f = (t - a[0]) / (b[0] - a[0]) if b[0] > a[0] else 0.0
        return (a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)

    def phy_stats(self, force: bool = False) -> dict:
        """Métricas PHY/MAC derivadas de los pcap (sin radiotap no hay potencia
        RX por trama; el resto se mide de eventos y trayectorias reales).
        Coste O((TX+RX) log TX): índices por estación precomputados."""
        if self._phy and not force:
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
        by_st_t = {st: [e["t"] for e in seq] for st, seq in by_st.items()}
        lat, dist = [], []
        for r in self.rx:                       # emparejar RX con su TX
            seq = by_st.get(r["tx"])
            if not seq:
                continue
            i = bisect_right(by_st_t[r["tx"]], r["t"])
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

        # RX esperadas: por cada TX, estaciones coexistentes (traj) menos el
        # emisor. Equivalente a Σ_estación (nº de TX ajenos durante su vida),
        # contado con bisect sobre la lista global de tiempos TX.
        expected = 0
        for st, pts in self.traj.items():
            a, b = pts[0][0] - 2.0, pts[-1][0] + 2.0
            in_life = bisect_right(self._tx_t, b) - bisect_left(self._tx_t, a)
            own = by_st_t.get(st)
            own_in = (bisect_right(own, b) - bisect_left(own, a)) if own else 0
            expected += max(in_life - own_in, 0)
        # en vivo los RX almacenados están diezmados: la ventana de TX/traj
        # también, así que el PER se estima con los recuentos exactos totales
        got_rx = self.rx_total if self.live else len(self.rx)
        if self.live:
            expected = int(expected * self.rx_total / max(len(self.rx) * self._rx_keep, 1)
                           ) if self.rx else expected
            expected = max(expected, got_rx)
        per = (1.0 - got_rx / expected) if expected else None

        # utilización de canal: preámbulo 40 µs + (payload + ~80 B de cabeceras)
        # a 12 Mbit/s, sobre la duración de la ventana
        dur = max(self.t1 - (self.tx[0]["t"] if self.tx else self.t0), 1e-6)
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
            "got_rx": got_rx,
            "pdr_best": {best_pair: pdr[best_pair]} if best_pair else {},
            "pdr_worst": {worst_pair: pdr[worst_pair]} if worst_pair else {},
            "rates_per_s": {k.replace("tx_", ""): round(v / max(self.t1 - self.t0, 1e-6), 2)
                            for k, v in self.stats["counts"].items()
                            if k.startswith("tx_")},
            "channel_util": round(air / dur, 4),
            "frames": {"tx": n_tx, "rx": got_rx},
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

    def _tx_near(self, station: int, t: float, tol: float) -> list[dict]:
        i0 = bisect_left(self._tx_t, t - tol)
        i1 = bisect_right(self._tx_t, t + tol)
        return [e for e in self.tx[i0:i1] if e["tx"] == station]

    def _rx_near(self, station: int, t: float, tol: float) -> list[dict]:
        i0 = bisect_left(self._rx_t, t - tol)
        i1 = bisect_right(self._rx_t, t + tol)
        return [r for r in self.rx[i0:i1] if r["tx"] == station]

    def decode(self, station: int, t: float, mtype_hint: str | None = None) -> dict:
        """Contenido decodificado del mensaje TX de `station` más cercano a t.

        Tolerante en tiempo (±20 ms): un clic sobre un ARCO llega con el
        instante de RECEPCIÓN, unos µs después del TX. `mtype_hint` (CAM/CPM/
        DENM) desambigua si la estación transmitió dos tipos muy seguidos.
        """
        cand = [e for e in self._tx_near(station, t, 0.02)
                if mtype_hint is None or e["type"] == mtype_hint]
        ev = min(cand, key=lambda e: abs(e["t"] - t)) if cand else None
        entry = (self._payloads.get((station, round(ev["t"] * 1e6)))
                 if ev else None)
        if entry is None or ev is None:
            return {"error": "mensaje no encontrado", "tx": station,
                    "mtype": mtype_hint or "?", "t": round(t, 6)}
        payload, meta = entry
        t = ev["t"]
        mtype = ev["type"]
        # los RX se registran hasta ~16 ms tras el TX (cola + backoff
        # 802.11p en ns-3): ventana de 30 ms para no perder receptores
        rxs = self._rx_near(station, t, 3e-2)
        p_tx = self._pos_at(station, t)
        by_rx = {r["rx"]: r for r in rxs}
        # OJO: la clave del tipo de mensaje se llama "mtype" (no "type") para
        # no pisar el "type":"msg_detail" del sobre WebSocket al hacer **det.
        out = {"t": round(t, 6), "tx": station, "mtype": mtype,
               "bytes": len(payload),
               "receivers": sorted(by_rx),
               # por receptor: distancia real (trayectorias) y RSSI si hay CSV
               "receivers_info": [
                   {"rx": r["rx"],
                    "dist_m": (round(_meters(p_tx, p_rx))
                               if p_tx and (p_rx := self._pos_at(r["rx"], r["t"]))
                               else None),
                    "rssi": r.get("rssi"), "snr": r.get("snr")}
                   for r in sorted(by_rx.values(), key=lambda x: x["rx"])],
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
