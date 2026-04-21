"""CHEQ-OK · Backend v0.3.0 — auth multiusuario + historial en Supabase"""

import asyncio
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from supabase import create_client, Client

from config import get_config, set_config

# ===================================================================
# CONFIGURACIÓN BCRA
# ===================================================================
BCRA_BASE = "https://api.bcra.gob.ar/centraldedeudores/v1.0/Deudas"
HTTP_TIMEOUT = 30.0
HTTP_REINTENTOS_READERROR = 8
HTTP_REINTENTOS_OTROS = 4
CACHE_TTL_SEGUNDOS = 24 * 3600

_CACHE: dict[tuple, tuple[float, Optional[dict]]] = {}
_ULTIMA_CONSULTA_BCRA = 0.0
_DELAY_ENTRE_CONSULTAS = 0.3
_LOCK_BCRA = asyncio.Lock()

# ===================================================================
# CONFIGURACIÓN DE AUTENTICACIÓN
# ===================================================================
# Los usuarios se configuran como variables de entorno en Render con prefijo USER_
# Ejemplo: USER_BRUNO_SARTOR=ZggA-PB5v-nvJv
# El usuario admin es el que tenga su nombre en la variable ADMIN_USER.

JWT_SECRET = os.environ.get("JWT_SECRET", "cambiar-en-produccion")
JWT_EXPIRACION_HORAS = 12
ADMIN_USER = os.environ.get("ADMIN_USER", "administrador")


def cargar_usuarios() -> dict[str, str]:
    """Lee las variables de entorno con prefijo USER_ y arma un diccionario
    usuario → clave. El nombre del usuario se obtiene sacando el prefijo USER_
    y reemplazando guiones bajos por puntos."""
    usuarios = {}
    for key, value in os.environ.items():
        if key.startswith("USER_"):
            # USER_BRUNO_SARTOR → Bruno.Sartor
            nombre_crudo = key[5:]  # saca USER_
            partes = nombre_crudo.split("_")
            # Capitaliza cada parte: BRUNO → Bruno
            nombre = ".".join(p.capitalize() for p in partes)
            usuarios[nombre] = value
    # Caso especial para administrador
    admin_pass = os.environ.get("ADMIN_PASSWORD")
    if admin_pass:
        usuarios[ADMIN_USER] = admin_pass
    return usuarios


# ===================================================================
# CLIENTE DE SUPABASE
# ===================================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print(f"ERROR conectando a Supabase: {e}")
        supabase = None


# ===================================================================
# FASTAPI
# ===================================================================
app = FastAPI(title="CHEQ-OK API", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ===================================================================
# MODELOS
# ===================================================================
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
    intentos: int = 1


class LoginBody(BaseModel):
    usuario: str
    clave: str


class LoginResponse(BaseModel):
    token: str
    usuario: str
    es_admin: bool


class ConfigUpdate(BaseModel):
    umbral_monto_sit2: Optional[float] = Field(None, ge=0)
    meses_rechazo_reciente: Optional[int] = Field(None, ge=0, le=120)


# ===================================================================
# AUTENTICACIÓN JWT
# ===================================================================
def crear_token(usuario: str) -> str:
    payload = {
        "usuario": usuario,
        "exp": datetime.now(tz=timezone.utc) + timedelta(hours=JWT_EXPIRACION_HORAS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def verificar_token(authorization: Optional[str] = Header(None)) -> str:
    """Devuelve el nombre de usuario si el token es válido, sino lanza 401."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Token faltante")
    token = authorization.replace("Bearer ", "").strip()
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        return payload["usuario"]
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Sesión expirada, volvé a iniciar sesión")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token inválido")


# ===================================================================
# ENDPOINTS PÚBLICOS
# ===================================================================
@app.get("/")
async def root():
    usuarios = cargar_usuarios()
    return {
        "servicio": "CHEQ-OK API",
        "version": "0.3.0",
        "cache_entries": len(_CACHE),
        "supabase_conectado": supabase is not None,
        "usuarios_cargados": len(usuarios),
        "endpoints": {
            "login": "POST /login",
            "evaluar": "GET /evaluar/{cuit} (requiere token)",
            "historial": "GET /historial (requiere token)",
        },
    }


@app.post("/login", response_model=LoginResponse)
async def login(body: LoginBody):
    usuarios = cargar_usuarios()
    if not usuarios:
        raise HTTPException(500, "No hay usuarios configurados en el servidor")
    clave_real = usuarios.get(body.usuario)
    if not clave_real or clave_real != body.clave:
        raise HTTPException(401, "Usuario o clave incorrectos")
    token = crear_token(body.usuario)
    return LoginResponse(
        token=token,
        usuario=body.usuario,
        es_admin=(body.usuario == ADMIN_USER),
    )


# ===================================================================
# CONSULTAS AL BCRA
# ===================================================================
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


async def consultar_bcra(endpoint: str, cuit: str) -> tuple[Optional[dict], bool, int]:
    global _ULTIMA_CONSULTA_BCRA
    hit, val = _cache_get(endpoint, cuit)
    if hit:
        return val, True, 0

    url = f"{BCRA_BASE}/{endpoint}/{cuit}" if endpoint else f"{BCRA_BASE}/{cuit}"
    ultimo_error = None
    intentos_readerror = 0
    intento = 0
    while intento < HTTP_REINTENTOS_READERROR:
        try:
            async with _LOCK_BCRA:
                elapsed = time.time() - _ULTIMA_CONSULTA_BCRA
                if elapsed < _DELAY_ENTRE_CONSULTAS:
                    await asyncio.sleep(_DELAY_ENTRE_CONSULTAS - elapsed)
                _ULTIMA_CONSULTA_BCRA = time.time()

            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, verify=False, follow_redirects=True) as client:
                r = await client.get(url, headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                })
            if r.status_code == 404:
                _cache_set(endpoint, cuit, None)
                return None, False, intento + 1
            if r.status_code == 200:
                datos = r.json().get("results")
                _cache_set(endpoint, cuit, datos)
                return datos, False, intento + 1
            if 500 <= r.status_code < 600:
                ultimo_error = f"BCRA respondió {r.status_code}"
                await asyncio.sleep(1 + intento)
                intento += 1
                continue
            raise HTTPException(502, f"BCRA devolvió status {r.status_code}")
        except httpx.TimeoutException:
            ultimo_error = f"timeout después de {HTTP_TIMEOUT}s"
            await asyncio.sleep(1 + intento)
            intento += 1
        except httpx.ReadError:
            ultimo_error = "conexión cortada por el BCRA"
            intentos_readerror += 1
            if intentos_readerror <= 3:
                await asyncio.sleep(0.5)
            elif intentos_readerror <= 5:
                await asyncio.sleep(1.5)
            else:
                await asyncio.sleep(3)
            intento += 1
        except Exception as exc:
            ultimo_error = f"{type(exc).__name__}: {str(exc)[:150]}"
            if intento >= HTTP_REINTENTOS_OTROS:
                break
            await asyncio.sleep(1 + intento * 2)
            intento += 1
    raise HTTPException(502, f"BCRA no disponible tras {intento} intentos. {ultimo_error}.")


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
            etiquetas = {3: "Situación 3 (Con problemas)", 4: "Situación 4 (Alto riesgo)", 5: "Situación 5 (Irrecuperable)", 6: "Situación 6"}
            motivos.append(MotivoDecision(categoria="situacion_crediticia", descripcion=f"{etiquetas.get(situacion, f'Situación {situacion}')} en {entidad_nombre}. Deuda: ${monto_pesos:,.0f}.", severidad="rojo"))
        elif situacion == 2 and monto_pesos > cfg["umbral_monto_sit2"]:
            motivos.append(MotivoDecision(categoria="situacion_crediticia", descripcion=f"Situación 2 en {entidad_nombre} con deuda ${monto_pesos:,.0f}, supera umbral.", severidad="rojo"))
        elif situacion == 2:
            motivos.append(MotivoDecision(categoria="situacion_crediticia", descripcion=f"Situación 2 en {entidad_nombre} por ${monto_pesos:,.0f} (dentro del umbral).", severidad="info"))
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
                    motivos.append(MotivoDecision(categoria="cheque_pendiente", descripcion=f"Cheque PENDIENTE nº {numero} en {nombre_entidad} ({desc_causal}) por ${monto:,.0f}.", severidad="rojo"))
                elif fecha_rechazo and fecha_rechazo >= fecha_limite:
                    motivos.append(MotivoDecision(categoria="cheque_reciente", descripcion=f"Cheque rechazado (regularizado) nº {numero} del {fecha_rechazo_str}.", severidad="rojo"))
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


# ===================================================================
# GRABACIÓN EN SUPABASE
# ===================================================================
async def grabar_consulta(usuario: str, cuit: str, resultado: Optional[Evaluacion], error: Optional[str] = None):
    """Graba la consulta en Supabase. Si supabase no está configurado, no hace nada."""
    if not supabase:
        return
    try:
        data = {
            "usuario": usuario,
            "cuit": cuit,
            "semaforo": resultado.semaforo if resultado else None,
            "denominacion": resultado.denominacion if resultado else None,
            "resumen": resultado.resumen if resultado else None,
            "motivos": [m.dict() for m in resultado.motivos] if resultado else None,
            "error": error,
            "tiempo_ms": resultado.tiempo_ms if resultado else None,
            "desde_cache": resultado.desde_cache if resultado else False,
        }
        # supabase-py es síncrono. Lo corremos en un thread para no bloquear.
        await asyncio.to_thread(supabase.table("consultas").insert(data).execute)
    except Exception as e:
        print(f"ERROR grabando en Supabase: {e}")


# ===================================================================
# ENDPOINTS AUTENTICADOS
# ===================================================================
@app.get("/evaluar/{cuit}", response_model=Evaluacion)
async def evaluar(cuit: str, usuario: str = Depends(verificar_token)):
    inicio = time.time()
    cuit_limpio = validar_cuit(cuit)
    cfg = get_config()
    try:
        (deudas, d_cache, d_intentos), (cheques, c_cache, c_intentos) = await asyncio.gather(
            consultar_bcra("", cuit_limpio),
            consultar_bcra("ChequesRechazados", cuit_limpio),
        )
    except HTTPException as e:
        # Aún así grabamos el error en el historial
        await grabar_consulta(usuario, cuit_limpio, None, error=e.detail)
        raise

    desde_cache = d_cache and c_cache
    tiempo_ms = int((time.time() - inicio) * 1000)
    intentos_total = max(d_intentos, c_intentos)
    motivos = []
    denominacion = None

    if deudas is None and cheques is None:
        resultado = Evaluacion(
            cuit=cuit_limpio, denominacion=None, semaforo="VERDE",
            motivos=[MotivoDecision(categoria="sin_antecedentes", descripcion="El CUIT no figura en la Central de Deudores del BCRA.", severidad="info")],
            resumen="VERDE · Sin antecedentes en BCRA.",
            consultado_en=datetime.now(), config_aplicada=cfg,
            desde_cache=desde_cache, tiempo_ms=tiempo_ms, intentos=intentos_total,
        )
        await grabar_consulta(usuario, cuit_limpio, resultado)
        return resultado

    if deudas:
        denominacion = deudas.get("denominacion")
        motivos.extend(evaluar_situacion_crediticia(deudas, cfg))
    if cheques:
        if not denominacion:
            denominacion = cheques.get("denominacion")
        motivos.extend(evaluar_cheques_rechazados(cheques, cfg))

    hay_rojo = any(m.severidad == "rojo" for m in motivos)
    semaforo = "ROJO" if hay_rojo else "VERDE"
    resumen = f"ROJO · {len(motivos)} motivo(s) detectado(s)." if semaforo == "ROJO" else "VERDE · No se detectaron alertas."

    resultado = Evaluacion(
        cuit=cuit_limpio, denominacion=denominacion, semaforo=semaforo,
        motivos=motivos, resumen=resumen,
        detalle_deudas=deudas, detalle_cheques_rechazados=cheques,
        consultado_en=datetime.now(), config_aplicada=cfg,
        desde_cache=desde_cache, tiempo_ms=tiempo_ms, intentos=intentos_total,
    )
    await grabar_consulta(usuario, cuit_limpio, resultado)
    return resultado


@app.get("/historial")
async def historial(
    usuario: str = Depends(verificar_token),
    limit: int = 100,
    filtro_usuario: Optional[str] = None,
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
):
    """Devuelve el historial de consultas.
    - Si es admin, puede ver todas y filtrar por usuario/fecha.
    - Si es vendedor, solo ve sus propias consultas de los últimos 30 días."""
    if not supabase:
        raise HTTPException(500, "Supabase no configurado")

    es_admin = (usuario == ADMIN_USER)
    try:
        query = supabase.table("consultas").select("*").order("created_at", desc=True).limit(limit)
        if es_admin:
            if filtro_usuario:
                query = query.eq("usuario", filtro_usuario)
            if desde:
                query = query.gte("created_at", desde)
            if hasta:
                query = query.lte("created_at", hasta)
        else:
            # Vendedor: solo sus propias consultas de últimos 30 días
            fecha_desde = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
            query = query.eq("usuario", usuario).gte("created_at", fecha_desde)

        result = await asyncio.to_thread(query.execute)
        return {
            "usuario_consultante": usuario,
            "es_admin": es_admin,
            "consultas": result.data,
            "total": len(result.data),
        }
    except Exception as e:
        raise HTTPException(500, f"Error consultando historial: {str(e)[:200]}")


@app.get("/historial/usuarios")
async def historial_usuarios(usuario: str = Depends(verificar_token)):
    """Devuelve la lista de usuarios que han hecho consultas (solo admin)."""
    if usuario != ADMIN_USER:
        raise HTTPException(403, "Solo el administrador puede ver esto")
    if not supabase:
        raise HTTPException(500, "Supabase no configurado")
    try:
        result = await asyncio.to_thread(supabase.table("consultas").select("usuario").execute)
        usuarios_set = sorted(set(r["usuario"] for r in result.data))
        return {"usuarios": usuarios_set}
    except Exception as e:
        raise HTTPException(500, f"Error: {str(e)[:200]}")


# ===================================================================
# CONFIG
# ===================================================================
@app.get("/config")
async def ver_config():
    return get_config()


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
