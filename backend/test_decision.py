"""
Tests del motor de decisión con datos simulados del BCRA.

Como el BCRA puede no estar disponible en todo momento, acá mockeamos
la respuesta de la API y probamos que la lógica de decisión sea correcta
en cada uno de los escenarios que definimos:

  1. CUIT sin antecedentes           → VERDE
  2. Situación 1 en todas las entidades → VERDE
  3. Situación 2 con deuda baja      → VERDE
  4. Situación 2 con deuda alta      → ROJO
  5. Situación 3                     → ROJO
  6. Cheque rechazado pendiente      → ROJO
  7. Cheque rechazado pagado reciente → ROJO
  8. Cheque rechazado pagado viejo   → VERDE
"""
from datetime import datetime, timedelta
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app)


def _fecha_hace(meses: int) -> str:
    d = datetime.now() - timedelta(days=30 * meses)
    return d.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Escenarios de prueba
# ---------------------------------------------------------------------------

ESCENARIOS = {
    "sin_antecedentes": {
        "descripcion": "CUIT no registrado en BCRA",
        "cuit": "20123456790",  # inventado pero con DV válido
        "deudas": None,
        "cheques": None,
        "esperado": "VERDE",
    },
    "situacion_1_normal": {
        "descripcion": "Cliente en situación 1 (normal) en todas las entidades",
        "cuit": "20245874319",
        "deudas": {
            "identificacion": 20245874319,
            "denominacion": "PEREZ JUAN",
            "periodos": [{
                "periodo": "2026-03",
                "entidades": [
                    {"entidad": "BANCO NACION", "situacion": 1, "monto": 250.0},
                ],
            }],
        },
        "cheques": None,
        "esperado": "VERDE",
    },
    "situacion_2_baja": {
        "descripcion": "Situación 2 con deuda de $300.000 (bajo umbral)",
        "cuit": "20245874319",
        "deudas": {
            "denominacion": "PEREZ JUAN",
            "periodos": [{
                "periodo": "2026-03",
                "entidades": [
                    {"entidad": "BANCO MACRO", "situacion": 2, "monto": 300.0},
                ],
            }],
        },
        "cheques": None,
        "esperado": "VERDE",
    },
    "situacion_2_alta": {
        "descripcion": "Situación 2 con deuda de $800.000 (supera umbral)",
        "cuit": "20245874319",
        "deudas": {
            "denominacion": "PEREZ JUAN",
            "periodos": [{
                "periodo": "2026-03",
                "entidades": [
                    {"entidad": "BANCO MACRO", "situacion": 2, "monto": 800.0},
                ],
            }],
        },
        "cheques": None,
        "esperado": "ROJO",
    },
    "situacion_3": {
        "descripcion": "Situación 3 (Con problemas) sin importar monto",
        "cuit": "20245874319",
        "deudas": {
            "denominacion": "PEREZ JUAN",
            "periodos": [{
                "periodo": "2026-03",
                "entidades": [
                    {"entidad": "BANCO SANTANDER", "situacion": 3, "monto": 100.0},
                ],
            }],
        },
        "cheques": None,
        "esperado": "ROJO",
    },
    "cheque_pendiente": {
        "descripcion": "Cheque rechazado sin pagar",
        "cuit": "20245874319",
        "deudas": {
            "denominacion": "PEREZ JUAN",
            "periodos": [{
                "periodo": "2026-03",
                "entidades": [
                    {"entidad": "BANCO NACION", "situacion": 1, "monto": 100.0},
                ],
            }],
        },
        "cheques": {
            "denominacion": "PEREZ JUAN",
            "causales": [{
                "descripcion": "Sin fondos",
                "entidades": [{
                    "entidad": "BANCO NACION",
                    "detalle": [{
                        "nroCheque": "12345",
                        "monto": 500000,
                        "fechaRechazo": _fecha_hace(3),
                        "fechaPago": None,
                    }],
                }],
            }],
        },
        "esperado": "ROJO",
    },
    "cheque_pagado_reciente": {
        "descripcion": "Cheque rechazado pero pagado, de hace 6 meses",
        "cuit": "20245874319",
        "deudas": None,
        "cheques": {
            "denominacion": "PEREZ JUAN",
            "causales": [{
                "descripcion": "Sin fondos",
                "entidades": [{
                    "entidad": "BANCO NACION",
                    "detalle": [{
                        "nroCheque": "98765",
                        "monto": 200000,
                        "fechaRechazo": _fecha_hace(6),
                        "fechaPago": _fecha_hace(5),
                    }],
                }],
            }],
        },
        "esperado": "ROJO",
    },
    "cheque_pagado_viejo": {
        "descripcion": "Cheque rechazado pagado hace 18 meses (fuera de ventana)",
        "cuit": "20245874319",
        "deudas": None,
        "cheques": {
            "denominacion": "PEREZ JUAN",
            "causales": [{
                "descripcion": "Sin fondos",
                "entidades": [{
                    "entidad": "BANCO NACION",
                    "detalle": [{
                        "nroCheque": "54321",
                        "monto": 100000,
                        "fechaRechazo": _fecha_hace(18),
                        "fechaPago": _fecha_hace(17),
                    }],
                }],
            }],
        },
        "esperado": "VERDE",
    },
}


@pytest.mark.parametrize("nombre,escenario", list(ESCENARIOS.items()))
def test_escenarios(nombre, escenario):
    """Para cada escenario, mockeamos la llamada al BCRA y verificamos
    que el semáforo coincida con lo esperado."""

    async def mock_consultar(endpoint, cuit):
        if endpoint == "ChequesRechazados":
            return escenario["cheques"]
        return escenario["deudas"]

    with patch("main.consultar_bcra", side_effect=mock_consultar):
        r = client.get(f"/evaluar/{escenario['cuit']}")

    assert r.status_code == 200, f"[{nombre}] status {r.status_code}: {r.text}"
    data = r.json()
    assert data["semaforo"] == escenario["esperado"], (
        f"[{nombre}] {escenario['descripcion']}\n"
        f"  esperado: {escenario['esperado']}\n"
        f"  obtenido: {data['semaforo']}\n"
        f"  motivos: {data['motivos']}"
    )
    print(f"✓ {nombre}: {data['semaforo']} — {data['resumen']}")


def test_cuit_invalido():
    """CUIT con DV incorrecto debe devolver 400."""
    r = client.get("/evaluar/20123456789")  # DV inválido
    assert r.status_code == 400


def test_cuit_corto():
    """CUIT con menos de 11 dígitos debe devolver 400."""
    r = client.get("/evaluar/12345")
    assert r.status_code == 400


def test_config_endpoint():
    """El endpoint /config debe devolver los umbrales actuales."""
    r = client.get("/config")
    assert r.status_code == 200
    cfg = r.json()
    assert "umbral_monto_sit2" in cfg
    assert "meses_rechazo_reciente" in cfg


def test_config_update():
    """Podemos actualizar el umbral y verlo reflejado."""
    r = client.post("/config", json={"umbral_monto_sit2": 750000})
    assert r.status_code == 200
    assert r.json()["umbral_monto_sit2"] == 750000
    # Restaurar default
    client.post("/config", json={"umbral_monto_sit2": 500000})
