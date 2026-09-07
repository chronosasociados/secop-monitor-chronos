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
import json
import os
import smtplib
import sys
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
        "id_del_proceso,entidad,departamento_entidad,ciudad_entidad,"
        "nombre_del_procedimiento,descripci_n_del_procedimiento,"
        "precio_base,modalidad_de_contratacion,fecha_de_recepcion_de,urlproceso"
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

    return "Nacional / sin región específica"


def deduplicar(procesos: list[dict]) -> list[dict]:
    """Colapsa procesos que son claramente el mismo (misma entidad + mismo
    nombre), quedándose con la fase más próxima a vencer."""
    vistos = {}
    for p in procesos:
        clave = (p.get("entidad"), (p.get("nombre_del_procedimiento") or "")[:80])
        actual = vistos.get(clave)
        if actual is None:
            vistos[clave] = p
            continue
        f_actual = actual.get("fecha_de_recepcion_de") or "9999"
        f_nuevo = p.get("fecha_de_recepcion_de") or "9999"
        if f_nuevo < f_actual:
            vistos[clave] = p
    return list(vistos.values())


def formatear_resumen(procesos: list[dict], hoy: datetime.date) -> str:
    if not procesos:
        return f"SECOP - Oportunidades del {hoy.isoformat()}\n\nNo hay procesos nuevos que cumplan los criterios hoy.\n"

    por_region: dict[str, list[dict]] = {}
    for p in procesos:
        por_region.setdefault(clasificar_region(p), []).append(p)

    orden_regiones = ["Orinoquia", "Amazonía", "Centro", "Norte", "Nacional / sin región específica"]

    lineas = [f"SECOP - Oportunidades abiertas del {hoy.isoformat()} ({len(procesos)} procesos)\n"]
    for region in orden_regiones:
        items = por_region.get(region)
        if not items:
            continue
        items.sort(key=lambda p: p.get("fecha_de_recepcion_de") or "9999")
        lineas.append(f"\n=== {region} ({len(items)}) ===")
        for p in items:
            fecha = (p.get("fecha_de_recepcion_de") or "")[:10]
            dias = "?"
            if fecha:
                try:
                    dias = (datetime.date.fromisoformat(fecha) - hoy).days
                except ValueError:
                    pass
            valor = int(float(p.get("precio_base") or 0))
            url = (p.get("urlproceso") or {}).get("url", "")
            lineas.append(
                f"- [{p.get('entidad')}] {p.get('nombre_del_procedimiento')}\n"
                f"    Valor: ${valor:,} | Cierra: {fecha} ({dias} días) | "
                f"Modalidad: {p.get('modalidad_de_contratacion')}\n"
                f"    {url}"
            )
    return "\n".join(lineas)


def enviar_por_correo(asunto: str, cuerpo: str) -> None:
    """Envía `cuerpo` por Gmail usando las variables de entorno GMAIL_ADDRESS,
    GMAIL_APP_PASSWORD y DEST_EMAIL. No hace nada si no están definidas."""
    remitente = os.environ.get("GMAIL_ADDRESS")
    clave_app = os.environ.get("GMAIL_APP_PASSWORD")
    destinatario = os.environ.get("DEST_EMAIL")

    if not (remitente and clave_app and destinatario):
        print("[info] Variables de correo no definidas — no se envía correo, solo se imprime.")
        return

    msg = MIMEText(cuerpo, "plain", "utf-8")
    msg["Subject"] = asunto
    msg["From"] = remitente
    msg["To"] = destinatario

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as servidor:
        servidor.login(remitente, clave_app)
        servidor.sendmail(remitente, [destinatario], msg.as_string())
    print(f"[ok] Correo enviado a {destinatario}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="Ruta opcional para guardar el JSON crudo de resultados")
    args = ap.parse_args()

    hoy = datetime.date.today()
    procesos = consultar_secop(hoy)
    procesos = deduplicar(procesos)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(procesos, f, ensure_ascii=False, indent=2)

    resumen = formatear_resumen(procesos, hoy)
    print(resumen)

    asunto = f"SECOP - Oportunidades del {hoy.isoformat()} ({len(procesos)} procesos)"
    enviar_por_correo(asunto, resumen)


if __name__ == "__main__":
    sys.exit(main())
