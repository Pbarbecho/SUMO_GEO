# Roadmap técnico — SUMO-GEO

Plan por fases para llevar el prototipo a una plataforma de gestión de
simulaciones de movilidad con SUMO, 3D en navegador y estimación de tráfico en
tiempo real. Cada fase entrega algo funcional y verificable.

> Estado actual (✅): el scaffolding cubre la **Fase 0** completa y la mayor
> parte de las Fases 1–3 con el escenario sintético de demostración.

## Fase 0 — Base ejecutable ✅
- [x] `docker compose up` levanta backend (SUMO+FastAPI) y frontend (nginx).
- [x] Escenario de ejemplo (rejilla 6×6, 400 vehículos, 25 edificios).
- [x] Puente TraCI validado (arranque, paso, cierre).
- **Hito:** demo reproducible sin datos externos.

## Fase 1 — Red y edificios en el navegador ✅ (demo)
- [x] `GET /api/network` → aristas GeoJSON; `GET /api/buildings` → polígonos con altura.
- [x] Georreferenciación: proyección real (OSM) o ancla ENU (redes sintéticas).
- [x] MapLibre `fill-extrusion` para edificios 3D; deck.gl para la red.
- [ ] **Ciudad real:** importar OSM/Overture (SUMO `netconvert`/`polyconvert` o GeoLibre) y validar alineación edificios↔calles.
- **Hito:** una zona real de Cuenca renderizada en 3D en el navegador.

## Fase 2 — Streaming en tiempo real ✅ (demo)
- [x] `WS /ws/live`: vehículos (lon/lat, ángulo, velocidad, tipo) por paso.
- [x] Controles: play/pausa/velocidad; contador y reloj de simulación.
- [ ] Reconexión automática y *backpressure* (descartar frames si el cliente va lento).
- [ ] Modo `remote` contra el contenedor SUMO existente del usuario.
- **Hito:** 60 min de simulación fluida a 10 fps con >500 vehículos.

## Fase 3 — Estimación de tráfico ✅ (base)
- [x] Densidad (veh/km/carril) → Nivel de Servicio (A–F) con rampa de color por arista.
- [ ] Métricas agregadas: flujo, tiempo de viaje, colas, tiempo perdido, emisiones (SUMO `emission`).
- [ ] Ventanas temporales y suavizado (medias móviles) para reducir parpadeo.
- [ ] Panel de series temporales (por arista/zona) y export CSV/GeoParquet.
- **Hito:** mapa de congestión creíble + KPIs exportables.

## Fase 4 — Escala y rendimiento
- [ ] Diezmado de vehículos y *culling* por viewport; `binaryType` / mensajes binarios (protobuf/flatbuffers) en el WS.
- [ ] libsumo (in-process) para pasos más rápidos; *subscriptions* de TraCI en vez de llamadas por vehículo.
- [ ] LOD de edificios; considerar 3D Tiles / CesiumJS si se necesita escala metropolitana global.
- [ ] Prueba de carga: 5k+ vehículos, varios clientes simultáneos.
- **Hito:** ciudad de tamaño medio a ≥15 fps en portátil estándar.

## Fase 5 — Gestión de simulaciones (producto)
- [ ] Multiescenario: subir/gestionar `.sumocfg`, redes y rutas; catálogo de escenarios.
- [ ] Sesiones aisladas por usuario; colas de ejecución; persistencia (Postgres/PostGIS).
- [ ] Autenticación y control de acceso; guardado/carga de proyectos.
- [ ] Editar demanda/semáforos desde la UI y relanzar.
- **Hito:** varios usuarios gestionan sus propias simulaciones.

## Fase 6 — Realismo e integraciones (opcional)
- [ ] Assets glTF de vehículos/edificios (autoría en Blender/BlenderGIS) cargados en deck.gl `ScenegraphLayer`.
- [ ] Sombras, iluminación por hora del día, texturas de fachada.
- [ ] Exportar clips cinemáticos con **sumo3Dviz** (render offline) para difusión/artículos.
- [ ] Integración como plugin de **GeoLibre** usando `app.getDeckGL()` para reusar su shell GIS.
- **Hito:** figuras/vídeos de alta calidad para publicación además del gemelo interactivo.

## Riesgos y decisiones
- **Georreferenciación:** preferir redes proyectadas (OSM) en producción; el ancla ENU es solo para demos sintéticas.
- **Volumen del WS:** el cuello de botella será el número de vehículos → mensajes binarios + *subscriptions* antes que optimizar el render.
- **“Solo navegador”:** descarta Unity WebGL / Unreal *pixel-streaming* a escala ciudad; si algún día se exige fotorrealismo de motor, aislarlo como export offline, no como runtime interactivo.
- **Fuente de edificios:** OSM footprints son irregulares; validar alturas (`building:levels`) y limpiar en GeoLibre.
