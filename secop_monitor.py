#!/usr/bin/env python3
"""
Monitor de procesos SECOP II para Chronos Asociados
=====================================================

Consulta el dataset abierto oficial "SECOP II - Procesos de Contratación"
(Colombia Compra Eficiente, vía datos.gov.co / Socrata) y reporta los procesos
VIGENTES que cumplan CUALQUIERA de estos dos criterios independientes:

  1. AERONÁUTICA CIVIL — todos los procesos publicados por la Unidad
     Administrativa Especial de Aeronáutica Civil, identificada por su NIT
     899999059. Se busca por NIT, no por palabras clave.

  2. ENERGÍA SOLAR Y ALTERNATIVAS — procesos de CUALQUIER entidad del país
     relacionados con paneles solares, energía solar/fotovoltaica y energías
     alternativas o similares (limpia, verde, renovable, sostenible, eólica,
     biomasa, etc.). Se busca por palabras clave en el nombre y la descripción.

"Vigente" significa: fecha límite de recepción aún no vencida Y el proceso no
figura como adjudicado, cancelado, suspendido, desierto o terminado.

Este script corre en un entorno con acceso normal a internet (GitHub Actions,
tu computador, un servidor) — NO dentro del sandbox de Claude. En producción
corre vía GitHub Actions, los martes y viernes.

Uso:
    pip install requests
    python secop_monitor.py                 # imprime el resumen en pantalla
    python secop_monitor.py --json out.json # además guarda el JSON crudo

Para que además ENVÍE el correo define estas variables de entorno:
    GMAIL_ADDRESS        cuenta de Gmail que envía
    GMAIL_APP_PASSWORD   "contraseña de aplicación" (no la clave normal)
    DEST_EMAIL           a quién se le envía
Si no están definidas, solo imprime (útil para probar en local).
"""

import argparse
import datetime
import html
import json
import os
import re
import smtplib
import sys
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

DATASET_URL = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
URL_BUSQUEDA_SECOP = "https://community.secop.gov.co/Public/Tendering/ContractNoticeManagement/Index"

# Etiquetas que espera la app de Base44 (no cambiar el texto).
CAT_AEROCIVIL = "Aeronáutica Civil"
CAT_SOLAR = "Energía Solar Fotovoltaica"

# ---------------------------------------------------------------------------
# Criterios de búsqueda (ajusta aquí si cambian)
# ---------------------------------------------------------------------------

# Criterio 1: Aerocivil, por NIT 899999059 (tal como viene en el dataset).
NIT_AEROCIVIL = "899999059"

# Valor mínimo (precio base, COP) por criterio. 0 = sin mínimo.
VALOR_MINIMO_AEROCIVIL = 0
VALOR_MINIMO_SOLAR = 500_000_000

# Criterio 2: pre-filtro AMPLIO que se envía al servidor (SoQL no distingue
# tildes ni límites de palabra, así que aquí se prefiere no perder nada).
# Luego Python aplica el filtro preciso (ver PATRONES_SOLAR_FUERTES).
PREFILTRO_SOLAR = [
    "%SOLAR%",
    "%FOTOVOLTAIC%",
    "%ENERG%LIMPIA%",
    "%ENERG%VERDE%",
    "%ENERG%ALTERN%",
    "%ENERG%RENOVABLE%",
    "%ENERG%SOSTENIBLE%",
    "%ENERG%CONVENCIONAL%",
    "%FUENTE%RENOVABLE%",
    "%EOLIC%",
    "%EÓLIC%",
    "%AEROGENERADOR%",
    "%FNCER%",
    "%BIOMASA%",
    "%BIOGAS%",
    "%BIOGÁS%",
    "%GEOTERMIC%",
    "%GEOTÉRMIC%",
    "%HIDROGENO%VERDE%",
    "%HIDRÓGENO%VERDE%",
]

# Filtro preciso (sobre texto SIN tildes y en mayúsculas, con límites de
# palabra). "SOLARWINDS" ya NO coincide con "SOLAR".
_ENERGIA = r"ENERGI(?:A|AS)"
_TIPOS_ENERGIA = (
    r"(?:SOLAR(?:ES)?|LIMPIAS?|VERDES?|ALTERNATIVAS?|ALTERNAS?|RENOVABLES?|"
    r"SOSTENIBLES?|EOLICAS?|NO\s+CONVENCIONALES?)"
)
PATRONES_SOLAR_FUERTES = [
    re.compile(p)
    for p in (
        r"\bFOTOVOLTAIC\w*",
        r"\bPANEL(?:ES)?\s+(?:\w+\s+){0,2}SOLAR(?:ES)?\b",
        rf"\b{_ENERGIA}\s+(?:\w+\s+){{0,2}}{_TIPOS_ENERGIA}\b",
        r"\bFUENTES?\s+(?:\w+\s+){0,2}RENOVABLES?\b",
        r"\bFUENTES?\s+NO\s+CONVENCIONALES?\b",
        r"\bFNCER\b",
        r"\bEOLIC\w*",
        r"\bAEROGENERADOR\w*",
        r"\bBIOMASA\b",
        r"\bBIOGAS\b",
        r"\bGEOTERMIC\w*",
        r"\bHIDROGENO\s+VERDE\b",
        r"\bTERMOSOLAR\w*",
        r"\bSISTEMAS?\s+(?:\w+\s+){0,3}SOLAR(?:ES)?\b",
    )
]
# "SOLAR" suelto es ambiguo (también significa "lote de terreno"): se acepta si
# hay contexto de energía; se rechaza si solo hay contexto de terreno.
_RE_SOLAR_SUELTO = re.compile(r"\bSOLAR(?:ES)?\b")
_CONTEXTO_ENERGIA = (
    "ENERG", "PANEL", "FOTOVOLT", "ELECTR", "BOMBE", "LUMINARIA", "ALUMBRADO",
    "ILUMINAC", "CALENTADOR", "GENERAC", "INVERSOR", "CELDA", "LAMPARA", "KW",
    "SISTEMA",
)
_CONTEXTO_TERRENO = ("LOTE", "TERRENO", "PREDIO", "INMUEBLE", "FINCA", "URBANIZ", "CASA ")

# Departamentos de interés, en el orden en que se muestran en el correo
# (Arauca primero). Los demás departamentos salen después, en orden alfabético.
DEPARTAMENTOS_PRIORITARIOS = [
    "Arauca", "Vichada", "Casanare", "Guaviare", "Guainía", "Meta",
    "Boyacá", "Santander", "Norte de Santander", "Cundinamarca",
]

# Anclas de ubicación: municipios, aeropuertos y nombres del departamento. SOLO
# se usan para entidades NACIONALES (o registradas en Bogotá, como Aerocivil),
# que ejecutan obras en regiones. Las entidades territoriales (gobernaciones,
# alcaldías, etc.) siempre usan su propio departamento. La coincidencia es por
# palabra completa y gana la ancla que aparece primero en el texto.
# Se evitaron nombres ambiguos ("Meta" suelto, "Vanguardia", "Miraflores"...).
# Aeropuertos tomados del listado de aeropuertos de Colombia; complétalo si falta alguno.
MUNICIPIOS_POR_DEPARTAMENTO = {
    "Arauca": [
        "Arauca", "Arauquita", "El Troncal", "Cravo Norte", "Fortul", "Puerto Rondón", "Saravena",
        "Tame", "Santiago Pérez", "Los Colonizadores", "Gustavo Vargas",
    ],
    "Vichada": ["Vichada", "Puerto Carreño", "Germán Olano", "Cumaribo"],
    "Casanare": ["Casanare", "Yopal", "El Alcaraván", "Paz de Ariporo", "Támara", "Trinidad", "Hato Corozal"],
    "Guaviare": ["Guaviare", "San José del Guaviare", "Jorge Enrique González", "El Retorno"],
    "Guainía": ["Guainía", "Inírida", "César Gaviria Trujillo"],
    "Meta": [
        "Departamento del Meta", "(Meta)", "Villavicencio", "Aeropuerto Vanguardia", "San Martín de los Llanos",
        "Aeropuerto San Martín", "La Macarena", "Puerto Gaitán", "Mapiripán", "Puerto López",
    ],
    "Boyacá": ["Boyacá", "Tunja", "Paipa", "Duitama", "Sogamoso", "Puerto Boyacá"],
    "Santander": [
        "Departamento de Santander", "(Santander)", "Bucaramanga", "Palonegro", "Lebrija", "Barrancabermeja",
        "Yariguíes", "San Gil", "Floridablanca", "Piedecuesta", "Girón", "Cimitarra",
    ],
    "Norte de Santander": [
        "Norte de Santander", "Cúcuta", "Camilo Daza", "Ocaña", "Aguas Claras", "Tibú", "Pamplona",
        "Villa del Rosario",
    ],
    "Cundinamarca": [
        "Cundinamarca", "Girardot", "Santiago Vila", "Guaymaral", "Facatativá", "Zipaquirá", "Fusagasugá", "Soacha",
    ],
}

# ---------------------------------------------------------------------------
# Utilidades de texto
# ---------------------------------------------------------------------------

_MAPA_TILDES = str.maketrans("ÁÉÍÓÚÜÑ", "AEIOUUN")


def _norm(texto) -> str:
    """Mayúsculas y sin tildes (conserva el largo del texto)."""
    return (texto or "").upper().translate(_MAPA_TILDES)


# El dataset pega al número y al nombre del proceso el texto de la fase, p.ej.
# "SDE-LP-2026-0120 (Fase de Selección (Presentación de ofertas))", y a veces
# lo corta a mitad de palabra. Se corta desde el primer paréntesis que abre con
# una palabra de fase.
_RE_FASE = re.compile(
    r"\s*\(\s*(?:MANIFESTACI|PRESENTACI|FASE\b|EVALUACI|PLANEACI|SELECCI|BORRADOR|MENOR CUANT|INVITACI)"
)


def limpiar_fase(texto) -> str:
    t = (texto or "").strip()
    m = _RE_FASE.search(_norm(t))
    if m:
        t = t[: m.start()]
    return t.strip(" -–—.;:,")


_NOMBRES_GENERICOS = {
    "SUBASTA INVERSA", "SUBASTA INVERSA ELECTRONICA", "LICITACION PUBLICA",
    "SELECCION ABREVIADA", "CONTRATACION DIRECTA", "CONCURSO DE MERITOS",
    "MINIMA CUANTIA", "MENOR CUANTIA", "INVITACION PUBLICA",
}


def _referencia(p: dict) -> str:
    return limpiar_fase(p.get("referencia_del_proceso") or p.get("id_del_proceso")) or "(sin número)"


def objeto_y_detalle(p: dict) -> tuple[str, str]:
    """(objeto principal, detalle). Si el nombre es genérico o es solo el
    número del proceso, el objeto real está en la descripción."""
    nombre = limpiar_fase(p.get("nombre_del_procedimiento"))
    desc = limpiar_fase(p.get("descripci_n_del_procedimiento"))
    ref_n = _norm(_referencia(p))
    nombre_n = _norm(nombre)
    generico = (not nombre) or nombre_n == ref_n or nombre_n in _NOMBRES_GENERICOS
    principal = desc if (generico and desc) else (nombre or desc)
    detalle = ""
    if desc and _norm(desc)[:60] != _norm(principal)[:60]:
        detalle = desc
    if len(detalle) > 300:
        detalle = detalle[:297].rstrip() + "..."
    return principal, detalle


def _url_proceso(p: dict) -> str:
    u = p.get("urlproceso")
    if isinstance(u, dict):
        u = u.get("url", "")
    u = (u or "").strip()
    if not u or "/Users/Login" in u:  # algunos procesos traen la página de login
        return ""
    return u


def _valor(p: dict) -> float:
    try:
        return float(p.get("precio_base") or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_cop(v: float) -> str:
    return "No informado" if not v else "$" + f"{int(v):,}".replace(",", ".")


def _texto_proceso(p: dict) -> str:
    return _norm(
        " ".join([p.get("nombre_del_procedimiento") or "", p.get("descripci_n_del_procedimiento") or ""])
    )


# ---------------------------------------------------------------------------
# Consulta al dataset
# ---------------------------------------------------------------------------

LIMITE_PAGINA = 500
MAX_PAGINAS = 6


def _get_json(params: dict) -> list[dict]:
    ultimo = None
    for intento in range(3):
        try:
            resp = requests.get(DATASET_URL, params=params, timeout=60)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            ultimo = e
            time.sleep(2 * (intento + 1))
    raise ultimo


def _consultar(where: str) -> list[dict]:
    """Trae TODAS las páginas (no se corta en 500). No usa $select a propósito:
    así los campos de estado llegan completos y un nombre de columna incorrecto
    no rompe la consulta."""
    filas: list[dict] = []
    for pagina in range(MAX_PAGINAS):
        params = {
            "$where": where,
            "$order": "fecha_de_recepcion_de ASC, id_del_proceso ASC",
            "$limit": str(LIMITE_PAGINA),
            "$offset": str(pagina * LIMITE_PAGINA),
        }
        lote = _get_json(params)
        filas.extend(lote)
        if len(lote) < LIMITE_PAGINA:
            break
    else:
        print(f"[aviso] Se alcanzó el máximo de {MAX_PAGINAS} páginas; puede haber más resultados.")
    return filas


def _fecha_iso(d: datetime.date) -> str:
    return f"{d.isoformat()}T00:00:00.000"


def where_aerocivil(hoy: datetime.date) -> str:
    w = f"nit_entidad like '{NIT_AEROCIVIL}%' AND fecha_de_recepcion_de >= '{_fecha_iso(hoy)}'"
    if VALOR_MINIMO_AEROCIVIL:
        w += f" AND precio_base >= {VALOR_MINIMO_AEROCIVIL}"
    return w


def where_solar(hoy: datetime.date) -> str:
    ors = []
    for pat in PREFILTRO_SOLAR:
        ors.append(f"upper(nombre_del_procedimiento) like '{pat}'")
        ors.append(f"upper(descripci_n_del_procedimiento) like '{pat}'")
    return (
        f"( {' OR '.join(ors)} ) "
        f"AND precio_base >= {VALOR_MINIMO_SOLAR} "
        f"AND fecha_de_recepcion_de >= '{_fecha_iso(hoy)}'"
    )


# ---------------------------------------------------------------------------
# Filtros precisos
# ---------------------------------------------------------------------------

_ESTADOS_NO_VIGENTES = (
    "ADJUDIC", "CANCEL", "SUSPEND", "DESIERT", "TERMINAD", "CELEBRAD",
    "LIQUIDAD", "REVOCAD", "DESCARTAD", "ANULAD",
)


def esta_vigente(p: dict, hoy: datetime.date) -> bool:
    """Fecha límite no vencida y sin estado de cierre (adjudicado, cancelado...)."""
    fecha = (p.get("fecha_de_recepcion_de") or "")[:10]
    try:
        if not fecha or datetime.date.fromisoformat(fecha) < hoy:
            return False
    except ValueError:
        return False
    if _norm(p.get("adjudicado")).strip() in ("SI", "TRUE"):
        return False
    for campo in ("estado_del_procedimiento", "estado_resumen", "fase"):
        valor = _norm(p.get(campo))
        if any(x in valor for x in _ESTADOS_NO_VIGENTES):
            return False
    return True


def coincide_solar(texto_norm: str) -> bool:
    if any(pat.search(texto_norm) for pat in PATRONES_SOLAR_FUERTES):
        return True
    if _RE_SOLAR_SUELTO.search(texto_norm):
        if any(c in texto_norm for c in _CONTEXTO_ENERGIA):
            return True
        if any(c in texto_norm for c in _CONTEXTO_TERRENO):
            return False
        return True
    return False


def es_aerocivil(p: dict) -> bool:
    digitos = re.sub(r"\D", "", p.get("nit_entidad") or "")
    return digitos.startswith(NIT_AEROCIVIL)


def obtener_aerocivil(hoy: datetime.date) -> list[dict]:
    filas = _consultar(where_aerocivil(hoy))
    return [
        p for p in filas
        if es_aerocivil(p) and esta_vigente(p, hoy) and _valor(p) >= VALOR_MINIMO_AEROCIVIL
    ]


def obtener_solar(hoy: datetime.date) -> list[dict]:
    filas = _consultar(where_solar(hoy))
    return [
        p for p in filas
        if coincide_solar(_texto_proceso(p)) and esta_vigente(p, hoy) and _valor(p) >= VALOR_MINIMO_SOLAR
    ]


# ---------------------------------------------------------------------------
# Clasificación, deduplicación y orden
# ---------------------------------------------------------------------------

_DEPTOS_NACIONALES = ("DISTRITO CAPITAL DE BOGOTA", "BOGOTA", "")


def _es_entidad_nacional(p: dict) -> bool:
    orden = _norm(p.get("ordenentidad")).strip()
    depto = _norm(p.get("departamento_entidad")).strip()
    return orden.startswith("NACIONAL") or depto in _DEPTOS_NACIONALES


def _patron_ancla(nombre: str) -> "re.Pattern":
    # (?<!\w)/(?!\w) en vez de \b para soportar anclas como "(Meta)".
    return re.compile(r"(?<!\w)" + re.escape(_norm(nombre)) + r"(?!\w)")


_PATRONES_ANCLA = {
    depto: [_patron_ancla(n) for n in nombres] for depto, nombres in MUNICIPIOS_POR_DEPARTAMENTO.items()
}


def clasificar_departamento(p: dict) -> str:
    """Departamento de la entidad. Solo para entidades nacionales se busca una
    ancla (municipio, aeropuerto o nombre del departamento) en el texto, por
    palabra completa, para ubicar la obra; si hay varias, gana la que aparece
    primero en el texto."""
    if _es_entidad_nacional(p):
        texto = _texto_proceso(p)
        mejor, pos_mejor = None, None
        for depto, patrones in _PATRONES_ANCLA.items():
            for pat in patrones:
                m = pat.search(texto)
                if m and (pos_mejor is None or m.start() < pos_mejor):
                    mejor, pos_mejor = depto, m.start()
        if mejor:
            return mejor
    return (p.get("departamento_entidad") or "").strip() or "Sin departamento (Nacional)"


def _clave_dedup(p: dict) -> tuple:
    ref = limpiar_fase(p.get("referencia_del_proceso"))
    if ref:
        return ("ref", p.get("nit_entidad") or p.get("entidad"), ref)
    return ("entidad_nombre", p.get("entidad"), (p.get("nombre_del_procedimiento") or "")[:80])


_ORDEN_FASE = ["borrador", "planeación", "manifestación de interés", "presentación de oferta", "evaluación", "adjudicación"]


def _prioridad_fase(p: dict) -> int:
    fase = (p.get("fase") or "").strip().lower()
    for i, palabra in enumerate(_ORDEN_FASE):
        if palabra in fase:
            return i
    return len(_ORDEN_FASE) // 2


def deduplicar(procesos: list[dict]) -> list[dict]:
    """Colapsa registros del mismo proceso, dejando la fase más avanzada."""
    vistos: dict = {}
    for p in procesos:
        clave = _clave_dedup(p)
        actual = vistos.get(clave)
        if actual is None or (_prioridad_fase(p), p.get("fecha_de_recepcion_de") or "") > (
            _prioridad_fase(actual), actual.get("fecha_de_recepcion_de") or ""
        ):
            vistos[clave] = p
    return list(vistos.values())


def _agrupar_por_departamento(procesos: list[dict]) -> dict[str, list[dict]]:
    por_depto: dict[str, list[dict]] = {}
    for p in procesos:
        por_depto.setdefault(clasificar_departamento(p), []).append(p)
    for items in por_depto.values():
        items.sort(key=lambda p: p.get("fecha_de_recepcion_de") or "9999")
    return por_depto


_CATCHALL = "Sin departamento (Nacional)"


def _es_prioritario(depto: str) -> bool:
    return _norm(depto) in {_norm(d) for d in DEPARTAMENTOS_PRIORITARIOS}


def _orden_departamentos(por_depto: dict) -> list[str]:
    """Departamentos de interés (en el orden de DEPARTAMENTOS_PRIORITARIOS),
    luego los demás en orden alfabético, y al final el catch-all."""
    rango = {_norm(d): i for i, d in enumerate(DEPARTAMENTOS_PRIORITARIOS)}
    prioritarios = sorted((d for d in por_depto if _norm(d) in rango), key=lambda d: rango[_norm(d)])
    otros = sorted(d for d in por_depto if _norm(d) not in rango and d != _CATCHALL)
    return prioritarios + otros + ([_CATCHALL] if _CATCHALL in por_depto else [])


def _dias_restantes(p: dict, hoy: datetime.date):
    fecha = (p.get("fecha_de_recepcion_de") or "")[:10]
    try:
        return fecha, (datetime.date.fromisoformat(fecha) - hoy).days
    except ValueError:
        return fecha, None


# ---------------------------------------------------------------------------
# Salidas: Base44, texto y HTML
# ---------------------------------------------------------------------------

def a_registro_base44(p: dict, categoria: str) -> dict:
    """Formato exacto que espera la entidad ProcesoSecop de la app de Base44."""
    objeto, _ = objeto_y_detalle(p)
    return {
        "numero_proceso": _referencia(p),
        "entidad": p.get("entidad") or "",
        "objeto": objeto,
        "valor": _valor(p),
        "fecha_limite": (p.get("fecha_de_recepcion_de") or "")[:10] or None,
        "link": _url_proceso(p) or URL_BUSQUEDA_SECOP,
        "region": clasificar_departamento(p),
        "categoria": categoria,
        "estado": "Abierto",
    }


ETIQUETAS = {"aero": "Aerocivil", "solar": "Solar / energías"}
COLORES = {"aero": "#1d4ed8", "solar": "#b45309"}


def _cuenta(procesos: list[dict]) -> tuple[int, int]:
    n_aero = sum(1 for p in procesos if p.get("_cat") == "aero")
    return n_aero, len(procesos) - n_aero


def formatear_resumen(procesos: list[dict], hoy: datetime.date, avisos: list[str]) -> str:
    """Texto plano, organizado por DEPARTAMENTO (Arauca primero)."""
    n_aero, n_solar = _cuenta(procesos)
    lineas = [
        f"SECOP - Oportunidades abiertas del {hoy.isoformat()} "
        f"({len(procesos)} procesos: Aerocivil {n_aero} · Solar/energías {n_solar})"
    ]
    for a in avisos:
        lineas.append(f"\n[ATENCIÓN] {a}")
    if not procesos:
        lineas.append("\nNo hay procesos vigentes hoy que cumplan los criterios.")
        return "\n".join(lineas)
    por_depto = _agrupar_por_departamento(procesos)
    orden = _orden_departamentos(por_depto)
    lineas.append("\nPor departamento: " + " · ".join(f"{d} ({len(por_depto[d])})" for d in orden))
    hubo_prioritario = mostro_otros = False
    for depto in orden:
        items = por_depto[depto]
        if _es_prioritario(depto):
            hubo_prioritario = True
        elif hubo_prioritario and not mostro_otros:
            mostro_otros = True
            lineas.append("\n---------- OTROS DEPARTAMENTOS ----------")
        lineas.append(f"\n=== {depto.upper()} ({len(items)}) ===")
        for p in items:
            fecha, dias = _dias_restantes(p, hoy)
            objeto, detalle = objeto_y_detalle(p)
            ubic = p.get("ciudad_entidad") or p.get("departamento_entidad") or "s/d"
            etiqueta = ETIQUETAS.get(p.get("_cat"), "")
            lineas.append(
                f"- [{etiqueta}] Nº proceso: {_referencia(p)}\n"
                f"  Entidad: {p.get('entidad')} ({ubic})\n"
                f"  Objeto: {objeto}\n"
                + (f"  Detalle: {detalle}\n" if detalle else "")
                + f"  Valor: {_fmt_cop(_valor(p))} | Cierra: {fecha} ({dias if dias is not None else '?'} días) | "
                f"Modalidad: {p.get('modalidad_de_contratacion')} | Fase: {p.get('fase')}\n"
                f"  Link: {_url_proceso(p) or 'Sin enlace directo — buscar la referencia en ' + URL_BUSQUEDA_SECOP}"
            )
    return "\n".join(lineas)


def formatear_resumen_html(procesos: list[dict], hoy: datetime.date, avisos: list[str]) -> str:
    """HTML organizado por DEPARTAMENTO (Arauca primero); dentro de cada uno,
    por fecha de cierre, con una etiqueta que indica el criterio."""
    e = html.escape
    st_tabla = "width:100%;border-collapse:collapse;margin:0 0 24px 0;font-family:Arial,sans-serif;font-size:13px;"
    st_th = "text-align:left;padding:6px 8px;background:#1f2937;color:#ffffff;border:1px solid #d1d5db;"
    st_td = "padding:6px 8px;border:1px solid #d1d5db;vertical-align:top;"
    gris = "color:#6b7280;"

    n_aero, n_solar = _cuenta(procesos)
    partes = [
        "<html><body style='font-family:Arial,sans-serif;color:#111827;'>",
        "<h1 style='margin-bottom:4px;font-size:20px;'>SECOP - Oportunidades abiertas</h1>",
        f"<p style='margin-top:0;{gris}'>{hoy.isoformat()} &middot; {len(procesos)} procesos "
        f"(Aerocivil {n_aero} &middot; Solar/energías {n_solar})</p>",
    ]
    for a in avisos:
        partes.append(
            "<p style='background:#fef2f2;border:1px solid #fecaca;padding:8px;color:#991b1b;'>"
            f"<b>Atención:</b> {e(a)}</p>"
        )
    if not procesos:
        partes.append("<p>No hay procesos vigentes hoy que cumplan los criterios.</p></body></html>")
        return "\n".join(partes)

    por_depto = _agrupar_por_departamento(procesos)
    orden = _orden_departamentos(por_depto)
    partes.append(
        f"<p style='font-size:13px;'><b>Por departamento:</b> "
        + " &middot; ".join(f"{e(d)} ({len(por_depto[d])})" for d in orden)
        + "</p>"
    )

    hubo_prioritario = mostro_otros = False
    for depto in orden:
        items = por_depto[depto]
        if _es_prioritario(depto):
            hubo_prioritario = True
        elif hubo_prioritario and not mostro_otros:
            mostro_otros = True
            partes.append(
                f"<p style='{gris}font-size:12px;text-transform:uppercase;letter-spacing:1px;"
                "border-top:2px solid #9ca3af;padding-top:12px;margin-top:32px;'>Otros departamentos</p>"
            )
        partes.append(
            f"<h2 style='background:#e5e7eb;padding:8px;margin:24px 0 8px 0;font-size:16px;'>"
            f"{e(depto)} <span style='{gris}font-weight:normal;'>({len(items)})</span></h2>"
        )
        partes.append(f"<table style='{st_tabla}'>")
        partes.append(
            "<tr>"
            f"<th style='{st_th}'>Proceso / Entidad</th>"
            f"<th style='{st_th}'>Objeto</th>"
            f"<th style='{st_th}'>Valor</th>"
            f"<th style='{st_th}'>Modalidad / Fase</th>"
            f"<th style='{st_th}'>Cierra</th>"
            f"<th style='{st_th}'>Ver</th>"
            "</tr>"
        )
        for p in items:
            fecha, dias = _dias_restantes(p, hoy)
            objeto, detalle = objeto_y_detalle(p)
            ubic = p.get("ciudad_entidad") or p.get("departamento_entidad") or "s/d"
            urgente = dias is not None and dias <= 5
            st_dias = "color:#b91c1c;font-weight:bold;" if urgente else gris
            cat = p.get("_cat")
            badge = (
                f"<span style='background:{COLORES.get(cat, '#374151')};color:#ffffff;font-size:11px;"
                f"padding:1px 6px;border-radius:3px;'>{e(ETIQUETAS.get(cat, ''))}</span><br>"
            )
            url = _url_proceso(p)
            enlace = (
                f"<a href='{e(url, quote=True)}'>Abrir</a>"
                if url
                else f"<a href='{e(URL_BUSQUEDA_SECOP, quote=True)}'>Buscar</a>"
                f"<br><span style='{gris}font-size:11px;'>sin enlace directo</span>"
            )
            partes.append(
                "<tr>"
                f"<td style='{st_td}'>{badge}<b>{e(_referencia(p))}</b><br>{e(p.get('entidad') or '')}"
                f"<br><span style='{gris}'>{e(ubic)}</span></td>"
                f"<td style='{st_td}'>{e(objeto)}"
                + (f"<br><span style='{gris}font-size:12px;'>{e(detalle)}</span>" if detalle else "")
                + "</td>"
                f"<td style='{st_td}white-space:nowrap;'>{e(_fmt_cop(_valor(p)))}</td>"
                f"<td style='{st_td}'>{e(p.get('modalidad_de_contratacion') or '')}"
                f"<br><span style='{gris}'>{e(p.get('fase') or '')}</span></td>"
                f"<td style='{st_td}white-space:nowrap;'>{e(fecha)}"
                f"<br><span style='{st_dias}'>{dias if dias is not None else '?'} días</span></td>"
                f"<td style='{st_td}'>{enlace}</td>"
                "</tr>"
            )
        partes.append("</table>")
    partes.append("</body></html>")
    return "\n".join(partes)


def enviar_por_correo(asunto: str, cuerpo_texto: str, cuerpo_html: str) -> None:
    remitente = os.environ.get("GMAIL_ADDRESS")
    clave_app = os.environ.get("GMAIL_APP_PASSWORD")
    destinatario = os.environ.get("DEST_EMAIL")
    if not (remitente and clave_app and destinatario):
        print("[info] Variables de correo no definidas — no se envía correo, solo se imprime.")
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = destinatario
    msg.attach(MIMEText(cuerpo_texto, "plain", "utf-8"))
    msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
        servidor.login(remitente, clave_app)
        servidor.sendmail(remitente, [destinatario], msg.as_string())
    print(f"[ok] Correo enviado a {destinatario}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="Ruta opcional para guardar el JSON crudo de resultados")
    ap.add_argument(
        "--base44-json",
        default="secop_data.json",
        help="Ruta del resumen listo para la app de Base44 (por defecto secop_data.json).",
    )
    args = ap.parse_args()

    # Fecha en hora de Colombia (en GitHub Actions el reloj está en UTC).
    hoy = datetime.datetime.now(ZoneInfo("America/Bogota")).date()

    avisos: list[str] = []
    aero: list[dict] = []
    solar: list[dict] = []
    ok_aero = ok_solar = True
    try:
        aero = deduplicar(obtener_aerocivil(hoy))
    except Exception as exc:  # noqa: BLE001
        ok_aero = False
        avisos.append(f"No se pudo consultar el criterio 1 (Aerocivil): {exc}")
    try:
        solar = deduplicar(obtener_solar(hoy))
    except Exception as exc:  # noqa: BLE001
        ok_solar = False
        avisos.append(f"No se pudo consultar el criterio 2 (energía solar/alternativas): {exc}")

    # Un proceso que cumple ambos criterios se muestra una sola vez, como Aerocivil.
    claves_aero = {_clave_dedup(p) for p in aero}
    solar = [p for p in solar if _clave_dedup(p) not in claves_aero]

    for p in aero:
        p["_cat"] = "aero"
    for p in solar:
        p["_cat"] = "solar"
    todos = aero + solar

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)

    # El JSON de Base44 solo se reescribe si AMBAS consultas funcionaron, para
    # no borrar los datos buenos con un resultado vacío por una falla de red.
    if ok_aero and ok_solar:
        registros = [
            a_registro_base44(p, CAT_AEROCIVIL if p["_cat"] == "aero" else CAT_SOLAR) for p in todos
        ]
        with open(args.base44_json, "w", encoding="utf-8") as f:
            json.dump(
                {"generado": hoy.isoformat(), "total": len(registros), "procesos": registros},
                f, ensure_ascii=False, indent=2,
            )

    texto = formatear_resumen(todos, hoy, avisos)
    cuerpo_html = formatear_resumen_html(todos, hoy, avisos)
    print(texto)

    asunto = f"SECOP - Oportunidades del {hoy.isoformat()} (Aerocivil: {len(aero)} · Solar/energías: {len(solar)})"
    if avisos:
        asunto = "[CON ERRORES] " + asunto
    enviar_por_correo(asunto, texto, cuerpo_html)

    if avisos:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
