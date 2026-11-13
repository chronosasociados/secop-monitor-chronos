#!/usr/bin/env python3
"""
Monitor de procesos SECOP II para Chronos Asociados
=====================================================

Consulta el dataset abierto oficial "SECOP II - Procesos de Contratación"
(Colombia Compra Eficiente, vía datos.gov.co / Socrata) y reporta los procesos
VIGENTES que cumplan CUALQUIERA de estos tres criterios independientes:

  1. AERONÁUTICA CIVIL — todos los procesos publicados por la Unidad
     Administrativa Especial de Aeronáutica Civil, identificada por su NIT
     899999059. Se busca por NIT, no por palabras clave.

  2. ENERGÍA SOLAR Y ALTERNATIVAS — procesos de CUALQUIER entidad del país
     relacionados con paneles solares, energía solar/fotovoltaica y energías
     alternativas o similares (limpia, verde, renovable, sostenible, eólica,
     biomasa, etc.). Se busca por palabras clave en el nombre y la descripción.

  3. ENTIDADES REGIONALES — todos los procesos, sin importar el objeto, de
     las entidades de Arauca, Casanare, Vichada, Guaviare y Boyacá (alcaldías,
     gobernaciones, descentralizadas y entidades nacionales con sede allí),
     con valor superior a $350 millones en Arauca y a $450 millones en los
     otros cuatro departamentos.

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

CAT_REGIONAL = "Entidades Regionales"

# Criterio 3: entidades regionales. Valor MÍNIMO EXCLUSIVO por departamento
# (el proceso debe valer MÁS que esta cifra). Para agregar un departamento,
# añade una línea aquí.
VALOR_MINIMO_REGIONAL = {
    "Arauca": 350_000_000,
    "Casanare": 450_000_000,
    "Vichada": 450_000_000,
    "Guaviare": 450_000_000,
    "Boyacá": 450_000_000,
}

# Entidades de esos departamentos que en SECOP II figuran SIN departamento
# ("No Definido"), por lo que hay que buscarlas por NIT (tal como aparece en
# SECOP; algunos traen dígito de verificación). Si aparece otra, agrégala aquí.
ENTIDADES_EXTRA = {
    "901023825": "Arauca",  # CENTRO EDUCATIVO LA ESMERALDA - ARAUQUITA
    "800212625": "Arauca",  # CONTRALORÍA DEPARTAMENTAL DE ARAUCA
    "800113549": "Arauca",  # EMPRESA MUNICIPAL DE SERVICIOS PUBLICOS DE ARAUCA EMSERPA E.I.C.E. E.S.P.
    "834000421": "Arauca",  # ESTABLECIMIENTO PENITENCIARIO DE MEDIANA SEGURIDAD Y CARCELARIO DE ARAUCA - INPEC
    "8002189794": "Arauca",  # HOSPITAL SAN VICENTE DE ARAUCA ESE
    "892000705": "Arauca",  # INSTITUCION EDUCATIVA GENERAL SANTANDER - ARAUCA*
    "800114869": "Arauca",  # INSTITUTO DE TRANSITO Y TRANSPORTE DEL DEPARTAMENTO DE ARAUCA
    "800099202": "Boyacá",  # ALCALDÍA MUNICIPIO DE GÜICÁN
    "820003774": "Boyacá",  # CÁRCEL Y PENITENCIARIA DE MEDIA SEGURIDAD DE TUNJA
    "800204994": "Boyacá",  # COLEGIO NACIONALIZADO LA PRESENTACION DUITAMA
    "800082204": "Boyacá",  # EMPOCHIQUINQUIRA E.S.P.
    "820000992": "Boyacá",  # ESTABLECIMIENTO PENITENCIARIO DE MEDIANA SEGURIDAD Y CARCELARIO DE PUERTO BOYACÁ
    "800110187": "Boyacá",  # INSTITUTO DE CULTURA Y BELLAS ARTES DE DUITAMA
    "820000484": "Boyacá",  # INSTITUTO MUNICIPAL DE DEPORTE Y RECREACION DE PUERTO BOYACA
    "826000522": "Boyacá",  # INSTITUTO PARA LA EDUCACION FISICA LA RECREACION Y EL DEPORTE DE DUITAMA
    "891855618": "Boyacá",  # INSTITUTO TECNICO SANTO TOMAS DE AQUINO DUITAMA
    "820001521": "Boyacá",  # PERSONERIA MUNICIPAL DE CHITARAQUE DEPARTAMENTO DE BOYACA
    "800065593": "Boyacá",  # MUNICIPIO DE PAJARITO
    "800099639": "Boyacá",  # MUNICIPIO DE TUNUNGUA
    "891800896": "Boyacá",  # MUNICIPIO DE GUAYATA
    "891801362": "Boyacá",  # MUNICIPIO DE OTANCHE
    "891802106": "Boyacá",  # MUNICIPIO DE ZETAQUIRA
    "891856593": "Boyacá",  # MUNICIPIO DE JERICO - BOYACA
    "844001370": "Casanare",  # INSTITUCION EDUCATIVA TECNICO EMPRESARIAL DEL NORTE DE CASANARE
    "844000635": "Casanare",  # ORGANIZACION REGIONAL INDIGENA DE CASANARE ORIC DEL MUNICIPIO DE YOPAL
    "800086017": "Casanare",  # MUNICIPIO DE CHAMEZA
    "800103196": "Guaviare",  # GOBERNACION DEL GUAVIARE
    "800191431": "Guaviare",  # Alcaldia Municipio de Calamar Guaviare
    "832000115": "Guaviare",  # CONTRALORIA DEPARTAMENTAL DEL GUAVIARE
    "832001966": "Guaviare",  # EMPRESA SOCIAL DEL ESTADO HOSPITAL SAN JOSE DEL GUAVIARE
    "900947497": "Guaviare",  # ASAMBLEA DEPARTALMENTAL DEL GUAVIARE
    "800103308": "Vichada",  # ALCALDÍA DE LA PRIMAVERA - VICHADA
    "842000081": "Vichada",  # ASAMBLEA DEPARTAMENTAL DE VICHADA
}

# NIT nacionales compartidos por todas las regionales del país: solo cuentan
# si el NOMBRE de la entidad menciona el departamento.
ENTIDADES_EXTRA_NIT_NACIONAL = {
    "899999067": ["Arauca", "Casanare", "Vichada", "Guaviare", "Boyacá"],  # Contraloría General - gerencias departamentales
    "899999069": ["Guaviare"],  # ICA - seccional Guaviare
}

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
    "Arauca", "Casanare", "Vichada", "Guaviare", "Boyacá", "Guainía", "Meta",
    "Santander", "Norte de Santander", "Cundinamarca",
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
    "Casanare": [
        "Casanare", "Yopal", "El Alcaraván", "Paz de Ariporo", "Támara", "Trinidad", "Hato Corozal",
        "Orocué", "Aguazul", "Tauramena",
    ],
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
    """(título, objeto completo). El objeto completo es la descripción del
    procedimiento, SIN recortar. El título es el nombre del procedimiento y
    solo se devuelve si aporta algo distinto (no es genérico, ni el número del
    proceso, ni el comienzo de la descripción)."""
    nombre = limpiar_fase(p.get("nombre_del_procedimiento"))
    desc = (p.get("descripci_n_del_procedimiento") or "").strip()
    if not desc:
        return "", nombre or "(sin objeto registrado)"
    nombre_n = _norm(nombre)
    generico = (not nombre) or nombre_n == _norm(_referencia(p)) or nombre_n in _NOMBRES_GENERICOS
    repetido = _norm(desc).replace(",", ";").startswith(nombre_n.replace(",", ";")[:50])
    return ("" if (generico or repetido) else nombre), desc


def objeto_completo(p: dict) -> str:
    titulo, objeto = objeto_y_detalle(p)
    return f"{titulo}. {objeto}" if titulo else objeto


def etapa(p: dict) -> str:
    """Etapa del proceso al momento del reporte: fase + estado del procedimiento."""
    fase = (p.get("fase") or "").strip()
    estado = (p.get("estado_del_procedimiento") or "").strip()
    if fase and estado and _norm(estado) != _norm(fase):
        return f"{fase} (estado: {estado})"
    return fase or estado or "No informada"


def _modalidad(p: dict) -> str:
    return (p.get("modalidad_de_contratacion") or "").strip() or "No informada"


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


def _sql_lista(valores) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in valores)


def where_regional(hoy: datetime.date) -> str:
    nits = list(ENTIDADES_EXTRA) + list(ENTIDADES_EXTRA_NIT_NACIONAL)
    return (
        f"( departamento_entidad in ({_sql_lista(VALOR_MINIMO_REGIONAL)}) "
        f"OR nit_entidad in ({_sql_lista(nits)}) ) "
        f"AND precio_base > {min(VALOR_MINIMO_REGIONAL.values())} "
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


_DEPTO_REGIONAL_NORM = {_norm(d): d for d in VALOR_MINIMO_REGIONAL}


def departamento_regional(p: dict):
    """Departamento (de los del criterio 3) al que pertenece la ENTIDAD, o None."""
    depto = _DEPTO_REGIONAL_NORM.get(_norm(p.get("departamento_entidad")).strip())
    if depto:
        return depto
    nit = re.sub(r"\D", "", p.get("nit_entidad") or "")
    if nit in ENTIDADES_EXTRA:
        return ENTIDADES_EXTRA[nit]
    if nit in ENTIDADES_EXTRA_NIT_NACIONAL:
        nombre = _norm(p.get("entidad"))
        for d in ENTIDADES_EXTRA_NIT_NACIONAL[nit]:
            if re.search(r"(?<!\w)" + re.escape(_norm(d)) + r"(?!\w)", nombre):
                return d
    return None


def obtener_regional(hoy: datetime.date) -> list[dict]:
    resultado = []
    for p in _consultar(where_regional(hoy)):
        depto = departamento_regional(p)
        if depto and esta_vigente(p, hoy) and _valor(p) > VALOR_MINIMO_REGIONAL[depto]:
            p["_depto"] = depto  # la entidad manda: no se reclasifica por el texto
            resultado.append(p)
    return resultado


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


_SIN_DEPTO = ("NO DEFINIDO", "NO DEFINIDA", "")


def _buscar_ancla(texto: str):
    mejor, pos_mejor = None, None
    for depto, patrones in _PATRONES_ANCLA.items():
        for pat in patrones:
            m = pat.search(texto)
            if m and (pos_mejor is None or m.start() < pos_mejor):
                mejor, pos_mejor = depto, m.start()
    return mejor


def clasificar_departamento(p: dict) -> str:
    """Sección del reporte en la que va el proceso.

    1. Entidades regionales (criterio 3): el departamento de la entidad.
    2. Entidades territoriales con departamento registrado: ese departamento.
    3. Entidades nacionales (p.ej. Aerocivil) o sin departamento registrado:
       se busca una ancla (municipio, aeropuerto o nombre del departamento),
       por palabra completa; gana la que aparece primero. Para las entidades
       sin departamento se mira primero el NOMBRE de la entidad.
    4. Aerocivil sin ancla: se agrupa por la regional que publica el proceso
       (una regional cubre varios departamentos).
    """
    if p.get("_depto"):
        return p["_depto"]
    depto = (p.get("departamento_entidad") or "").strip()
    sin_depto = _norm(depto) in _SIN_DEPTO
    if sin_depto:
        ancla = _buscar_ancla(_norm(p.get("entidad")))
        if ancla:
            return ancla
    if sin_depto or _es_entidad_nacional(p):
        ancla = _buscar_ancla(_texto_proceso(p))
        if ancla:
            return ancla
    if es_aerocivil(p):
        unidad = " ".join((p.get("nombre_de_la_unidad_de") or "").split()).title()
        ciudad = (p.get("ciudad_de_la_unidad_de") or "").strip()
        if unidad:
            sede = f" (sede {ciudad})" if ciudad and _norm(ciudad) != "NO APLICA" else ""
            return f"{_PREFIJO_AERO}{unidad}{sede}"
    return _CATCHALL if sin_depto else depto


def _clave_dedup(p: dict) -> tuple:
    ref = limpiar_fase(p.get("referencia_del_proceso"))
    if ref:
        return ("ref", p.get("nit_entidad") or p.get("entidad"), ref)
    return ("entidad_nombre", p.get("entidad"), (p.get("nombre_del_procedimiento") or "")[:80])


# De la etapa más temprana a la más avanzada. "Presentación de observaciones"
# (al proyecto de pliego) es ANTERIOR a la presentación de ofertas.
_ORDEN_FASE = [
    "borrador", "planeación", "observaciones", "manifestación de interés",
    "presentación de oferta", "evaluación", "adjudicación",
]


def _prioridad_fase(p: dict) -> int:
    fase = (p.get("fase") or "").strip().lower()
    for i, palabra in enumerate(_ORDEN_FASE):
        if palabra in fase:
            return i
    return 2


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


_CATCHALL = "Sin departamento registrado"
_PREFIJO_AERO = "Aerocivil - "


def _es_prioritario(depto: str) -> bool:
    return depto.startswith(_PREFIJO_AERO) or _norm(depto) in {_norm(d) for d in DEPARTAMENTOS_PRIORITARIOS}


def _orden_departamentos(por_depto: dict) -> list[str]:
    """Departamentos de interés (en el orden de DEPARTAMENTOS_PRIORITARIOS),
    luego los demás en orden alfabético, y al final el catch-all."""
    rango = {_norm(d): i for i, d in enumerate(DEPARTAMENTOS_PRIORITARIOS)}
    prioritarios = sorted((d for d in por_depto if _norm(d) in rango), key=lambda d: rango[_norm(d)])
    aero = sorted(d for d in por_depto if d.startswith(_PREFIJO_AERO))
    otros = sorted(
        d for d in por_depto if _norm(d) not in rango and d != _CATCHALL and not d.startswith(_PREFIJO_AERO)
    )
    return prioritarios + aero + otros + ([_CATCHALL] if _CATCHALL in por_depto else [])


def _dias_restantes(p: dict, hoy: datetime.date):
    fecha = (p.get("fecha_de_recepcion_de") or "")[:10]
    try:
        return fecha, (datetime.date.fromisoformat(fecha) - hoy).days
    except ValueError:
        return fecha, None


# ---------------------------------------------------------------------------
# Salidas: Base44, texto y HTML
# ---------------------------------------------------------------------------

def _categoria_base44(p: dict) -> str:
    cats = p.get("_cats") or []
    if "aero" in cats:
        return CAT_AEROCIVIL
    if "solar" in cats:
        return CAT_SOLAR
    return CAT_REGIONAL


def a_registro_base44(p: dict) -> dict:
    """Formato que espera la entidad ProcesoSecop de la app de Base44 (los
    campos originales no cambian; modalidad, etapa y entidad_ubicacion son nuevos)."""
    return {
        "numero_proceso": _referencia(p),
        "entidad": p.get("entidad") or "",
        "objeto": objeto_completo(p),
        "valor": _valor(p),
        "fecha_limite": (p.get("fecha_de_recepcion_de") or "")[:10] or None,
        "link": _url_proceso(p) or URL_BUSQUEDA_SECOP,
        "region": clasificar_departamento(p),
        "categoria": _categoria_base44(p),
        "estado": "Abierto",
        "modalidad": _modalidad(p),
        "etapa": etapa(p),
        "id_secop": p.get("id_del_proceso") or "",
    }


ETIQUETAS = {"aero": "Aerocivil", "solar": "Solar / energías", "regional": "Entidad regional"}
COLORES = {"aero": "#1d4ed8", "solar": "#b45309", "regional": "#047857"}
_ORDEN_CATS = ["aero", "solar", "regional"]


def _cuenta(procesos: list[dict]) -> str:
    partes = []
    for c in _ORDEN_CATS:
        n = sum(1 for p in procesos if c in (p.get("_cats") or []))
        partes.append(f"{ETIQUETAS[c]}: {n}")
    return " · ".join(partes)


def _fecha_larga(fecha: str) -> str:
    try:
        d = datetime.date.fromisoformat(fecha)
    except ValueError:
        return fecha or "No informada"
    return f"{d.day:02d}/{d.month:02d}/{d.year}"


def _ubicacion(p: dict) -> str:
    ciudad = (p.get("ciudad_entidad") or "").strip()
    return "" if _norm(ciudad) in _SIN_DEPTO else ciudad


def formatear_resumen(procesos: list[dict], hoy: datetime.date, avisos: list[str]) -> str:
    """Texto plano: una sección por DEPARTAMENTO; dentro, por fecha de cierre."""
    lineas = [f"SECOP - Procesos abiertos al {_fecha_larga(hoy.isoformat())} ({len(procesos)} procesos; {_cuenta(procesos)})"]
    for a in avisos:
        lineas.append(f"\n[ATENCIÓN] {a}")
    if not procesos:
        lineas.append("\nNo hay procesos vigentes hoy que cumplan los criterios.")
        return "\n".join(lineas)
    por_depto = _agrupar_por_departamento(procesos)
    orden = _orden_departamentos(por_depto)
    lineas.append("\nRESUMEN POR DEPARTAMENTO")
    for d in orden:
        total = sum(_valor(p) for p in por_depto[d])
        lineas.append(f"  {d}: {len(por_depto[d])} procesos · {_fmt_cop(total)}")
    for depto in orden:
        items = por_depto[depto]
        lineas.append("\n" + "=" * 70)
        lineas.append(f"{depto.upper()} ({len(items)} procesos)")
        lineas.append("=" * 70)
        for i, p in enumerate(items, 1):
            fecha, dias = _dias_restantes(p, hoy)
            ubic = _ubicacion(p)
            criterios = ", ".join(ETIQUETAS[c] for c in _ORDEN_CATS if c in (p.get("_cats") or []))
            lineas.append(
                f"\n{i}. Nº de proceso: {_referencia(p)}\n"
                f"   Entidad: {p.get('entidad')}" + (f" ({ubic})" if ubic else "") + "\n"
                f"   Objeto: {objeto_completo(p)}\n"
                f"   Valor: {_fmt_cop(_valor(p))}\n"
                f"   Modalidad de selección: {_modalidad(p)}\n"
                f"   Etapa al momento del reporte: {etapa(p)}\n"
                f"   Fecha de cierre: {_fecha_larga(fecha)} (faltan {dias if dias is not None else '?'} días)\n"
                f"   Link: {_url_proceso(p) or 'Sin enlace directo; buscar el número en ' + URL_BUSQUEDA_SECOP}\n"
                f"   Criterio: {criterios}"
            )
    return "\n".join(lineas)


def formatear_resumen_html(procesos: list[dict], hoy: datetime.date, avisos: list[str]) -> str:
    """HTML: índice por departamento y una tabla por DEPARTAMENTO; dentro de
    cada uno, por fecha de cierre."""
    e = html.escape
    st_tabla = "width:100%;border-collapse:collapse;margin:0 0 28px 0;font-family:Arial,sans-serif;font-size:13px;"
    st_th = "text-align:left;padding:6px 8px;background:#1f2937;color:#ffffff;border:1px solid #d1d5db;"
    st_td = "padding:6px 8px;border:1px solid #d1d5db;vertical-align:top;"
    gris = "color:#6b7280;"

    partes = [
        "<html><body style='font-family:Arial,sans-serif;color:#111827;'>",
        "<h1 style='margin-bottom:4px;font-size:20px;'>SECOP - Procesos abiertos</h1>",
        f"<p style='margin-top:0;{gris}'>Reporte del {_fecha_larga(hoy.isoformat())} &middot; {len(procesos)} procesos "
        f"({e(_cuenta(procesos))})</p>",
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

    partes.append(f"<table style='{st_tabla}width:auto;'>")
    partes.append(
        f"<tr><th style='{st_th}'>Departamento</th><th style='{st_th}'>Procesos</th>"
        f"<th style='{st_th}'>Valor total</th></tr>"
    )
    for d in orden:
        total = sum(_valor(p) for p in por_depto[d])
        partes.append(
            f"<tr><td style='{st_td}'><b>{e(d)}</b></td>"
            f"<td style='{st_td}text-align:right;'>{len(por_depto[d])}</td>"
            f"<td style='{st_td}text-align:right;white-space:nowrap;'>{e(_fmt_cop(total))}</td></tr>"
        )
    partes.append("</table>")

    for depto in orden:
        items = por_depto[depto]
        partes.append(
            f"<h2 style='background:#1f2937;color:#ffffff;padding:10px;margin:28px 0 0 0;font-size:17px;'>"
            f"{e(depto.upper())} <span style='font-weight:normal;'>&middot; {len(items)} procesos</span></h2>"
        )
        partes.append(f"<table style='{st_tabla}'>")
        partes.append(
            "<tr>"
            f"<th style='{st_th}'>N&ordm; de proceso / Entidad</th>"
            f"<th style='{st_th}'>Objeto</th>"
            f"<th style='{st_th}'>Valor</th>"
            f"<th style='{st_th}'>Modalidad de selecci&oacute;n</th>"
            f"<th style='{st_th}'>Etapa actual</th>"
            f"<th style='{st_th}'>Fecha de cierre</th>"
            f"<th style='{st_th}'>Link</th>"
            "</tr>"
        )
        for p in items:
            fecha, dias = _dias_restantes(p, hoy)
            ubic = _ubicacion(p)
            urgente = dias is not None and dias <= 5
            st_dias = "color:#b91c1c;font-weight:bold;" if urgente else gris
            badges = "".join(
                f"<span style='background:{COLORES[c]};color:#ffffff;font-size:11px;"
                f"padding:1px 6px;border-radius:3px;margin-right:3px;'>{e(ETIQUETAS[c])}</span>"
                for c in _ORDEN_CATS if c in (p.get("_cats") or [])
            )
            url = _url_proceso(p)
            enlace = (
                f"<a href='{e(url, quote=True)}'>Abrir en SECOP</a>"
                if url
                else f"<a href='{e(URL_BUSQUEDA_SECOP, quote=True)}'>Buscar en SECOP</a>"
                f"<br><span style='{gris}font-size:11px;'>sin enlace directo: buscar por el n&uacute;mero</span>"
            )
            partes.append(
                "<tr>"
                f"<td style='{st_td}'><b style='font-size:14px;'>{e(_referencia(p))}</b><br>{e(p.get('entidad') or '')}"
                + (f"<br><span style='{gris}'>{e(ubic)}</span>" if ubic else "")
                + f"<br>{badges}</td>"
                f"<td style='{st_td}'>{e(objeto_completo(p))}</td>"
                f"<td style='{st_td}white-space:nowrap;text-align:right;'>{e(_fmt_cop(_valor(p)))}</td>"
                f"<td style='{st_td}'>{e(_modalidad(p))}</td>"
                f"<td style='{st_td}'>{e(etapa(p))}</td>"
                f"<td style='{st_td}white-space:nowrap;'>{e(_fecha_larga(fecha))}"
                f"<br><span style='{st_dias}'>faltan {dias if dias is not None else '?'} d&iacute;as</span></td>"
                f"<td style='{st_td}'>{enlace}</td>"
                "</tr>"
            )
        partes.append("</table>")
    partes.append(
        f"<p style='{gris}font-size:12px;'>Criterios: (1) Aeron&aacute;utica Civil, todos los procesos; "
        "(2) energ&iacute;a solar y alternativas en todo el pa&iacute;s, desde $500 millones; "
        "(3) entidades de Arauca (m&aacute;s de $350 millones) y de Casanare, Vichada, Guaviare y Boyac&aacute; "
        "(m&aacute;s de $450 millones). Fuente: datos abiertos SECOP II. La etapa y la fecha de cierre son las "
        "registradas al momento del reporte; conf&iacute;rmalas en el link de cada proceso.</p>"
    )
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
    ap.add_argument("--html", help="Ruta opcional para guardar el correo en HTML (para revisarlo sin enviarlo)")
    args = ap.parse_args()

    # Fecha en hora de Colombia (en GitHub Actions el reloj está en UTC).
    hoy = datetime.datetime.now(ZoneInfo("America/Bogota")).date()

    avisos: list[str] = []
    criterios = [
        ("aero", "criterio 1 (Aerocivil)", obtener_aerocivil),
        ("solar", "criterio 2 (energía solar/alternativas)", obtener_solar),
        ("regional", "criterio 3 (entidades regionales)", obtener_regional),
    ]
    # Un proceso que cumple varios criterios se muestra UNA sola vez, con
    # todas sus etiquetas.
    encontrados: list[dict] = []
    todo_ok = True
    for cat, nombre, obtener in criterios:
        try:
            for p in obtener(hoy):
                p["_cats"] = [cat]
                encontrados.append(p)
        except Exception as exc:  # noqa: BLE001
            todo_ok = False
            avisos.append(f"No se pudo consultar el {nombre}: {exc}")

    cats_por_clave: dict = {}
    depto_por_clave: dict = {}
    for p in encontrados:
        clave = _clave_dedup(p)
        cats_por_clave.setdefault(clave, set()).update(p["_cats"])
        if p.get("_depto"):
            depto_por_clave[clave] = p["_depto"]
    todos = deduplicar(encontrados)
    for p in todos:
        clave = _clave_dedup(p)
        p["_cats"] = [c for c in _ORDEN_CATS if c in cats_por_clave[clave]]
        if clave in depto_por_clave:
            p["_depto"] = depto_por_clave[clave]

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)

    # El JSON de Base44 solo se reescribe si TODAS las consultas funcionaron,
    # para no borrar los datos buenos con un resultado vacío por una falla de red.
    if todo_ok:
        por_depto = _agrupar_por_departamento(todos)
        registros = [a_registro_base44(p) for d in _orden_departamentos(por_depto) for p in por_depto[d]]
        with open(args.base44_json, "w", encoding="utf-8") as f:
            json.dump(
                {"generado": hoy.isoformat(), "total": len(registros), "procesos": registros},
                f, ensure_ascii=False, indent=2,
            )

    texto = formatear_resumen(todos, hoy, avisos)
    cuerpo_html = formatear_resumen_html(todos, hoy, avisos)
    print(texto)
    if args.html:
        with open(args.html, "w", encoding="utf-8") as f:
            f.write(cuerpo_html)

    asunto = f"SECOP - Procesos abiertos al {_fecha_larga(hoy.isoformat())} ({len(todos)} procesos; {_cuenta(todos)})"
    if avisos:
        asunto = "[CON ERRORES] " + asunto
    enviar_por_correo(asunto, texto, cuerpo_html)

    if avisos:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
