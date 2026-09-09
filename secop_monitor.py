#!/usr/bin/env python3
"""
Monitor de procesos SECOP II para Chronos Asociados
=====================================================

Consulta el dataset abierto oficial "SECOP II - Procesos de Contratación"
(Colombia Compra Eficiente, vía datos.gov.co / Socrata) y filtra los procesos
que están ACTUALMENTE ABIERTOS (fecha límite de respuesta aún no vencida) y
que cumplen los criterios de Chronos Asociados:

    - Sector: aeronáutica civil (y relacionados) o energía solar fotovoltaica
    - Valor mínimo: $350.000.000 COP
    - Alcance: nacional, pero clasificado por región de interés

Este script está pensado para correr en un entorno con acceso normal a
internet (tu propio computador, un servidor, GitHub Actions, PythonAnywhere,
etc.) — NO dentro del sandbox de Claude, cuyo acceso a redes externas está
restringido. En producción corre vía GitHub Actions, los martes y viernes.

Uso:
    pip install requests
    python secop_monitor.py                # imprime el resumen en pantalla
    python secop_monitor.py --json out.json # además guarda el JSON crudo

Para que además ENVÍE el correo (esto es lo que hace GitHub Actions todos los
días), define estas tres variables de entorno antes de correrlo:

    GMAIL_ADDRESS       la cuenta de Gmail que envía el correo
    GMAIL_APP_PASSWORD  una "contraseña de aplicación" de esa cuenta (NO la
                         contraseña normal de Gmail — se genera en
                         https://myaccount.google.com/apppasswords)
    DEST_EMAIL          a quién se le envía (p.ej. chronos.asociados@gmail.com)

Si esas variables no están definidas, el script simplemente imprime el
resumen en pantalla y no intenta enviar nada (útil para probar en local).
"""

import argparse
import datetime
import html
import json
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

DATASET_URL = "https://www.datos.gov.co/resource/p6dx-8zbt.json"

# ---------------------------------------------------------------------------
# Criterios de búsqueda (ajusta aquí si cambian)
# ---------------------------------------------------------------------------

PALABRAS_CLAVE = [
    "AERONAUT",
    "AERONAVE",
    "AEROPUERTO",
    "AERODROMO",
    "AVIACION",
    "FOTOVOLTAIC",
    "ENERGIA SOLAR",
]

VALOR_MINIMO = 350_000_000

# Regiones de interés: departamento (tal como aparece en el dataset) -> región
REGIONES = {
    "Orinoquia": ["Vichada", "Meta", "Guainía", "Casanare", "Guaviare", "Arauca"],
    "Amazonía": ["Amazonas", "Vaupés", "Caquetá", "Putumayo"],
    "Centro": ["Cundinamarca", "Boyacá", "Huila"],
    "Norte": [
        "Santander",
        "Norte de Santander",
        "Cesar",
        "La Guajira",
        "Córdoba",
        "Sucre",
        "Bolívar",
        "Magdalena",
    ],
}

# Palabras clave que definen cada categoría (para clasificar cada proceso en
# la app de Base44 como "Aeronáutica Civil" o "Energía Solar Fotovoltaica").
PALABRAS_AERONAUTICA = ["AERONAUT", "AERONAVE", "AEROPUERTO", "AERODROMO", "AVIACION"]
PALABRAS_SOLAR = ["FOTOVOLTAIC", "ENERGIA SOLAR"]

# Municipios "ancla" para reconocer procesos que, aunque la entidad esté
# registrada en Bogotá (p.ej. Aerocivil), en realidad se ejecutan en una de
# las regiones de interés (esto pasa mucho con aeropuertos regionales).
MUNICIPIOS_POR_DEPARTAMENTO = {
    "Arauca": ["Arauca", "Arauquita", "Cravo Norte", "Fortul", "Puerto Rondón", "Saravena", "Tame"],
    # Agrega aquí más municipios "ancla" de otros departamentos si te interesa
    # detectarlos por nombre de ciudad/aeropuerto en el texto del proceso.
}

DEPARTAMENTO_A_REGION = {
    dep: region for region, deps in REGIONES.items() for dep in deps
}


def construir_where(fecha_min_iso: str) -> str:
    """Arma la cláusula $where de SoQL: sector + valor mínimo + aún abierto."""
    ors = []
    for palabra in PALABRAS_CLAVE:
        ors.append(f"upper(nombre_del_procedimiento) like '%{palabra}%'")
        ors.append(f"upper(descripci_n_del_procedimiento) like '%{palabra}%'")
    clausula_sector = " OR ".join(ors)
    return (
        f"( {clausula_sector} ) "
        f"AND precio_base >= {VALOR_MINIMO} "
        f"AND fecha_de_recepcion_de >= '{fecha_min_iso}'"
    )


def consultar_secop(fecha_min: datetime.date) -> list[dict]:
    campos = (
        "id_del_proceso,referencia_del_proceso,entidad,departamento_entidad,ciudad_entidad,"
        "nombre_del_procedimiento,descripci_n_del_procedimiento,"
        "precio_base,modalidad_de_contratacion,fase,fecha_de_recepcion_de,urlproceso"
    )
    params = {
        "$where": construir_where(f"{fecha_min.isoformat()}T00:00:00.000"),
        "$select": campos,
        "$order": "fecha_de_recepcion_de ASC",
        "$limit": "500",
    }
    resp = requests.get(DATASET_URL, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def clasificar_region(proceso: dict) -> str:
    depto = (proceso.get("departamento_entidad") or "").strip()
    if depto in DEPARTAMENTO_A_REGION:
        return DEPARTAMENTO_A_REGION[depto]

    texto = " ".join(
        [
            proceso.get("nombre_del_procedimiento") or "",
            proceso.get("descripci_n_del_procedimiento") or "",
            proceso.get("ciudad_entidad") or "",
        ]
    ).upper()

    for depto_ancla, municipios in MUNICIPIOS_POR_DEPARTAMENTO.items():
        region = DEPARTAMENTO_A_REGION.get(depto_ancla)
        if region and any(m.upper() in texto for m in municipios):
            return region

    return "Nacional"


def clasificar_categoria(proceso: dict) -> str:
    """Determina si el proceso es de Aeronáutica Civil o de Energía Solar
    Fotovoltaica, según qué palabra clave hizo match."""
    texto = " ".join(
        [
            proceso.get("nombre_del_procedimiento") or "",
            proceso.get("descripci_n_del_procedimiento") or "",
        ]
    ).upper()
    if any(p in texto for p in PALABRAS_SOLAR):
        return "Energía Solar Fotovoltaica"
    if any(p in texto for p in PALABRAS_AERONAUTICA):
        return "Aeronáutica Civil"
    return "Aeronáutica Civil"  # respaldo (no debería pasar: el filtro ya exige una de las dos)


def a_registro_base44(p: dict) -> dict:
    """Convierte un proceso al formato exacto que espera la entidad
    ProcesoSecop de la app de Base44."""
    fecha = (p.get("fecha_de_recepcion_de") or "")[:10] or None
    return {
        "numero_proceso": p.get("referencia_del_proceso") or p.get("id_del_proceso") or "(sin número)",
        "entidad": p.get("entidad") or "",
        "objeto": p.get("nombre_del_procedimiento") or p.get("descripci_n_del_procedimiento") or "",
        "valor": float(p.get("precio_base") or 0),
        "fecha_limite": fecha,
        "link": (p.get("urlproceso") or {}).get("url", ""),
        "region": clasificar_region(p),
        "categoria": clasificar_categoria(p),
        "estado": "Abierto",
    }


def _clave_dedup(p: dict) -> tuple:
    """Referencia del proceso sin la parte de fase entre paréntesis
    (p.ej. 'SA-SIE-011-2026 (Presentación de oferta)' -> 'SA-SIE-011-2026'),
    que es estable entre las distintas fases de un mismo proceso. Si no hay
    referencia, usa entidad + nombre como respaldo."""
    ref = (p.get("referencia_del_proceso") or "").split("(")[0].strip()
    if ref:
        return ("ref", ref)
    return ("entidad_nombre", p.get("entidad"), (p.get("nombre_del_procedimiento") or "")[:80])


# Orden aproximado del ciclo de vida del proceso: entre más alto, más
# "avanzado" (más cerca de la presentación real de la oferta). Se usa para
# quedarnos con la fase vigente cuando el mismo proceso aparece varias veces.
_ORDEN_FASE = [
    "borrador",
    "planeación",
    "manifestación de interés",
    "presentación de oferta",
    "evaluación",
    "adjudicación",
]


def _prioridad_fase(p: dict) -> int:
    fase = (p.get("fase") or "").strip().lower()
    for i, palabra in enumerate(_ORDEN_FASE):
        if palabra in fase:
            return i
    return len(_ORDEN_FASE) // 2  # fase desconocida: prioridad media


def deduplicar(procesos: list[dict]) -> list[dict]:
    """Colapsa procesos que son claramente el mismo (misma referencia, o misma
    entidad + mismo nombre), quedándose con la fase más avanzada/vigente
    (y, si empatan, con la fecha límite más reciente)."""
    vistos = {}
    for p in procesos:
        clave = _clave_dedup(p)
        actual = vistos.get(clave)
        if actual is None:
            vistos[clave] = p
            continue
        nuevo_mejor = (
            _prioridad_fase(p),
            p.get("fecha_de_recepcion_de") or "",
        ) > (
            _prioridad_fase(actual),
            actual.get("fecha_de_recepcion_de") or "",
        )
        if nuevo_mejor:
            vistos[clave] = p
    return list(vistos.values())


ORDEN_REGIONES = ["Orinoquia", "Amazonía", "Centro", "Norte", "Nacional"]


def _agrupar_por_region(procesos: list[dict]) -> dict[str, list[dict]]:
    por_region: dict[str, list[dict]] = {}
    for p in procesos:
        por_region.setdefault(clasificar_region(p), []).append(p)
    for items in por_region.values():
        items.sort(key=lambda p: p.get("fecha_de_recepcion_de") or "9999")
    return por_region


def _dias_restantes(p: dict, hoy: datetime.date):
    fecha = (p.get("fecha_de_recepcion_de") or "")[:10]
    if not fecha:
        return fecha, None
    try:
        return fecha, (datetime.date.fromisoformat(fecha) - hoy).days
    except ValueError:
        return fecha, None


def formatear_resumen(procesos: list[dict], hoy: datetime.date) -> str:
    """Versión en texto plano (respaldo para clientes de correo sin HTML)."""
    if not procesos:
        return f"SECOP - Oportunidades del {hoy.isoformat()}\n\nNo hay procesos abiertos que cumplan los criterios hoy.\n"

    por_region = _agrupar_por_region(procesos)

    lineas = [f"SECOP - Oportunidades abiertas del {hoy.isoformat()} ({len(procesos)} procesos)\n"]
    for region in ORDEN_REGIONES:
        items = por_region.get(region)
        if not items:
            continue
        lineas.append(f"\n=== {region} ({len(items)}) ===")
        for p in items:
            fecha, dias = _dias_restantes(p, hoy)
            valor = int(float(p.get("precio_base") or 0))
            url = (p.get("urlproceso") or {}).get("url", "")
            referencia = p.get("referencia_del_proceso") or p.get("id_del_proceso") or "(sin número)"
            lineas.append(
                f"- Nº proceso: {referencia}\n"
                f"    Entidad: {p.get('entidad')} ({p.get('ciudad_entidad') or p.get('departamento_entidad') or 's/d'})\n"
                f"    Objeto: {p.get('nombre_del_procedimiento')}\n"
                f"    Valor: ${valor:,} | Cierra: {fecha} ({dias if dias is not None else '?'} días) | "
                f"Modalidad: {p.get('modalidad_de_contratacion')} | Fase: {p.get('fase')}\n"
                f"    Link: {url}"
            )
    return "\n".join(lineas)


def formatear_resumen_html(procesos: list[dict], hoy: datetime.date) -> str:
    """Versión en HTML: una tabla por región, ordenada por fecha límite más próxima."""
    estilo_tabla = (
        "width:100%;border-collapse:collapse;margin:0 0 24px 0;font-family:Arial,sans-serif;font-size:13px;"
    )
    estilo_th = (
        "text-align:left;padding:6px 8px;background:#1f2937;color:#ffffff;border:1px solid #d1d5db;"
    )
    estilo_td = "padding:6px 8px;border:1px solid #d1d5db;vertical-align:top;"

    if not procesos:
        return (
            f"<html><body style='font-family:Arial,sans-serif;'>"
            f"<h2>SECOP - Oportunidades del {hoy.isoformat()}</h2>"
            f"<p>No hay procesos abiertos que cumplan los criterios hoy.</p>"
            f"</body></html>"
        )

    por_region = _agrupar_por_region(procesos)

    partes = [
        "<html><body style='font-family:Arial,sans-serif;color:#111827;'>",
        f"<h2 style='margin-bottom:4px;'>SECOP - Oportunidades abiertas</h2>",
        f"<p style='margin-top:0;color:#4b5563;'>{hoy.isoformat()} &middot; {len(procesos)} procesos</p>",
    ]

    for region in ORDEN_REGIONES:
        items = por_region.get(region)
        if not items:
            continue
        partes.append(f"<h3 style='margin-bottom:6px;'>{html.escape(region)} ({len(items)})</h3>")
        partes.append(f"<table style='{estilo_tabla}'>")
        partes.append(
            "<tr>"
            f"<th style='{estilo_th}'>Nº proceso</th>"
            f"<th style='{estilo_th}'>Entidad / ubicación</th>"
            f"<th style='{estilo_th}'>Objeto</th>"
            f"<th style='{estilo_th}'>Valor</th>"
            f"<th style='{estilo_th}'>Modalidad / Fase</th>"
            f"<th style='{estilo_th}'>Cierra</th>"
            f"<th style='{estilo_th}'>Días</th>"
            f"<th style='{estilo_th}'>Ver</th>"
            "</tr>"
        )
        for p in items:
            fecha, dias = _dias_restantes(p, hoy)
            valor = int(float(p.get("precio_base") or 0))
            url = (p.get("urlproceso") or {}).get("url", "")
            referencia = p.get("referencia_del_proceso") or p.get("id_del_proceso") or "(sin número)"
            ubicacion = p.get("ciudad_entidad") or p.get("departamento_entidad") or "s/d"

            urgente = dias is not None and dias <= 5
            estilo_dias = "color:#b91c1c;font-weight:bold;" if urgente else ""

            partes.append(
                "<tr>"
                f"<td style='{estilo_td}'>{html.escape(str(referencia))}</td>"
                f"<td style='{estilo_td}'>{html.escape(p.get('entidad') or '')}<br>"
                f"<span style='color:#6b7280;'>{html.escape(ubicacion)}</span></td>"
                f"<td style='{estilo_td}'>{html.escape(p.get('nombre_del_procedimiento') or '')}</td>"
                f"<td style='{estilo_td}'>${valor:,}</td>"
                f"<td style='{estilo_td}'>{html.escape(p.get('modalidad_de_contratacion') or '')}"
                f"<br><span style='color:#6b7280;'>{html.escape(p.get('fase') or '')}</span></td>"
                f"<td style='{estilo_td}'>{fecha}</td>"
                f"<td style='{estilo_td}{estilo_dias}'>{dias if dias is not None else '?'}</td>"
                f"<td style='{estilo_td}'><a href='{html.escape(url)}'>Abrir</a></td>"
                "</tr>"
            )
        partes.append("</table>")

    partes.append("</body></html>")
    return "\n".join(partes)


def enviar_por_correo(asunto: str, cuerpo_texto: str, cuerpo_html: str) -> None:
    """Envía el resumen por Gmail (HTML, con texto plano de respaldo) usando
    las variables de entorno GMAIL_ADDRESS, GMAIL_APP_PASSWORD y DEST_EMAIL.
    No hace nada si no están definidas."""
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
    # El primer "attach" es el respaldo de texto plano; el último es el que
    # los clientes de correo modernos (Gmail incluido) muestran por defecto.
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
        help="Ruta donde se guarda siempre el resumen del día ya listo para la app de "
        "Base44 (numero_proceso, entidad, objeto, valor, fecha_limite, link, region, "
        "categoria, estado). Por defecto: secop_data.json en el directorio actual.",
    )
    args = ap.parse_args()

    hoy = datetime.date.today()
    procesos = consultar_secop(hoy)
    procesos = deduplicar(procesos)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(procesos, f, ensure_ascii=False, indent=2)

    # Este archivo se guarda SIEMPRE (no solo con --json) porque GitHub Actions
    # lo sube de vuelta al repositorio en cada corrida, y desde ahí Claude lo
    # lee para mantener sincronizada la app de Base44 sin depender de tu PC.
    registros_base44 = [a_registro_base44(p) for p in procesos]
    with open(args.base44_json, "w", encoding="utf-8") as f:
        json.dump(
            {"generado": hoy.isoformat(), "total": len(registros_base44), "procesos": registros_base44},
            f,
            ensure_ascii=False,
            indent=2,
        )

    resumen_texto = formatear_resumen(procesos, hoy)
    resumen_html = formatear_resumen_html(procesos, hoy)
    print(resumen_texto)

    asunto = f"SECOP - Oportunidades del {hoy.isoformat()} ({len(procesos)} procesos)"
    enviar_por_correo(asunto, resumen_texto, resumen_html)


if __name__ == "__main__":
    sys.exit(main())
