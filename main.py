"""
CHEQ-OK · Backend de evaluación de cheques argentinos
======================================================
Consulta la API pública del BCRA (Central de Deudores) y devuelve una
recomendación tipo semáforo sobre si conviene o no recibir un cheque,
según las reglas de negocio acordadas.

Reglas configurables en /config (ver archivo config.py):
  - umbral_monto_sit2: monto en pesos a partir del cual la situación 2
    dispara ROJO.
  - meses_rechazo_reciente: cantidad de meses hacia atrás en que un
    cheque rechazado (aunque haya sido pagado) sigue siendo una alerta.

Endpoints:
  GET  /                      -> info del servicio
  GET  /evaluar/{cuit}        -> evalúa un CUIT y devuelve semáforo
  GET  /config                -> ver configuración actual
  POST /config                -> actualizar configuración (umbrales)
  GET  /docs                  -> documentación interactiva (Swagger)
"""

from datetime import datetime, timedelta
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from config import get_config, set_config

# ---------------------------------------------------------------------------
# Configuración general
# ---------------------------------------------------------------------------

BCRA_BASE = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas"
HTTP_TIMEOUT = 15.0  # segundos

app = FastAPI(
    title="CHEQ-OK API",
    description=(
        "Evaluación automática de cheques argentinos usando la API pública "
        "del BCRA. Devuelve un semáforo VERDE/ROJO según la situación "
        "crediticia y los cheques rechazados del librador."
    ),
    version="0.1.0",
)

# Habilitar CORS para que la web de prueba y la app Android puedan consumir
# este backend desde cualquier origen durante el desarrollo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Modelos de datos (lo que la API devuelve)
# ---------------------------------------------------------------------------

class MotivoDecision(BaseModel):
    """Un motivo individual que contribuye al semáforo final."""
    categoria: str = Field(..., description="Tipo de alerta: situacion_crediticia, cheque_pendiente, cheque_reciente")
    descripcion: str
    severidad: str = Field(..., description="rojo | amarillo | info")


class Evaluacion(BaseModel):
    cuit: str
    denominacion: Optional[str] = None
    semaforo: str = Field(..., description="VERDE | ROJO")
    motivos: list[MotivoDecision]
    resumen: str
    detalle_deudas: Optional[dict] = None
    detalle_cheques_rechazados: Optional[dict] = None
    consultado_en: datetime
    config_aplicada: dict


# ---------------------------------------------------------------------------
# Helpers: validación de CUIT y llamadas al BCRA
# ---------------------------------------------------------------------------

def validar_cuit(cuit: str) -> str:
    """Saca guiones/espacios y valida que sea un CUIT de 11 dígitos con
    dígito verificador correcto. Devuelve el CUIT limpio."""
    limpio = "".join(c for c in cuit if c.isdigit())
    if len(limpio) != 11:
        raise HTTPException(400, f"CUIT debe tener 11 dígitos (recibido: {len(limpio)})")

    # Validación del dígito verificador (algoritmo oficial AFIP)
    multiplicadores = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    suma = sum(int(limpio[i]) * multiplicadores[i] for i in range(10))
    resto = suma % 11
    dv_esperado = 0 if resto == 0 else (11 - resto)
    # El dígito verificador puede ser 10, en cuyo caso la AFIP lo asigna como 9
    if dv_esperado == 10:
        dv_esperado = 9
    if int(limpio[10]) != dv_esperado:
        raise HTTPException(400, f"CUIT inválido: dígito verificador incorrecto")

    return limpio


async def consultar_bcra(endpoint: str, cuit: str) -> Optional[dict]:
    """Hace un GET al BCRA. Devuelve el dict 'results' si hay datos,
    None si el BCRA responde 404 (sin antecedentes). Lanza error en
    otros casos."""
    url = f"{BCRA_BASE}/{endpoint}/{cuit}" if endpoint else f"{BCRA_BASE}/{cuit}"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(url)
    except httpx.RequestError as exc:
        raise HTTPException(502, f"Error conectando al BCRA: {exc}")

    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise HTTPException(502, f"BCRA devolvió status {r.status_code}")

    data = r.json()
    return data.get("results")


# ---------------------------------------------------------------------------
# Motor de decisión
# ---------------------------------------------------------------------------

def evaluar_situacion_crediticia(deudas: dict, cfg: dict) -> list[MotivoDecision]:
    """Revisa cada entidad en el último período y genera motivos ROJO
    cuando corresponde."""
    motivos: list[MotivoDecision] = []

    periodos = deudas.get("periodos", [])
    if not periodos:
        return motivos

    # Tomamos el período más reciente (el primero en la lista, según BCRA)
    ultimo = periodos[0]
    for ent in ultimo.get("entidades", []):
        situacion = ent.get("situacion", 1)
        # BCRA informa montos en MILES de pesos, así que multiplicamos x1000
        monto_pesos = float(ent.get("monto", 0)) * 1000
        entidad_nombre = ent.get("entidad", "entidad desconocida")

        if situacion >= 3:
            etiqueta_sit = {
                3: "Situación 3 (Con problemas)",
                4: "Situación 4 (Alto riesgo de insolvencia)",
                5: "Situación 5 (Irrecuperable)",
                6: "Situación 6 (Irrec. por disposición técnica)",
            }.get(situacion, f"Situación {situacion}")
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=(
                    f"{etiqueta_sit} en {entidad_nombre}. "
                    f"Deuda informada: ${monto_pesos:,.0f}."
                ),
                severidad="rojo",
            ))
        elif situacion == 2 and monto_pesos > cfg["umbral_monto_sit2"]:
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=(
                    f"Situación 2 (Riesgo bajo) en {entidad_nombre} con deuda "
                    f"de ${monto_pesos:,.0f}, supera el umbral configurado de "
                    f"${cfg['umbral_monto_sit2']:,.0f}."
                ),
                severidad="rojo",
            ))
        elif situacion == 2:
            motivos.append(MotivoDecision(
                categoria="situacion_crediticia",
                descripcion=(
                    f"Situación 2 en {entidad_nombre} por ${monto_pesos:,.0f} "
                    f"(dentro del umbral permitido)."
                ),
                severidad="info",
            ))

    return motivos


def evaluar_cheques_rechazados(cheques: dict, cfg: dict) -> list[MotivoDecision]:
    """Recorre cheques rechazados y genera motivos ROJO para:
       - Cualquier cheque rechazado sin pagar (pendiente).
       - Cheques rechazados pagados pero dentro de los últimos N meses.
    """
    motivos: list[MotivoDecision] = []
    hoy = datetime.now()
    fecha_limite = hoy - timedelta(days=30 * cfg["meses_rechazo_reciente"])

    # La estructura del BCRA agrupa cheques por causal (sin fondos, defectos
    # formales) y dentro de cada uno por entidad. Recorremos toda la
    # estructura de forma defensiva.
    causales = cheques.get("causales", [])
    for causal in causales:
        descripcion_causal = causal.get("descripcion", "rechazo")
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
                        descripcion=(
                            f"Cheque rechazado PENDIENTE de pago nº {numero} "
                            f"en {nombre_entidad} ({descripcion_causal}) por "
                            f"${monto:,.0f}"
                            + (f", rechazado el {fecha_rechazo_str}." if fecha_rechazo_str else ".")
                        ),
                        severidad="rojo",
                    ))
                elif fecha_rechazo and fecha_rechazo >= fecha_limite:
                    motivos.append(MotivoDecision(
                        categoria="cheque_reciente",
                        descripcion=(
                            f"Cheque rechazado (luego regularizado) nº {numero} "
                            f"en {nombre_entidad}, del {fecha_rechazo_str}, "
                            f"dentro de los últimos {cfg['meses_rechazo_reciente']} "
                            f"meses."
                        ),
                        severidad="rojo",
                    ))

    return motivos


def _parse_fecha(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {
        "servicio": "CHEQ-OK API",
        "version": "0.1.0",
        "descripcion": "Evaluación de cheques argentinos vía BCRA",
        "endpoints": {
            "evaluar": "/evaluar/{cuit}",
            "config": "/config",
            "docs": "/docs",
        },
    }


@app.get("/evaluar/{cuit}", response_model=Evaluacion)
async def evaluar(cuit: str):
    """Evalúa un CUIT consultando el BCRA y devuelve un semáforo."""
    cuit_limpio = validar_cuit(cuit)
    cfg = get_config()

    # Consultas en paralelo sería más rápido, pero secuencial es más simple
    # y en práctica el BCRA responde en < 1s.
    deudas = await consultar_bcra("", cuit_limpio)
    cheques = await consultar_bcra("ChequesRechazados", cuit_limpio)

    motivos: list[MotivoDecision] = []
    denominacion = None

    # Caso 1: sin antecedentes en BCRA → VERDE directo
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
            detalle_deudas=None,
            detalle_cheques_rechazados=None,
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

    # Decisión final: ROJO si hay al menos un motivo de severidad "rojo"
    hay_rojo = any(m.severidad == "rojo" for m in motivos)
    semaforo = "ROJO" if hay_rojo else "VERDE"

    if semaforo == "ROJO":
        resumen = (
            f"ROJO · No se recomienda recibir el cheque. "
            f"{len(motivos)} motivo(s) de alerta detectado(s)."
        )
    else:
        resumen = (
            "VERDE · Se recomienda recibir el cheque. "
            "No se detectaron alertas según las reglas configuradas."
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
