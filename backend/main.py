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
    """Saca guiones/espacios y valida que sean 11 dígitos. No valida
    el dígito verificador: si el CUIT fuera inválido, el BCRA responderá
    404 y lo manejaremos como 'sin antecedentes' (siguiendo la regla
    que definimos). Esto da mejor tolerancia a errores de transcripción.
    """
    limpio = "".join(c for c in cuit if c.isdigit())
    if len(limpio) != 11:
        raise HTTPException(
            400,
            f"CUIT debe tener 11 dígitos (recibidos: {len(limpio)})
