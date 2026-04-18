# CHEQ-OK · Prototipo de evaluación de cheques

Aplicación que consulta el BCRA para recomendar si conviene o no recibir un cheque, según el CUIT del librador.

Este paquete contiene:

- **`backend/`** — el servidor que consulta el BCRA y aplica las reglas de decisión. Hecho en Python con FastAPI.
- **`web/`** — una página de prueba para usarlo desde el navegador (no es la app Android final, es para validar que la lógica funcione antes de construir la app).

Este prototipo valida end-to-end: foto → OCR del CUIT (paso manual todavía, ingresás el CUIT a mano) → consulta BCRA → semáforo. Una vez que esto funcione, el siguiente paso es sumar la parte OCR y empaquetarlo como app Android.

---

## Cómo correrlo (guía paso a paso)

### 1. Instalar Python

Si no tenés Python en tu computadora:

- **Windows / Mac**: descargarlo desde https://www.python.org/downloads/ (versión 3.10 o superior).
- Durante la instalación en Windows, marcá la opción **"Add Python to PATH"**.

Para verificar que funcionó, abrí una terminal y escribí:
```
python3 --version
```
Tiene que mostrar algo como `Python 3.11.x`.

### 2. Instalar las dependencias del backend

Abrí una terminal, andá a la carpeta `backend/` y ejecutá:

```
pip install fastapi uvicorn httpx
```

(en algunas Mac puede ser `pip3` en vez de `pip`)

### 3. Arrancar el backend

En la misma terminal, parado en la carpeta `backend/`:

```
python3 main.py
```

Tenés que ver un mensaje como:
```
Uvicorn running on http://0.0.0.0:8000
```

Dejá esa terminal abierta. Mientras esté corriendo, el backend está escuchando.

### 4. Abrir la página web de prueba

Hay dos opciones:

**Opción A (la fácil)**: hacé doble click en el archivo `web/index.html`. Se abre en el navegador.

**Opción B (la correcta)**: abrí **otra** terminal (sin cerrar la del backend), andá a la carpeta `web/` y ejecutá:
```
python3 -m http.server 8080
```
Después andá al navegador y abrí http://localhost:8080

### 5. Probar

En la página web, ingresá un CUIT de los ejemplos (están como botones abajo del input), o cualquier otro CUIT de 11 dígitos, y apretá "Evaluar". Vas a ver el semáforo, los motivos y los datos crudos del BCRA.

---

## Cómo funciona (en criollo)

1. La página web toma el CUIT que ingresaste.
2. Se lo manda al backend (`/evaluar/{CUIT}`).
3. El backend hace dos consultas al BCRA:
   - Deudas y situación crediticia.
   - Cheques rechazados.
4. Aplica las reglas que definimos:
   - **ROJO** si hay cheques rechazados pendientes de pago.
   - **ROJO** si hay cheques rechazados (aunque estén pagados) de los últimos 12 meses.
   - **ROJO** si hay situación 3, 4, 5 o 6 en cualquier entidad.
   - **ROJO** si hay situación 2 con deuda > $500.000.
   - **VERDE** si no figura en el BCRA (sin antecedentes).
   - **VERDE** en cualquier otro caso.
5. Devuelve el semáforo + los motivos + los datos crudos para que puedas ver el detalle.

## Cambiar los umbrales

Los parámetros (umbral de $500.000 y ventana de 12 meses) son configurables:

- Desde la página web: clickeá en "Configuración de reglas" y modificalos.
- Desde terminal: editá el archivo `backend/config.json` que se genera automáticamente.

## Documentación de la API

Mientras el backend esté corriendo, abrí en el navegador:

- http://localhost:8000/docs — documentación interactiva donde podés probar cada endpoint.
- http://localhost:8000/ — info general del servicio.

---

## Qué sigue

Próximos pasos del proyecto:

1. **OCR del cheque** — integrar Google ML Kit para leer automáticamente el CUIT desde la foto. Hoy se ingresa a mano.
2. **App Android** — empaquetar todo como app móvil (probablemente con Flutter para poder tener iOS después).
3. **Lectura de la línea CMC-7** (los números raros al pie del cheque) como fuente complementaria al OCR de texto.
4. **Histórico local** — guardar las evaluaciones pasadas en el celular para poder revisarlas.
5. **Despliegue del backend** — hoy corre en tu compu. Para que la app funcione fuera de tu red, hay que ponerlo en algún servicio cloud (Render, Railway, Fly.io — todos tienen plan gratuito para empezar).

---

## Archivos del proyecto

```
cheqok/
├── backend/
│   ├── main.py                 # servidor FastAPI con los endpoints
│   ├── config.py               # manejo de parámetros configurables
│   ├── config.json             # se crea solo la primera vez
│   ├── test_decision.py        # tests automáticos con pytest
│   └── validar_logica.py       # script de validación sin dependencias
└── web/
    └── index.html              # página de prueba
```

## Validación

Corrimos 12 escenarios de prueba sobre la lógica de decisión y todos pasaron:

- Sin antecedentes → VERDE ✓
- Situación 1 → VERDE ✓
- Situación 2 bajo umbral → VERDE ✓
- Situación 2 sobre umbral → ROJO ✓
- Situación 3/4/5/6 → ROJO ✓
- Cheque pendiente → ROJO ✓
- Cheque pagado reciente → ROJO ✓
- Cheque pagado viejo → VERDE ✓
- Combinaciones varias → ROJO cuando corresponde ✓

Para correr los tests vos mismo: `python3 validar_logica.py` dentro de `backend/`.
