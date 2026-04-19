"""CHEQ-OK · Backend v0.2.0 (paralelo + cache 24hs + anti rate-limit)"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import get_config, set_config

BCRA_BASE = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas"
HTTP_TIMEOUT = 45.0
HTTP_REINTENTOS = 4
CACHE_TTL_SEGUNDOS = 24 * 3600  # 24 horas (antes era 1 hora)

# Cache en memoria
_CACHE: dict[tuple, tuple[float, Optional[dict]]] = {}

# Control de rate limit: momento de la última consulta al BCRA
_ULTIMA_CONSULTA_BCRA = 0.0
_DELAY_ENTRE_CONSULTAS = 0.5  # 500ms mínimo entre consultas al BCRA
_LOCK_BCRA = asyncio.Lock()


app = FastAPI(title="CHEQ-OK API", version="0.2.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class MotivoDecision(BaseModel):
    categoria: str
    descripcion: str
    severidad: str


class Evaluacion(BaseModel):
    cuit: str
    denominacion: Optional[str] = None
    semaforo: str
    motivos: list[MotivoDecision]
    resumen: str
    detalle_deudas: Optional[dict] = None
    detalle_cheques_rechazados: Optional[dict] = None
    consultado_en: datetime
    config_aplicada: dict
    desde_cache: bool = False
    tiempo_ms: int = 0


def validar_cuit(cuit: str) -> str:
    limpio = "".join(c for c in cuit if c.isdigit())
    if len(limpio) != 11:
        raise HTTPException(400, f"CUIT debe tener 11 dígitos (recibidos: {len(limpio)})")
    return limpio


def _cache_get(endpoint: str, cuit: str):
    key = (endpoint, cuit)
    if key in _CACHE:
        ts, val = _CACHE[key]
        if time.time() - ts < CACHE_TTL_SEGUNDOS:
            return True, val
        del _CACHE[key]
    return False, None


def _cache_set(endpoint: str, cuit: str, val):
    _CACHE[(endpoint, cuit)] = (time.time(), val)


async def consultar_bcra(endpoint: str, cuit: str) -> tuple[Optional[dict], bool]:
    """Devuelve (datos, desde_cache). Aplica delay mínimo entre consultas al BCRA
    para evitar rate limit."""
    global _ULTIMA_CONSULTA_BCRA

    hit, val = _cache_get(endpoint, cuit)
    if hit:
        return val, True

    url = f"{BCRA_BASE}/{endpoint}/{cuit}" if endpoint else f"{BCRA_BASE}/{cuit}"
    ultimo_error = None

    for intento in range(HTTP_REINTENTOS):
        try:
            # Respeta el delay mínimo entre consultas al BCRA (rate limit)
            async with _LOCK_BCRA:
                elapsed = time.time() - _ULTIMA_CONSULTA_BCRA
                if elapsed < _DELAY_ENTRE_CONSULTAS:
                    await asyncio.sleep(_DELAY_ENTRE_CONSULTAS - elapsed)
                _ULTIMA_CONSULTA_BCRA = time.time()

            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False, follow_redirects=True) as client:
                r = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
                })

            if r.status_code == 404:
                _cache_set(endpoint, cuit, None)
                return None, False
            if r.status_code == 200:
                datos = r.json().get("results")
                _cache_set(endpoint, cuit, datos)
                return datos, False
            if 500 <= r.status_code < 600:
                ultimo_error = f"BCRA respondió {r.status_code}"
                await asyncio.sleep(1 + intento * 2)
                continue
            raise HTTPException(502, f"BCRA devolvió status {r.status_code}")

        except httpx.TimeoutException:
            ultimo_error = f"timeout después de {HTTP_TIMEOUT}s"
            await asyncio.sleep(1 + intento * 2)
        except httpx.ReadError:
            ultimo_error = "conexión cortada por el BCRA (rate limit)"
            await asyncio.sleep(2 + intento * 3)
        except Exception as exc:
            ultimo_error = f"{type(exc).__name__}: {str(exc)[:150]}"
            await asyncio.sleep(1 + intento * 2)

    raise HTTPException(502, f"BCRA no disponible tras {HTTP_REINTENTOS} intentos. {ultimo_error}.")


def evaluar_situacion_crediticia(deudas, cfg):
    motivos = []
    periodos = deudas.get("periodos", [])
    if not periodos:
        return motivos
    ultimo = periodos[0]
    for ent in ultimo.get("entidades", []):
        situacion = ent.get("situacion", 1)
        monto_pesos = float(ent.get("monto", 0)) * 1000
        entidad_nombre = ent.get("entidad", "entidad")
        if situacion >= 3:
            etiquetas = {
                3: "Situación 3 (Con problemas)",
                4: "Situación 4 (Alto riesgo)",
                5: "Situación 5 (Irrecuperable)",
                6: "Situación 6",
            }
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=f"{etiquetas.get(situacion, f'Situación {situacion}')} en {entidad_nombre}. Deuda: ${monto_pesos:,.0f}.",
                severidad="rojo",
            ))
        elif situacion == 2 and monto_pesos > cfg["umbral_monto_sit2"]:
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=f"Situación 2 en {entidad_nombre} con deuda ${monto_pesos:,.0f}, supera umbral.",
                severidad="rojo",
            ))
        elif situacion == 2:
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=f"Situación 2 en {entidad_nombre} por ${monto_pesos:,.0f} (dentro del umbral).",
                severidad="info",
            ))
    return motivos


def evaluar_cheques_rechazados(cheques, cfg):
    motivos = []
    fecha_limite = datetime.now() - timedelta(days=30 * cfg["meses_rechazo_reciente"])
    for causal in cheques.get("causales", []):
        desc_causal = causal.get("descripcion", "rechazo")
        for entidad in causal.get("entidades", []):
            nombre_entidad = entidad.get("entidad", "banco")
            for chq in entidad.get("detalle", []):
                numero = chq.get("nroCheque", "s/n")
                monto = float(chq.get("monto", 0))
                fecha_rechazo_str = chq.get("fechaRechazo")
                fecha_pago_str = chq.get("fechaPago")
                fecha_rechazo = _parse_fecha(fecha_rechazo_str)
                pendiente = not fecha_pago_str or fecha_pago_str in ("0001-01-01", "")
                if pendiente:
                    motivos.append(MotivoDecision(
                        categoria="cheque_pendiente",
                        descripcion=f"Cheque PENDIENTE nº {numero} en {nombre_entidad} ({desc_causal}) por ${monto:,.0f}.",
                        severidad="rojo",
                    ))
                elif fecha_rechazo and fecha_rechazo >= fecha_limite:
                    motivos.append(MotivoDecision(
                        categoria="cheque_reciente",
                        descripcion=f"Cheque rechazado (regularizado) nº {numero} del {fecha_rechazo_str}.",
                        severidad="rojo",
                    ))
    return motivos


def _parse_fecha(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt)
        except ValueError:
            continue
    return None


@app.get("/")
async def root():
    return {
        "servicio": "CHEQ-OK API",
        "version": "0.2.0",
        "cache_entries": len(_CACHE),
        "cache_ttl_horas": CACHE_TTL_SEGUNDOS / 3600,
        "endpoints": {
            "evaluar": "/evaluar/{cuit}",
            "precargar": "/precargar/{cuit}",
            "config": "/config",
            "cache/clear": "/cache/clear",
            "docs": "/docs",
        },
    }


@app.get("/evaluar/{cuit}", response_model=Evaluacion)
async def evaluar(cuit: str):
    """Evalúa un CUIT. Hace las dos consultas al BCRA EN PARALELO."""
    inicio = time.time()
    cuit_limpio = validar_cuit(cuit)
    cfg = get_config()

    # CAMBIO CLAVE: las dos consultas al BCRA van en paralelo
    (deudas, d_cache), (cheques, c_cache) = await asyncio.gather(
        consultar_bcra("", cuit_limpio),
        consultar_bcra("ChequesRechazados", cuit_limpio),
    )

    desde_cache = d_cache and c_cache
    tiempo_ms = int((time.time() - inicio) * 1000)

    motivos = []
    denominacion = None

    if deudas is None and cheques is None:
        return Evaluacion(
            cuit=cuit_limpio, denominacion=None, semaforo="VERDE",
            motivos=[MotivoDecision(
                categoria="sin_antecedentes",
                descripcion="El CUIT no figura en la Central de Deudores del BCRA.",
                severidad="info",
            )],
            resumen="VERDE · Sin antecedentes en BCRA.",
            consultado_en=datetime.now(), config_aplicada=cfg,
            desde_cache=desde_cache, tiempo_ms=tiempo_ms,
        )

    if deudas:
        denominacion = deudas.get("denominacion")
        motivos.extend(evaluar_situacion_crediticia(deudas, cfg))
    if cheques:
        if not denominacion:
            denominacion = cheques.get("denominacion")
        motivos.extend(evaluar_cheques_rechazados(cheques, cfg))

    hay_rojo = any(m.severidad == "rojo" for m in motivos)
    semaforo = "ROJO" if hay_rojo else "VERDE"
    resumen = (
        f"ROJO · {len(motivos)} motivo(s) detectado(s)."
        if semaforo == "ROJO"
        else "VERDE · No se detectaron alertas."
    )

    return Evaluacion(
        cuit=cuit_limpio, denominacion=denominacion, semaforo=semaforo,
        motivos=motivos, resumen=resumen,
        detalle_deudas=deudas, detalle_cheques_rechazados=cheques,
        consultado_en=datetime.now(), config_aplicada=cfg,
        desde_cache=desde_cache, tiempo_ms=tiempo_ms,
    )


@app.get("/precargar/{cuit}")
async def precargar(cuit: str):
    """Pre-carga las consultas de un CUIT en cache sin devolver el análisis
    completo. Se usa desde la web cuando el usuario empieza a escribir el CUIT,
    para que cuando apriete Evaluar la respuesta sea instantánea."""
    cuit_limpio = validar_cuit(cuit)
    try:
        await asyncio.gather(
            consultar_bcra("", cuit_limpio),
            consultar_bcra("ChequesRechazados", cuit_limpio),
        )
        return {"ok": True, "cuit": cuit_limpio, "precargado": True}
    except Exception as e:
        return {"ok": False, "cuit": cuit_limpio, "error": str(e)}


@app.get("/config")
async def ver_config():
    return get_config()


class ConfigUpdate(BaseModel):
    umbral_monto_sit2: Optional[float] = Field(None, ge=0)
    meses_rechazo_reciente: Optional[int] = Field(None, ge=0, le=120)


@app.post("/config")
async def actualizar_config(body: ConfigUpdate):
    nuevos = {k: v for k, v in body.dict().items() if v is not None}
    return set_config(nuevos)


@app.post("/cache/clear")
async def limpiar_cache():
    cantidad = len(_CACHE)
    _CACHE.clear()
    return {"eliminadas": cantidad}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
