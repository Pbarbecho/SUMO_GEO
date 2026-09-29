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

Rendimiento (2026-09-29)
------------------------
* El frame se serializa UNA vez (orjson) y se manda el mismo texto a todos.
* Cada visor tiene su propia tarea de envío con una cola de UN frame: si un
  navegador va lento (pestaña en segundo plano, portátil justo), se descarta
  su frame atrasado y recibe el siguiente. Un visor lento ya no frena el
  lockstep con ns-3 ni a los demás visores (*backpressure*).
* Protocolo v2 compacto (ver ``frames.py``): un visor que se conecta a mitad
  de corrida recibe un *snapshot* con la flota completa y a partir de ahí
  solo deltas.
"""
from __future__ import annotations

import asyncio
import traceback

from fastapi import WebSocket, WebSocketDisconnect

from .config import settings
from .frames import FrameBuilder, encode
from .sumo_bridge import SumoBridge


_FRAME = object()          # marcador en la cola: "manda el último frame"


class Viewer:
    """Un navegador suscrito. Los mensajes de control van en orden por una
    cola sin límite; los frames se COALESCEN: solo se conserva el más reciente
    y en la cola hay como mucho un marcador pendiente. Un visor lento recibe
    menos frames (nunca frames viejos) y no frena al hub ni a los demás."""

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue()
        self.frame: str | None = None            # último frame no enviado
        self.dropped = 0
        self.task: asyncio.Task | None = None

    def push(self, text: str, coalesce: bool = True) -> None:
        """Encolar sin bloquear jamás."""
        if not coalesce:
            self.queue.put_nowait(text)
            return
        if self.frame is not None:               # había uno sin enviar: se sustituye
            self.dropped += 1
            self.frame = text
            return
        self.frame = text
        self.queue.put_nowait(_FRAME)

    async def sender(self) -> None:
        try:
            while True:
                item = await self.queue.get()
                if item is _FRAME:
                    text, self.frame = self.frame, None
                    if text is None:
                        continue
                else:
                    text = item
                await self.ws.send_text(text)
        except Exception:
            return


class LiveHub:
    def __init__(self, state: dict, live_rep_nowait):
        self._state = state                 # _state de main: netgeo, meta
        self._live_rep = live_rep_nowait    # índice V2X en vivo (nunca bloquea)
        self.viewers: dict[WebSocket, Viewer] = {}
        self.status = "waiting"             # waiting | running | ended
        self.detail = ""                    # texto legible para el visor
        self.paused = False
        self.period = 1.0 / max(settings.max_fps, 0.1)
        self.bridge: SumoBridge | None = None
        self.builder = FrameBuilder(state["netgeo"], settings.los_every)
        self.last_frame: dict | None = None
        self.t = 0.0
        self._lock = asyncio.Lock()         # un solo comando TraCI a la vez
        self._task: asyncio.Task | None = None
        self._sent_v2x: set = set()
        self.frame_ms = 0.0                 # coste medio del último frame (diagnóstico)

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
            self.builder.reset()
            self.last_frame = None
            self.t = 0.0
            self.paused = False        # cada corrida arranca en marcha
            print(f"[live_hub] conectado a SUMO (orden {settings.sumo_order})",
                  flush=True)
            await self._set_status("running", "")
            return

    async def _stream(self) -> None:
        import time as _time
        loop = asyncio.get_running_loop()
        while True:
            if self.paused or (settings.hold_without_viewers and not self.viewers):
                await asyncio.sleep(0.05)
                continue
            t0 = loop.time()
            try:
                async with self._lock:
                    frame, done = await asyncio.to_thread(self._collect)
            except Exception as exc:  # noqa: BLE001 - SUMO se fue (fin de corrida)
                print(f"[live_hub] conexión TraCI perdida: {exc}", flush=True)
                await self._finish()
                return
            self._attach_live_v2x(frame)
            self.last_frame = frame
            self._broadcast_frame(frame)
            self.frame_ms = 0.8 * self.frame_ms + 0.2 * 1000 * (loop.time() - t0)
            if done:
                await self._finish()
                return
            # cadencia: descontar lo que ya tardó el frame (antes se sumaban
            # ambos y a 10 fps pedidos salían ~8 reales)
            await asyncio.sleep(max(self.period - (loop.time() - t0), 0.0))

    def _collect(self):
        """Corre en un hilo: un paso de lockstep + lectura del estado (bloquea
        en el socket mientras ns-3 calcula su parte). Round-trips por frame:
        simulationStep (trae flota + semáforos + variables de simulación)."""
        b = self.bridge
        t = b.step()
        vehs = b.vehicles(self._state["netgeo"])
        frame = self.builder.build(t, vehs, b.frame_stats(), b.edge_agg,
                                   b.trafficlights_if_changed(),
                                   with_station=bool(settings.live_pcap_dir))
        self.t = t
        return frame, b.min_expected_number() <= 0

    def _attach_live_v2x(self, frame: dict) -> None:
        """Mensajes V2X en vivo (pcaps que ns-3 escribe durante la corrida)."""
        if not settings.live_pcap_dir:
            return
        t = self.t
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
        self._broadcast({"type": "end", "t": round(self.t, 1)})
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
                "t": round(self.t, 1), "frame_ms": round(self.frame_ms, 1),
                "dropped": sum(v.dropped for v in self.viewers.values())}

    async def _set_status(self, status: str, detail: str) -> None:
        if (status, detail) == (self.status, self.detail):
            return
        self.status, self.detail = status, detail
        self._broadcast(self.status_msg())

    def _broadcast(self, msg: dict) -> None:
        text = encode(msg)
        for v in list(self.viewers.values()):
            v.push(text, coalesce=False)

    def _broadcast_frame(self, frame: dict) -> None:
        text = encode(frame)                  # UNA serialización para todos
        for v in list(self.viewers.values()):
            v.push(text, coalesce=True)

    async def serve(self, ws: WebSocket) -> None:
        """Un visor: se suscribe a los frames y manda órdenes globales."""
        viewer = Viewer(ws)
        viewer.task = asyncio.create_task(viewer.sender())
        self.viewers[ws] = viewer
        try:
            viewer.push(encode({"type": "meta", **self._state["meta"], "proto": 2}),
                        coalesce=False)
            viewer.push(encode(self.status_msg()), coalesce=False)
            if self.last_frame is not None:        # pintar al instante tras recargar
                viewer.push(encode(self.builder.snapshot(self.last_frame)),
                            coalesce=False)
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
                    self._broadcast(self.status_msg())
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
                    viewer.push(encode({"type": "inspect", **details}), coalesce=False)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        finally:
            self.viewers.pop(ws, None)
            if viewer.task:
                viewer.task.cancel()
            # el hold (sin visores) se reevalúa solo en el bucle de _stream
