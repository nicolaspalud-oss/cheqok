"""
CHEQ-OK · Backend de evaluación de cheques argentinos (v0.1.1)
"""

import asyncio
from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import get_config, set_config

BCRA_BASE = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas"
HTTP_TIMEOUT = 30.0
HTTP_REINTENTOS = 3

app = FastAPI(
    title="CHEQ-OK API",
    description="Evaluación automática de cheques argentinos usando BCRA.",
    version="0.1.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def validar_cuit(cuit: str) -> str:
    limpio = "".join(c for c in cuit if c.isdigit())
    if len(limpio) != 11:
        raise HTTPException(400, f"CUIT debe tener 11 dígitos (recibidos: {len(limpio)})")
    return limpio


async def consultar_bcra(endpoint: str, cuit: str) -> Optional[dict]:
    url = f"{BCRA_BASE}/{endpoint}/{cuit}" if endpoint else f"{BCRA_BASE}/{cuit}"
    ultimo_error = None

    for intento in range(HTTP_REINTENTOS):
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0 cheqok/0.1"})

            if r.status_code == 404:
                return None
            if r.status_code == 200:
                return r.json().get("results")

            if 500 <= r.status_code < 600:
                ultimo_error = f"BCRA respondió {r.status_code}"
                await asyncio.sleep(2 ** intento)
                continue

            raise HTTPException(502, f"BCRA devolvió status {r.status_code}")

        except httpx.TimeoutException:
            ultimo_error = "timeout después de 30s"
            await asyncio.sleep(2 ** intento)
        except httpx.ConnectError as exc:
            ultimo_error = f"no se pudo conectar: {type(exc).__name__}"
            await asyncio.sleep(2 ** intento)
        except httpx.RequestError as exc:
            ultimo_error = f"{type(exc).__name__}: {str(exc)[:100]}"
            await asyncio.sleep(2 ** intento)
        except Exception as exc:
            ultimo_error = f"{type(exc).__name__}: {str(exc)[:100]}"
            await asyncio.sleep(2 ** intento)

    raise HTTPException(
        502,
        f"No se pudo conectar al BCRA después de {HTTP_REINTENTOS} intentos. "
        f"Último error: {ultimo_error}"
    )


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
                4: "Situación 4 (Alto riesgo de insolvencia)",
                5: "Situación 5 (Irrecuperable)",
                6: "Situación 6 (Irrec. por disposición técnica)",
            }
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=f"{etiquetas.get(situacion, f'Situación {situacion}')} en {entidad_nombre}. Deuda: ${monto_pesos:,.0f}.",
                severidad="rojo",
            ))
        elif situacion == 2 and monto_pesos > cfg["umbral_monto_sit2"]:
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=f"Situación 2 en {entidad_nombre} con deuda ${monto_pesos:,.0f}, supera umbral ${cfg['umbral_monto_sit2']:,.0f}.",
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
                        descripcion=f"Cheque PENDIENTE de pago nº {numero} en {nombre_entidad} ({desc_causal}) por ${monto:,.0f}" + (f", rechazado el {fecha_rechazo_str}." if fecha_rechazo_str else "."),
                        severidad="rojo",
                    ))
                elif fecha_rechazo and fecha_rechazo >= fecha_limite:
                    motivos.append(MotivoDecision(
                        categoria="cheque_reciente",
                        descripcion=f"Cheque rechazado (regularizado) nº {numero} en {nombre_entidad}, del {fecha_rechazo_str}, dentro de los últimos {cfg['meses_rechazo_reciente']} meses.",
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
        "version": "0.1.1",
        "endpoints": {"evaluar": "/evaluar/{cuit}", "config": "/config", "docs": "/docs"},
    }


@app.get("/evaluar/{cuit}", response_model=Evaluacion)
async def evaluar(cuit: str):
    cuit_limpio = validar_cuit(cuit)
    cfg = get_config()

    deudas = await consultar_bcra("", cuit_limpio)
    cheques = await consultar_bcra("ChequesRechazados", cuit_limpio)

    motivos = []
    denominacion = None

    if deudas is None and cheques is None:
        return Evaluacion(
            cuit=cuit_limpio,
            denominacion=None,
            semaforo="VERDE",
            motivos=[MotivoDecision(
                categoria="sin_antecedentes",
                descripcion="El CUIT no figura en la Central de Deudores del BCRA.",
                severidad="info",
            )],
            resumen="VERDE · Sin antecedentes en BCRA. Se recomienda recibir el cheque.",
            consultado_en=datetime.now(),
            config_aplicada=cfg,
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
        f"ROJO · No se recomienda recibir el cheque. {len(motivos)} motivo(s) detectado(s)."
        if semaforo == "ROJO"
        else "VERDE · Se recomienda recibir el cheque. No se detectaron alertas."
    )

    return Evaluacion(
        cuit=cuit_limpio,
        denominacion=denominacion,
        semaforo=semaforo,
        motivos=motivos,
        resumen=resumen,
        detalle_deudas=deudas,
        detalle_cheques_rechazados=cheques,
        consultado_en=datetime.now(),
        config_aplicada=cfg,
    )


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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
