"""
Validación manual del motor de decisión usando solo Python estándar.
Extrae la lógica de main.py y la ejecuta con datos simulados para
confirmar que las reglas responden correctamente antes de instalar
las dependencias.
"""
from datetime import datetime, timedelta


# ---------------------------------------------------------------------------
# Lógica replicada (sin FastAPI, para testear standalone)
# ---------------------------------------------------------------------------

def _parse_fecha(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19] if "T" in s else s, fmt)
        except ValueError:
            continue
    return None


def evaluar_situacion_crediticia(deudas, cfg):
    motivos = []
    if not deudas:
        return motivos
    periodos = deudas.get("periodos", [])
    if not periodos:
        return motivos
    ultimo = periodos[0]
    for ent in ultimo.get("entidades", []):
        situacion = ent.get("situacion", 1)
        monto_pesos = float(ent.get("monto", 0)) * 1000
        entidad_nombre = ent.get("entidad", "entidad")
        if situacion >= 3:
            motivos.append({
                "severidad": "rojo",
                "desc": f"Situación {situacion} en {entidad_nombre}: ${monto_pesos:,.0f}",
            })
        elif situacion == 2 and monto_pesos > cfg["umbral_monto_sit2"]:
            motivos.append({
                "severidad": "rojo",
                "desc": f"Situación 2 en {entidad_nombre}: ${monto_pesos:,.0f} > umbral ${cfg['umbral_monto_sit2']:,.0f}",
            })
        elif situacion == 2:
            motivos.append({
                "severidad": "info",
                "desc": f"Situación 2 en {entidad_nombre}: ${monto_pesos:,.0f} (dentro del umbral)",
            })
    return motivos


def evaluar_cheques(cheques, cfg):
    motivos = []
    if not cheques:
        return motivos
    fecha_limite = datetime.now() - timedelta(days=30 * cfg["meses_rechazo_reciente"])
    for causal in cheques.get("causales", []):
        for entidad in causal.get("entidades", []):
            for chq in entidad.get("detalle", []):
                numero = chq.get("nroCheque", "?")
                fecha_rechazo_str = chq.get("fechaRechazo")
                fecha_pago_str = chq.get("fechaPago")
                fecha_rechazo = _parse_fecha(fecha_rechazo_str)
                pendiente = not fecha_pago_str or fecha_pago_str in ("", "0001-01-01")
                if pendiente:
                    motivos.append({
                        "severidad": "rojo",
                        "desc": f"Cheque {numero} PENDIENTE de pago (rechazado {fecha_rechazo_str})",
                    })
                elif fecha_rechazo and fecha_rechazo >= fecha_limite:
                    motivos.append({
                        "severidad": "rojo",
                        "desc": f"Cheque {numero} rechazado el {fecha_rechazo_str} (pagado, pero < {cfg['meses_rechazo_reciente']} meses)",
                    })
    return motivos


def evaluar(deudas, cheques, cfg):
    motivos = []
    if deudas is None and cheques is None:
        return "VERDE", [{"severidad": "info", "desc": "Sin antecedentes en BCRA"}]
    motivos.extend(evaluar_situacion_crediticia(deudas, cfg))
    motivos.extend(evaluar_cheques(cheques, cfg))
    semaforo = "ROJO" if any(m["severidad"] == "rojo" for m in motivos) else "VERDE"
    return semaforo, motivos


# ---------------------------------------------------------------------------
# Casos de prueba
# ---------------------------------------------------------------------------

def _hace(meses):
    return (datetime.now() - timedelta(days=30 * meses)).strftime("%Y-%m-%d")


CFG = {"umbral_monto_sit2": 500_000, "meses_rechazo_reciente": 12}

CASOS = [
    {
        "nombre": "1. Sin antecedentes",
        "deudas": None, "cheques": None,
        "esperado": "VERDE",
    },
    {
        "nombre": "2. Situación 1 normal",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "NACION", "situacion": 1, "monto": 500.0}
        ]}]},
        "cheques": None,
        "esperado": "VERDE",
    },
    {
        "nombre": "3. Situación 2 con $300.000 (bajo umbral)",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "MACRO", "situacion": 2, "monto": 300.0}
        ]}]},
        "cheques": None,
        "esperado": "VERDE",
    },
    {
        "nombre": "4. Situación 2 con $800.000 (supera umbral)",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "MACRO", "situacion": 2, "monto": 800.0}
        ]}]},
        "cheques": None,
        "esperado": "ROJO",
    },
    {
        "nombre": "5. Situación 3 (Con problemas)",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "SANTANDER", "situacion": 3, "monto": 100.0}
        ]}]},
        "cheques": None,
        "esperado": "ROJO",
    },
    {
        "nombre": "6. Situación 5 (Irrecuperable)",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "BBVA", "situacion": 5, "monto": 50.0}
        ]}]},
        "cheques": None,
        "esperado": "ROJO",
    },
    {
        "nombre": "7. Cheque rechazado PENDIENTE",
        "deudas": None,
        "cheques": {"causales": [{"entidades": [{"entidad": "NACION", "detalle": [
            {"nroCheque": "12345", "monto": 500000, "fechaRechazo": _hace(3), "fechaPago": None}
        ]}]}]},
        "esperado": "ROJO",
    },
    {
        "nombre": "8. Cheque rechazado pagado hace 6 meses",
        "deudas": None,
        "cheques": {"causales": [{"entidades": [{"entidad": "NACION", "detalle": [
            {"nroCheque": "987", "monto": 200000, "fechaRechazo": _hace(6), "fechaPago": _hace(5)}
        ]}]}]},
        "esperado": "ROJO",
    },
    {
        "nombre": "9. Cheque rechazado pagado hace 18 meses (fuera de ventana)",
        "deudas": None,
        "cheques": {"causales": [{"entidades": [{"entidad": "NACION", "detalle": [
            {"nroCheque": "543", "monto": 100000, "fechaRechazo": _hace(18), "fechaPago": _hace(17)}
        ]}]}]},
        "esperado": "VERDE",
    },
    {
        "nombre": "10. Combo: Sit 1 + cheque pendiente",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "NACION", "situacion": 1, "monto": 100.0}
        ]}]},
        "cheques": {"causales": [{"entidades": [{"entidad": "NACION", "detalle": [
            {"nroCheque": "111", "monto": 100000, "fechaRechazo": _hace(2), "fechaPago": None}
        ]}]}]},
        "esperado": "ROJO",
    },
    {
        "nombre": "11. Múltiples entidades, todas sit 1",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "NACION", "situacion": 1, "monto": 1000.0},
            {"entidad": "SANTANDER", "situacion": 1, "monto": 500.0},
            {"entidad": "BBVA", "situacion": 1, "monto": 2000.0},
        ]}]},
        "cheques": None,
        "esperado": "VERDE",
    },
    {
        "nombre": "12. Múltiples entidades, una en sit 4",
        "deudas": {"periodos": [{"entidades": [
            {"entidad": "NACION", "situacion": 1, "monto": 1000.0},
            {"entidad": "SANTANDER", "situacion": 4, "monto": 50.0},
        ]}]},
        "cheques": None,
        "esperado": "ROJO",
    },
]


def main():
    print(f"\n{'='*70}")
    print("VALIDACIÓN DEL MOTOR DE DECISIÓN · CHEQ-OK")
    print(f"{'='*70}")
    print(f"Configuración: umbral sit.2=${CFG['umbral_monto_sit2']:,.0f}, "
          f"ventana cheques={CFG['meses_rechazo_reciente']} meses\n")

    fallos = 0
    for caso in CASOS:
        semaforo, motivos = evaluar(caso["deudas"], caso["cheques"], CFG)
        ok = semaforo == caso["esperado"]
        marca = "✓" if ok else "✗"
        print(f"{marca} {caso['nombre']}")
        print(f"    esperado={caso['esperado']}  obtenido={semaforo}")
        for m in motivos:
            print(f"    · [{m['severidad']}] {m['desc']}")
        if not ok:
            fallos += 1
        print()

    print(f"{'='*70}")
    if fallos == 0:
        print(f"✓ TODOS LOS {len(CASOS)} CASOS PASARON")
    else:
        print(f"✗ {fallos}/{len(CASOS)} CASOS FALLARON")
    print(f"{'='*70}\n")
    return fallos


if __name__ == "__main__":
    import sys
    sys.exit(main())
