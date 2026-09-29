"""Thin wrapper around a SUMO simulation via TraCI (or libsumo).

Modes (see :mod:`app.config`):

* ``managed`` - launch SUMO as a subprocess with :func:`traci.start`.
* ``remote``  - connect to a running SUMO TraCI server with :func:`traci.init`.

libsumo (in-process, faster, no socket) is used automatically when
``use_libsumo`` is set and the module is importable; it exposes the same API.

Coste por frame (lo que importa para escalar a cientos/miles de vehículos)
-------------------------------------------------------------------------
Cada llamada TraCI es un round-trip por socket (~0.1-0.5 ms en Docker). El
puente está pensado para que un frame cueste un número FIJO de round-trips,
independiente del tamaño de la flota, de la red y del nº de semáforos:

1. ``simulationStep``            avanza SUMO y trae, en la MISMA respuesta,
                                 todas las suscripciones:
   * flota completa (posición/rumbo/velocidad/tipo/arista/CO2/espera) vía
     una suscripción de CONTEXTO sobre un cruce con radio "infinito" -> los
     vehículos nuevos entran solos, sin ``subscribe`` por vehículo;
   * variables de simulación (tiempo, salidas, llegadas, mínimo esperado);
   * estado de TODOS los semáforos (una suscripción por TLS al arrancar).
2. nada más. La estimación LOS por arista se calcula a partir de la flota
   (ver :func:`app.traffic.edge_estimation_from_fleet`): 0 llamadas TraCI.

Antes: ~3 llamadas por arista activa + 1 por semáforo + 5 de bookkeeping por
frame (con 600 vehículos, ~700 round-trips y ~55 ms por frame).
"""
from __future__ import annotations

import itertools
import re

import traci.constants as tc

from .config import settings

_CONN_COUNTER = itertools.count()   # unique traci connection labels

# Per-vehicle variables streamed via TraCI subscriptions: the whole fleet arrives
# in ONE round trip per step instead of ~6 socket calls per vehicle.
_SUB_VARS = (tc.VAR_POSITION, tc.VAR_ANGLE, tc.VAR_SPEED, tc.VAR_TYPE,
             tc.VAR_ROAD_ID, tc.VAR_CO2EMISSION, tc.VAR_WAITING_TIME)
_SIM_VARS = (tc.VAR_TIME, tc.VAR_DEPARTED_VEHICLES_IDS,
             tc.VAR_ARRIVED_VEHICLES_IDS, tc.VAR_MIN_EXPECTED_VEHICLES)
_CTX_RANGE = 1.0e8          # m: "toda la red" (SUMO filtra por distancia al cruce)
_STATION_RE = re.compile(r"(\d+)")


def station_of(vid: str) -> int | None:
    """stationID ETSI = nº del id SUMO (veh12 -> 12), como en el replay."""
    m = _STATION_RE.search(vid)
    return int(m.group(1)) if m else None


class SumoBridge:
    def __init__(self):
        self.conn = None          # the (labeled) traci connection or libsumo module
        self.label = None
        self.running = False
        self._libsumo = False
        self._dims: dict = {}     # typeID -> (length, width) metres, cached
        # aggregates for the historical panel
        self._depart_t: dict = {}                 # vid -> departure sim-time
        from collections import deque
        self._tt = deque(maxlen=300)              # travel times of recent arrivals (s)
        self._arrived_total = 0
        self.last_co2 = 0.0                       # fleet total, mg/s (from subscriptions)
        self.last_wait_mean = 0.0                 # mean waiting of stopped vehicles (s)
        self.last_wait_n = 0                      # how many vehicles are waiting
        # subscription bookkeeping
        self._ctx_junction: str | None = None     # junction that anchors the context sub
        self._sim_sub = False                     # simulation vars subscribed?
        self._tls_sub = False                     # traffic lights subscribed?
        self._t = 0.0                             # último tiempo conocido (s)
        self._min_expected = 1
        self.departed: list[str] = []             # ids salidos en el último step
        self.arrived: list[str] = []              # ids llegados en el último step
        self.edge_agg: dict = {}                  # edge -> [n, speed_sum, len_sum]
        self._tls_last: dict = {}                 # tid -> estado; para detectar cambios

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

    def start(self, config: str | None = None, begin: float | None = None) -> None:
        client = self._import_client()
        if settings.sumo_mode == "remote" and not self._libsumo:
            # Connect to an already-running SUMO server (your existing container).
            # Con sumo_port_scan > 0 se prueban también los puertos siguientes:
            # el TraCI de ns-3 (GetFreePort) corre el puerto si el base está
            # ocupado o en TIME_WAIT tras la corrida anterior.
            last_exc: Exception | None = None
            for port in range(settings.sumo_port,
                              settings.sumo_port + max(settings.sumo_port_scan, 0) + 1):
                # numRetries=0: UN intento por puerto, sin el sleep de 1 s ni las
                # líneas "Retrying..." de traci (el hub ya reintenta cada 2 s).
                # OJO: nunca "sondear" el puerto con un TCP connect+close: con
                # --num-clients N SUMO cuenta ese socket como cliente y al
                # cerrarse aborta la corrida ("peer shutdown") antes de que ns-3
                # llegue a conectarse.
                try:
                    client.init(host=settings.sumo_host, port=port, numRetries=0)
                    if port != settings.sumo_port:
                        print(f"[sumo_bridge] SUMO respondió en {port} "
                              f"(base {settings.sumo_port})", flush=True)
                    last_exc = None
                    break
                except Exception as exc:  # noqa: BLE001 - probar el siguiente puerto
                    last_exc = exc
            if last_exc is not None:
                raise last_exc
            if settings.sumo_order:
                # SUMO multi-cliente (--num-clients N): cada cliente debe declarar
                # su orden antes del primer simulationStep (VaN3Twin es el 1).
                client.setOrder(settings.sumo_order)
            self.conn = client
        else:
            from sumolib import checkBinary
            binary = checkBinary(settings.sumo_binary)
            args = [
                binary,
                "-c", config or settings.sumo_config,
                "--step-length", str(settings.step_length),
                "--start", "--quit-on-end",
            ]
            if begin is not None:
                args += ["--begin", str(begin)]
            if self._libsumo:
                client.start(args)
                self.conn = client
            else:
                # unique label so reconnecting WebSockets don't clash on traci's
                # single global 'default' connection
                self.label = f"ws{next(_CONN_COUNTER)}"
                client.start(args, label=self.label)
                self.conn = client.getConnection(self.label)
        self.running = True
        self._t = self.conn.simulation.getTime()
        self._subscribe_all()

    # ------------------------------------------------------------ suscripciones
    def _subscribe_all(self) -> None:
        conn = self.conn
        # 1) flota completa: contexto sobre un cruce con radio "infinito"
        try:
            jids = conn.junction.getIDList()
            if jids:
                jid = jids[0]
                conn.junction.subscribeContext(jid, tc.CMD_GET_VEHICLE_VARIABLE,
                                               _CTX_RANGE, _SUB_VARS)
                self._ctx_junction = jid
        except Exception as exc:  # noqa: BLE001 - fallback: una suscripción por vehículo
            print(f"[sumo_bridge] sin suscripción de contexto ({exc}); "
                  f"usando suscripción por vehículo", flush=True)
            self._ctx_junction = None
        if self._ctx_junction is None:
            try:                                # vehicles already in the run
                for vid in conn.vehicle.getIDList():
                    conn.vehicle.subscribe(vid, _SUB_VARS)
            except Exception:
                pass
        # 2) variables de simulación (tiempo, salidas, llegadas, mín. esperado)
        try:
            conn.simulation.subscribe(_SIM_VARS)
            self._sim_sub = True
        except Exception:
            self._sim_sub = False
        # 3) semáforos: estado de todos en cada step
        try:
            for tid in conn.trafficlight.getIDList():
                conn.trafficlight.subscribe(tid, (tc.TL_RED_YELLOW_GREEN_STATE,))
            self._tls_sub = True
        except Exception:
            self._tls_sub = False

    # ------------------------------------------------------------------- paso
    def step(self) -> float:
        conn = self.conn
        if settings.sumo_mode == "remote" and settings.sumo_order:
            # Multi-cliente con ns-3 (VaN3Twin): pedir un objetivo ABSOLUTO grueso
            # (t_actual + step_length). ns-3 avanza con sus pasos finos y marca el
            # ritmo; si pidiéramos "un paso" por frame, ns-3 quedaría esclavo del
            # visor (validado empíricamente — ver GUIA_INTEGRACION_SUMO_GEO.md).
            conn.simulationStep(self._t + settings.step_length)
        else:
            conn.simulationStep()
        sim = None
        if self._sim_sub:
            try:
                sim = conn.simulation.getSubscriptionResults() or None
            except Exception:
                sim = None
        if sim and tc.VAR_TIME in sim:
            now = float(sim[tc.VAR_TIME])
            self.departed = list(sim.get(tc.VAR_DEPARTED_VEHICLES_IDS, ()))
            self.arrived = list(sim.get(tc.VAR_ARRIVED_VEHICLES_IDS, ()))
            self._min_expected = int(sim.get(tc.VAR_MIN_EXPECTED_VEHICLES, 1))
        else:                                   # sin suscripción: 4 round-trips
            now = conn.simulation.getTime()
            try:
                self.departed = list(conn.simulation.getDepartedIDList())
                self.arrived = list(conn.simulation.getArrivedIDList())
                self._min_expected = conn.simulation.getMinExpectedNumber()
            except Exception:
                self.departed, self.arrived = [], []
        self._t = now
        try:                                    # keep the subscription set complete
            for vid in self.departed:
                if self._ctx_junction is None:
                    conn.vehicle.subscribe(vid, _SUB_VARS)
                self._depart_t[vid] = now       # for travel-time stats
            for vid in self.arrived:
                t0 = self._depart_t.pop(vid, None)
                if t0 is not None:
                    self._tt.append(now - t0)
                    self._arrived_total += 1
        except Exception:
            pass
        return now

    def _type_dims(self, type_id: str) -> tuple[float, float]:
        """(length, width) in metres for a vType, queried once and cached so the
        frontend can draw each vehicle at its true footprint (car vs. bus)."""
        d = self._dims.get(type_id)
        if d is None:
            try:
                d = (round(self.conn.vehicletype.getLength(type_id), 2),
                     round(self.conn.vehicletype.getWidth(type_id), 2))
            except Exception:
                d = (4.5, 1.8)          # sensible passenger-car fallback
            self._dims[type_id] = d
        return d

    def _fleet_results(self) -> dict:
        conn = self.conn
        res = {}
        try:
            if self._ctx_junction is not None:
                res = conn.junction.getContextSubscriptionResults(self._ctx_junction) or {}
            else:
                res = conn.vehicle.getAllSubscriptionResults() or {}
        except Exception:
            res = {}
        return res

    def vehicles(self, netgeo) -> list[dict]:
        """Flota completa como lista de dicts (una entrada por vehículo).

        Además deja en ``self.edge_agg`` la agregación por arista
        (nº vehículos, suma de velocidades, suma de longitudes) que usa la
        estimación LOS sin más llamadas TraCI.
        """
        res = self._fleet_results()
        if not res and self.conn.vehicle.getIDCount() > 0:
            return self._vehicles_polled(netgeo)             # safety fallback
        ids = list(res.keys())
        xs, ys = [], []
        for vid in ids:
            x, y = res[vid][tc.VAR_POSITION]
            xs.append(x)
            ys.append(y)
        lons, lats = netgeo.xy_to_lonlat_many(xs, ys)        # 1 llamada pyproj
        out = []
        co2_total = 0.0
        wait_sum, wait_n = 0.0, 0
        agg: dict = {}
        for i, vid in enumerate(ids):
            r = res[vid]
            vtype = r[tc.VAR_TYPE]
            length, width = self._type_dims(vtype)
            speed = r[tc.VAR_SPEED]
            edge = r.get(tc.VAR_ROAD_ID, "")
            co2_total += r.get(tc.VAR_CO2EMISSION, 0.0)
            w = r.get(tc.VAR_WAITING_TIME, 0.0)
            if w > 0:                            # stopped (mostly at signals/queues)
                wait_sum += w
                wait_n += 1
            if edge and not edge.startswith(":"):
                a = agg.get(edge)
                if a is None:
                    agg[edge] = [1, speed, length]
                else:
                    a[0] += 1
                    a[1] += speed
                    a[2] += length
            out.append({
                "id": vid,
                "lon": round(lons[i], 6),        # 1e-6 deg ≈ 11 cm: sobra para el 3D
                "lat": round(lats[i], 6),
                "angle": round(r[tc.VAR_ANGLE], 1),
                "speed": round(speed, 2),
                "type": vtype,
                "len": length,
                "wid": width,
                "edge": edge,
            })
        self.edge_agg = agg
        self.last_co2 = co2_total
        self.last_wait_mean = (wait_sum / wait_n) if wait_n else 0.0
        self.last_wait_n = wait_n
        return out

    def frame_stats(self) -> dict:
        """Aggregates for the historical panel (computed during vehicles())."""
        tt_mean = (sum(self._tt) / len(self._tt)) if self._tt else None
        return {
            "co2": round(self.last_co2 / 1000.0, 2),        # g/s fleet total
            "wait": round(self.last_wait_mean, 1),          # s, mean of waiting vehicles
            "wait_n": self.last_wait_n,                     # vehicles currently waiting
            "tt": round(tt_mean, 1) if tt_mean is not None else None,   # s, recent arrivals
            "arrived": self._arrived_total,
        }

    def _vehicles_polled(self, netgeo) -> list[dict]:
        """Old per-vehicle polling path (used only if subscriptions are empty)."""
        conn = self.conn
        out = []
        agg: dict = {}
        for vid in conn.vehicle.getIDList():
            x, y = conn.vehicle.getPosition(vid)
            lon, lat = netgeo.xy_to_lonlat(x, y)
            vtype = conn.vehicle.getTypeID(vid)
            length, width = self._type_dims(vtype)
            speed = conn.vehicle.getSpeed(vid)
            edge = conn.vehicle.getRoadID(vid)
            if edge and not edge.startswith(":"):
                a = agg.setdefault(edge, [0, 0.0, 0.0])
                a[0] += 1
                a[1] += speed
                a[2] += length
            out.append({
                "id": vid, "lon": round(lon, 6), "lat": round(lat, 6),
                "angle": round(conn.vehicle.getAngle(vid), 1),
                "speed": round(speed, 2),
                "type": vtype, "len": length, "wid": width,
                "edge": edge,
            })
        self.edge_agg = agg
        return out

    def vehicle_details(self, vid: str) -> dict:
        """Extended per-vehicle stats for the right-click inspector. Each query
        is guarded — not every measure exists in every SUMO build/vehicle."""
        v = self.conn.vehicle
        out: dict = {"id": vid}
        probes = {
            "co2": lambda: v.getCO2Emission(vid),              # mg/s
            "fuel": lambda: v.getFuelConsumption(vid),         # mg/s (ml/s in old SUMO)
            "noise": lambda: v.getNoiseEmission(vid),          # dB(A)
            "waiting": lambda: v.getWaitingTime(vid),          # s stopped (current)
            "waiting_acc": lambda: v.getAccumulatedWaitingTime(vid),
            "timeloss": lambda: v.getTimeLoss(vid),            # s lost vs. free flow
            "distance": lambda: v.getDistance(vid),            # m driven since depart
            "lane": lambda: v.getLaneID(vid),
            "edge": lambda: v.getRoadID(vid),
            "route_index": lambda: v.getRouteIndex(vid),
            "route_edges": lambda: len(v.getRoute(vid)),
        }
        for key, fn in probes.items():
            try:
                out[key] = fn()
            except Exception:
                pass
        out["gone"] = len(out) <= 2   # only id (+gone) -> vehicle left the run
        return out

    def trafficlights(self) -> dict:
        """Current signal-state string per traffic light (SUMO r/y/g/G/u/o codes).
        Con suscripción: 0 round-trips (viene con el simulationStep)."""
        conn = self.conn
        if self._tls_sub:
            try:
                res = conn.trafficlight.getAllSubscriptionResults() or {}
                out = {}
                for tid, r in res.items():
                    st = r.get(tc.TL_RED_YELLOW_GREEN_STATE)
                    if st is not None:
                        out[tid] = st
                if out or not res:
                    return out
            except Exception:
                pass
        return {tid: conn.trafficlight.getRedYellowGreenState(tid)
                for tid in conn.trafficlight.getIDList()}

    def trafficlights_if_changed(self) -> dict | None:
        """Estado de semáforos solo si cambió respecto al último frame (los
        estados cambian cada varios segundos: no tiene sentido mandarlos y
        recolorearlos 10 veces por segundo)."""
        cur = self.trafficlights()
        if cur == self._tls_last:
            return None
        self._tls_last = cur
        return cur

    @property
    def tls_state(self) -> dict:
        return self._tls_last

    def min_expected_number(self) -> int:
        """0 when no vehicles remain and none are scheduled -> simulation done."""
        if self._sim_sub:
            return self._min_expected
        return self.conn.simulation.getMinExpectedNumber()

    def close(self) -> None:
        if self.conn and self.running:
            try:
                self.conn.close()
            except Exception:
                pass
        self.running = False
