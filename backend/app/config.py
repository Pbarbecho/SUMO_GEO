"""Application configuration.

All settings can be overridden with environment variables prefixed with ``APP_``
(e.g. ``APP_SUMO_MODE=remote``) or via a local ``.env`` file.
"""
from __future__ import annotations

import os

# Point PROJ at pyproj's bundled CRS database so SUMO/sumolib stop warning
# "pj_obj_create: Cannot find proj.db" when converting UTM <-> lon/lat. Harmless
# either way, but this silences the log noise. Must run before the net is loaded.
try:
    import pyproj
    _proj_dir = pyproj.datadir.get_data_dir()
    os.environ.setdefault("PROJ_DATA", _proj_dir)
    os.environ.setdefault("PROJ_LIB", _proj_dir)
except Exception:
    pass

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_", env_file=".env", extra="ignore"
    )

    # --- SUMO connection -----------------------------------------------------
    # "managed" -> the backend launches SUMO itself (SUMO must be installed in
    #              this container/host; the provided Dockerfile does that).
    # "remote"  -> connect to an already-running SUMO TraCI server (e.g. your
    #              existing SUMO Docker container started with --remote-port).
    sumo_mode: str = "managed"
    sumo_binary: str = "sumo"                 # "sumo" or "sumo-gui"
    sumo_config: str = "/sumo/demo.sumocfg"   # path to the .sumocfg (managed mode)
    sumo_host: str = "sumo"                    # TraCI host (remote mode)
    sumo_port: int = 8813                      # TraCI port (remote mode)
    sumo_order: int = 0        # >0 => TraCI multi-cliente: llamar setOrder(N) al conectar
    sumo_port_scan: int = 0    # nº de puertos extra a probar tras sumo_port (el TraCI de
                               # ns-3/VaN3Twin corre el puerto +1 si el anterior está en TIME_WAIT)

    # --- Replay offline (mensajes V2X desde .pcap de VaN3Twin) ---------------
    # El WS con ?replay=1 reproduce una corrida grabada: movilidad desde los
    # encabezados GeoNetworking y mensajes CAM/CPM/DENM con contenido ASN.1.
    replay_dir: str = "/replay"            # carpeta con v2v-*-<n>-0.pcap
    replay_pattern: str = "*.pcap"
    asn_dir: str = "/ns3/ns-3-dev/src/automotive/model/ASN1"
    use_libsumo: bool = False                  # use in-process libsumo if available
    step_length: float = 1.0                   # seconds per simulation step

    # Per-level scenarios (low/mid/high) selectable from the app. {level} is
    # substituted; the WebSocket picks the level from its ?level= query param.
    sumo_config_template: str = "/mapa/metro_{level}.sumocfg"
    sim_begin: float = 25200.0                 # level runs open at 07:00 (traffic already flowing)

    # Optional explicit file overrides (otherwise parsed from the .sumocfg)
    net_file: str | None = None
    poly_file: str | None = None

    # --- Geo-referencing -----------------------------------------------------
    # For synthetic networks without a projection, vehicle/edge/building local
    # (x, y) metres are anchored to this WGS84 origin with an ENU approximation.
    # Networks that carry a real projection (OSM imports) are converted with it.
    origin_lon: float = -79.0045               # Cuenca, Ecuador (demo anchor)
    origin_lat: float = -2.9006

    # Optional map view centre [lon, lat]. When set, the app opens here instead of
    # the network's geometric centre — handy when demand is concentrated in one
    # area (e.g. only the city centre of a large metropolitan network).
    view_lon: float | None = None
    view_lat: float | None = None

    # --- Streaming -----------------------------------------------------------
    max_fps: float = 10.0                       # WebSocket frame-rate cap
    cors_origins: str = "*"                     # comma-separated list or "*"


settings = Settings()
