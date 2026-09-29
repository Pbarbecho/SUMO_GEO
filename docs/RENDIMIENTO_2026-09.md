# Rendimiento SUMO-GEO + VaN3Twin — revisión de septiembre de 2026

Objetivo: que el visor escale a **cientos de vehículos y mapas grandes** con el
menor uso posible de recursos en el navegador, y que el acoplamiento con ns-3
(VaN3Twin) no sea el cuello de botella. Cambios en tres repos:

| Repo | Commit | Qué cambia |
|---|---|---|
| `SUMO_GEO` | este | backend (puente TraCI, protocolo v2, hub con *backpressure*, índice pcap incremental), frontend (registro de flota, atributos binarios, red en MapLibre, modo ligero), nginx |
| `VaN3TwinGEO` (ns-3) | ver `RENDIMIENTO_SUMO_GEO.md` allí | suscripciones TraCI en `TraciClient`, VDP/sensor/MetricSupervisor sin round-trips por vehículo, `--pcap` |
| `van3twin-docker` / `RVH` | README | instrucciones de actualización y escalado |

## Medidas (backend)

Banco: `scripts/bench_backend.py` contra SUMO 1.27 real (modo *managed*,
red del centro de Cuenca, 705 aristas, 78 semáforos, demanda aleatoria densa,
`APP_STEP_LENGTH=0.5`). Media por frame; "antes" = camino v1 medido en la
misma corrida (`--legacy`).

| Flota | step (SUMO) | vehículos | LOS aristas | semáforos | serializar | **frame** | **bytes/frame** |
|---|---|---|---|---|---|---|---|
| 590 antes | 14.1 | 2.5 | 30.7 | 4.7 | 2.0 (json) | **53.6 ms** | **108 KB** |
| 590 después | 14.1 | 2.1 | 0 (en build) | 0.03 | 0.5 (orjson) | **16.8 ms** | **23 KB** |
| 1 540 antes | 36.6 | 5.4 | 36.1 | 5.1 | 4.6 | **86.6 ms** | **254 KB** |
| 1 540 después | 36.6 | 4.2 | 0 | 0.04 | 1.1 | **41.9 ms** | **60 KB** |

Lo que queda es el propio `simulationStep` de SUMO (en modo VaN3Twin es el
tiempo que tarda ns-3 en avanzar su parte del *lockstep*): el visor ya no añade
coste apreciable por vehículo, arista ni semáforo.

### Por qué era lento

1. **LOS por arista**: 3 llamadas TraCI (`getLastStepVehicleNumber/Occupancy/
   MeanSpeed`) por arista con tráfico y por frame → 500-600 round-trips por
   frame con 600 vehículos (30 ms). Ahora se calcula de la agregación de la
   flota que ya llega por suscripción: 0 llamadas.
2. **Semáforos**: `getIDList` + `getRedYellowGreenState` por TLS por frame
   (78 llamadas, ~5 ms; miles en la red metropolitana). Ahora: una suscripción
   por TLS al conectar (viene con el `simulationStep`) y solo se envían al
   visor cuando cambia alguna fase.
3. **Bookkeeping**: `getTime` ×2, `getDepartedIDList`, `getArrivedIDList`,
   `getMinExpectedNumber` por frame → una suscripción de simulación.
4. **Suscripción por vehículo** (`vehicle.subscribe` por cada salida) → una
   **suscripción de contexto** sobre un cruce con radio "infinito": la flota
   completa, incluidos los recién salidos, en un solo mensaje.
5. **Proyección**: `convertXY2LonLat` por vehículo (pyproj escalar) → una
   llamada vectorizada por frame.
6. **JSON**: dicts de 9 claves con floats de 15 dígitos por vehículo →
   filas planas con 6 decimales, atributos estáticos una sola vez, `orjson`.
7. **Difusión**: el hub serializaba y enviaba secuencialmente a cada visor;
   un navegador lento bloqueaba el *lockstep*. Ahora: una serialización, cola
   por visor con descarte del frame atrasado (`dropped` en `/api/health`).
8. **Índice pcap en vivo**: releía y reparseaba todos los pcaps enteros cada
   2 s (O(tamaño total), que crece ~N²). Ahora incremental por offset, ventana
   deslizante, diezmado adaptativo de RX almacenadas (recuentos exactos) y
   búsquedas por `bisect` (las stats PHY eran O(RX × TX_por_estación)).

## Frontend (navegador)

Validado en Chromium headless (SwiftShader) con 560 vehículos: sin errores,
5 grupos glTF instanciados, LOS por `feature-state` en ~165 aristas,
semáforos recoloreados 9 veces en 20 s (antes: 200), picking por índice.

- **Registro de flota mutado in-place**: antes cada tick de animación creaba
  un objeto nuevo por vehículo (`{...v}`) → 15 000 objetos/s con 500 vehículos
  y presión de GC. Ahora los objetos viven mientras el vehículo existe y la
  interpolación escribe `lon/lat/angle` en ellos.
- **Atributos binarios** (`data: {length, attributes}`): posiciones fp64 y
  matrices de modelo 3×4 en `Float64Array`/`Float32Array` preasignados por
  grupo de modelo; deck.gl los sube a la GPU sin invocar un *accessor* por
  instancia. El *picking* usa el índice de instancia.
- **Red vial en MapLibre** (`road-casing`, `road-surface`, `lane-lines`,
  anchura en metros con interpolación exponencial de zoom): MapLibre tesela
  en un *worker* y solo dibuja los tiles visibles; la congestión se aplica con
  `setFeatureState` a las ~200 aristas con tráfico. Antes, 3 `GeoJsonLayer`
  de deck.gl redibujaban las 34 k aristas completas en cada tick.
- **Cadencia**: 30 fps máximo (antes 60 con flotas pequeñas), 20 y 11 según
  flota; cada tick repinta toda la escena, así que es la palanca más grande.
- **Modo ligero** (`?lite=1` o casilla): `pixelRatio: 1` (4× menos fragmentos
  en pantallas retina), sin MSAA, edificios 3D apagados al inicio, `maxPitch`
  70°, animación un escalón más lenta.
- **nginx**: `Cache-Control: no-cache` + ETag (antes `no-store`: 660 KB de
  modelos glTF en cada recarga), gzip; el backend sirve `/api/network`
  pre-comprimido (56 KB → 9 KB en el centro; ~12 MB → ~2 MB en la red metro).

## ns-3 (VaN3TwinGEO) — dónde se iba el tiempo

Por cada intervalo de sincronización (`--sumo-updates`, 10-100 por segundo
simulado) y por cada vehículo, VaN3Twin hacía round-trips TraCI:

| Componente | Antes (por vehículo y por 100 ms) | Después |
|---|---|---|
| `TraciClient::UpdatePositions` | 1 `getPosition` (×N por intervalo) | 0: `VehicleSnapshot` desde la suscripción que llega con `simulationStep` |
| `CABasicService::checkCamConditions` | `getAngle`, `getDistance`, `getSpeed` + **2×`getPosition`+2×`convertXYtoLonLat` solo para cadenas de log** | 0 (instantánea); las cadenas solo si el log de disparo está activo |
| `VDPTraCI::getCAMMandatoryData` (por CAM) | 8 llamadas | 1 (`convertXYtoLonLat`, una vez por instantánea) |
| `SUMOSensor::updateDetectedObjects` | `getIDList` + **N×(`getPosition`+`convertXYtoLonLat`)** → **O(N²)** | 0 (distancias en metros SUMO sobre las instantáneas) |
| `MetricSupervisor::signalSentPacket` (por paquete TX, con `--met-sup`) | copia del mapa + **N×(`getPosition`+`convertXYtoLonLat`)** → **O(N²) por segundo** | 1 `convertLonLattoXY` por paquete + distancias locales |

Además, `--pcap=false` en el ejemplo EVA desactiva los pcap por nodo (su
volumen crece ~N²: son el principal coste de E/S con cientos de vehículos; el
visor los necesita solo para los mensajes V2X en vivo y el replay).

**Estos parches C++ no se han podido compilar en la nube** (no hay árbol ns-3):
hay que hacer `./ns3 build` en el contenedor `van3twin` y validar con el EVA
(ver `RENDIMIENTO_SUMO_GEO.md` en VaN3TwinGEO). El atributo
`ns3::TraciClient::UseSubscriptions=false` restaura el comportamiento anterior
sin recompilar.

## Cómo reproducir las medidas

```bash
pip install -r backend/requirements.txt          # incluye eclipse-sumo
python3 $SUMO_HOME/tools/randomTrips.py -n sumo/cuenca.net.xml -e 1200 -p 0.15 \
    --validate -r /tmp/bench.rou.xml --vehicle-class passenger
# cfg mínimo con esa demanda y la red, luego:
APP_SUMO_CONFIG=/tmp/bench.sumocfg APP_STEP_LENGTH=0.5 \
    python3 scripts/bench_backend.py --min-veh 500 --frames 60 --legacy
```
