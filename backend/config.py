"""Configuración persistente de la app. Guarda los umbrales en un archivo
JSON al lado del código, así se puede modificar sin tocar el programa.

En producción, esto podría ir en una base de datos o en variables de
entorno. Para el MVP con un JSON local alcanza y sobra.
"""
import json
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(__file__).parent / "config.json"

DEFAULTS = {
    "umbral_monto_sit2": 500_000,      # pesos
    "meses_rechazo_reciente": 12,      # meses
}


def _cargar() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULTS, indent=2))
        return dict(DEFAULTS)
    try:
        return {**DEFAULTS, **json.loads(CONFIG_PATH.read_text())}
    except Exception:
        return dict(DEFAULTS)


def get_config() -> dict:
    return _cargar()


def set_config(nuevos: dict[str, Any]) -> dict:
    actual = _cargar()
    actual.update(nuevos)
    CONFIG_PATH.write_text(json.dumps(actual, indent=2))
    return actual
