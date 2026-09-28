"""Conexión TraCI PERSISTENTE al SUMO de VaN3Twin (modo ``remote`` multi-cliente).

Por qué existe
--------------
Antes cada WebSocket abría su propio cliente TraCI y lo cerraba al desconectar.
Con ``--num-clients N`` SUMO acepta exactamente N sockets al arrancar y después
CIERRA el puerto de escucha, así que al recargar el navegador:

* el backend cerraba el cliente 2 -> ns-3 seguía solo y a toda velocidad;
* el WebSocket nuevo no podía volver a entrar ("Connection refused"), aunque
  probara los puertos 3400-3410: ya no escucha ninguno;
* y si el socket moría de golpe (reinicio del backend), SUMO abortaba la
  corrida entera ("peer shutdown").

Verificado empíricamente con SUMO 1.12 (2026-09-28). No es el puerto "aleatorio"
(eso ya lo cubre ``sumo_port_scan``): es el ciclo de vida de la conexión.

Qué hace
--------
El backend mantiene UNA conexión (el *hub*) durante toda la corrida:

* una tarea de fondo se conecta a SUMO reintentando hasta que ns-3 lo lance,
  declara ``setOrder(N)`` y avanza en lockstep con ns-3;
* cada frame se difunde a todos los visores conectados; recargar la página o
  abrir varias pestañas solo (des)suscribe visores, la conexión TraCI no se toca;
* play/pausa/velocidad son globales (estado de la corrida, no de la pestaña);
* sin visores conectados la corrida queda en espera (``hold_without_viewers``)
  para que ns-3 no avance sin que nadie lo vea;
* cuando SUMO termina (fin de la corrida o ``--quit-on-end``) el hub vuelve a
  esperar la siguiente: se puede relanzar ``./ns3 run`` sin reiniciar nada.
"""
from __future__ import annotations

import asyncio
import re
import traceback
from collections import Counter

from fastapi import WebSocket, WebSocketDisconnect

from .config import settings
from .sumo_bridge import SumoBridge
from .traffic import edge_estimation


class LiveHub:
    def __init__(self, state: dict, live_rep_nowait):
        self._state = state                 # _state de main: netgeo, meta
        self._live_rep = live_rep_nowait    # índice V2X en vivo (nunca bloquea)
        self.viewers: set[WebSocket] = set()
        self.status = "waiting"             # waiting | running | ended
        self.detail = ""                    # texto legible para el visor
        self.paused = False
        self.period = 1.0 / max(settings.max_fps, 0.1)
        self.bridge: SumoBridge | None = None
        self.last_frame: dict | None = None
        self.t = 0.0
        self._lock = asyncio.Lock()         # un solo comando TraCI a la vez
        self._task: asyncio.Task | None = None
        self._sent_v2x: set = set()

    # ------------------------------------------------------------------ ciclo
    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="live-hub")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._close_bridge()

    async def _run(self) -> None:
        while True:
            await self._attach()
            await self._stream()

    def _ports(self) -> str:
        p0 = settings.sumo_port
        p1 = p0 + max(settings.sumo_port_scan, 0)
        return f"{p0}" if p1 == p0 else f"{p0}-{p1}"

    async def _attach(self) -> None:
        """Reintenta hasta que SUMO acepte la conexión (ns-3 lo lanza él)."""
        logged = False
        while True:
            bridge = SumoBridge()
            try:
                await asyncio.to_thread(bridge.start)
            except Exception as exc:  # noqa: BLE001 - SUMO aún no existe
                await self._set_status(
                    "waiting",
                    f"esperando a SUMO en {settings.sumo_host}:{self._ports()} "
                    f"(lanza ./ns3 run ... --num-traci-clients=2)")
                if not logged:
                    print(f"[live_hub] SUMO no disponible ({exc}); reintentando "
                          f"cada {settings.sumo_retry_s}s", flush=True)
                    logged = True
                await asyncio.sleep(settings.sumo_retry_s)
                continue
            self.bridge = bridge
            self._sent_v2x = set()
            self.last_frame = None
            self.t = 0.0
            self.paused = False        # cada corrida arranca en marcha
            print(f"[live_hub] conectado a SUMO (orden {settings.sumo_order})",
                  flush=True)
            await self._set_status("running", "")
            return

    async def _stream(self) -> None:
        netgeo = self._state["netgeo"]
        while True:
            if self.paused or (settings.hold_without_viewers and not self.viewers):
                await asyncio.sleep(0.05)
                continue
            try:
                async with self._lock:
                    frame, done = await asyncio.to_thread(self._collect, netgeo)
            except Exception as exc:  # noqa: BLE001 - SUMO se fue (fin de corrida)
                print(f"[live_hub] conexión TraCI perdida: {exc}", flush=True)
                await self._finish()
                return
            self._attach_live_v2x(frame)
            self.last_frame = frame
            await self._broadcast(frame)
            if done:
                await self._finish()
                return
            await asyncio.sleep(self.period)

    def _collect(self, netgeo):
        """Corre en un hilo: un paso de lockstep + lectura del estado (bloquea
        en el socket mientras ns-3 calcula su parte)."""
        b = self.bridge
        t = b.step()
        vehs = b.vehicles(netgeo)
        stats = b.frame_stats()
        stats["types"] = dict(Counter(v["type"] for v in vehs))
        frame = {
            "type": "frame",
            "t": round(t, 1),
            "vehicles": vehs,
            "edges": edge_estimation(b.conn, netgeo, (v["edge"] for v in vehs)),
            "tls": b.trafficlights(),
            "stats": stats,
        }
        self.t = t
        return frame, b.min_expected_number() <= 0

    def _attach_live_v2x(self, frame: dict) -> None:
        """Mensajes V2X en vivo (pcaps que ns-3 escribe durante la corrida)."""
        if not settings.live_pcap_dir:
            return
        t = self.t
        for v in frame["vehicles"]:
            m = re.search(r"(\d+)", v["id"])
            if m:
                v["station"] = int(m.group(1))
        rep = self._live_rep()
        if rep is None:
            return
        win = rep.window(t - 6.0, t)           # margen: ns-3 vuelca con retraso
        msgs = {"tx": [], "rx": []}
        for kind in ("tx", "rx"):
            for e in win[kind]:
                key = (kind, e["tx"], e.get("rx"), round(e["t"], 4), e["type"])
                if key in self._sent_v2x:
                    continue
                self._sent_v2x.add(key)
                msgs[kind].append(e)
        if len(self._sent_v2x) > 20000:
            self._sent_v2x = {k for k in self._sent_v2x if k[3] > t - 12.0}
        if msgs["tx"] or msgs["rx"]:
            frame["messages"] = msgs

    async def _finish(self) -> None:
        await self._broadcast({"type": "end", "t": round(self.t, 1)})
        self._close_bridge()
        self.paused = False
        await self._set_status("ended", "simulación finalizada: esperando la "
                               "siguiente corrida de ns-3")

    def _close_bridge(self) -> None:
        if self.bridge:
            try:
                self.bridge.close()
            except Exception:
                pass
            self.bridge = None

    # -------------------------------------------------------------- visores
    def status_msg(self) -> dict:
        return {"type": "status", "status": self.status, "detail": self.detail,
                "paused": self.paused, "viewers": len(self.viewers),
                "t": round(self.t, 1)}

    async def _set_status(self, status: str, detail: str) -> None:
        if (status, detail) == (self.status, self.detail):
            return
        self.status, self.detail = status, detail
        await self._broadcast(self.status_msg())

    async def _broadcast(self, msg: dict) -> None:
        dead = []
        for ws in list(self.viewers):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.viewers.discard(ws)

    async def serve(self, ws: WebSocket) -> None:
        """Un visor: se suscribe a los frames y manda órdenes globales."""
        self.viewers.add(ws)
        try:
            await ws.send_json({"type": "meta", **self._state["meta"]})
            await ws.send_json(self.status_msg())
            if self.last_frame is not None:        # pintar al instante tras recargar
                await ws.send_json(self.last_frame)
            while True:
                try:
                    cmd = await ws.receive_json()
                except (ValueError, TypeError):
                    continue
                action = cmd.get("cmd")
                if action in ("pause", "play"):
                    # solo con corrida activa: una orden tardía (la corrida acabó
                    # justo al pulsar) no debe dejar pausada la SIGUIENTE corrida.
                    # Se difunde el estado igualmente para re-sincronizar el botón.
                    if self.status == "running":
                        self.paused = action == "pause"
                    await self._broadcast(self.status_msg())
                elif action == "speed":
                    self.period = 1.0 / max(float(cmd.get("fps", settings.max_fps)), 0.1)
                elif action == "inspect":
                    vid = str(cmd.get("id", ""))
                    details = {"id": vid, "gone": True}
                    if self.bridge is not None:
                        try:
                            async with self._lock:
                                details = await asyncio.to_thread(
                                    self.bridge.vehicle_details, vid)
                        except Exception:
                            pass
                    await ws.send_json({"type": "inspect", **details})
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self.viewers.discard(ws)
            # el hold (sin visores) se reevalúa solo en el bucle de _stream
