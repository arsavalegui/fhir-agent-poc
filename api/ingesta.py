"""Ingesta FHIR con UNA TABLA POR TIPO DE RECURSO.

El tipo se detecta por el campo `resourceType` DENTRO del JSON (fuente de
verdad), no por el nombre del archivo. Un archivo puede ser:
  - un Bundle con muchos recursos de distintos tipos (entry[].resource), o
  - un recurso suelto.
Cada recurso va a la tabla con su tipo en minúsculas (Patient → patient),
que se CREA si no existe y a la que se hace append (ON CONFLICT DO NOTHING).
El recurso Patient se anonimiza al entrar (capa PII)."""
import json
import os
import re
import glob

import psycopg2
from psycopg2.extras import execute_values

# Campos con identificadores directos que se quitan del recurso Patient.
PII_PATIENT = ("name", "telecom", "address", "identifier", "photo", "contact", "communication")


def nombre_tabla(resource_type):
    """resourceType → nombre de tabla seguro (minúsculas, solo [a-z0-9_])."""
    t = re.sub(r"[^a-z0-9_]", "", resource_type.lower())
    return t or "desconocido"


def ref_paciente(recurso):
    if recurso.get("resourceType") == "Patient":
        return recurso.get("id")
    for campo in ("subject", "patient"):
        ref = (recurso.get(campo) or {}).get("reference", "")
        if ref:
            return ref.split(":")[-1].split("/")[-1]
    return None


def anonimizar(recurso):
    """Enmascara PII del recurso Patient; los demás no traen identificadores
    directos (referencian al paciente por uuid)."""
    if recurso.get("resourceType") != "Patient":
        return recurso
    r = {k: v for k, v in recurso.items() if k not in PII_PATIENT}
    if recurso.get("birthDate"):
        r["birthYear"] = str(recurso["birthDate"])[:4]
        r.pop("birthDate", None)
    return r


def asegurar_tabla(cur, tabla):
    """Crea la tabla del tipo si no existe (id, recurso jsonb, paciente_id)."""
    cur.execute(f'''
        CREATE TABLE IF NOT EXISTS "{tabla}" (
            id          TEXT PRIMARY KEY,
            recurso     JSONB NOT NULL,
            paciente_id TEXT,
            cargado_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )''')
    cur.execute(f'CREATE INDEX IF NOT EXISTS "{tabla}_gin" ON "{tabla}" USING gin (recurso)')
    cur.execute(f'CREATE INDEX IF NOT EXISTS "{tabla}_pac" ON "{tabla}" (paciente_id)')


def recursos_de_archivo(obj):
    """Devuelve la lista de recursos, sea un Bundle o un recurso suelto."""
    if obj.get("resourceType") == "Bundle":
        return [e["resource"] for e in obj.get("entry", []) if e.get("resource")]
    if obj.get("resourceType"):
        return [obj]
    return []


def cargar_bundle(cur, ruta):
    """Carga un archivo (bundle o recurso suelto) repartiendo por tabla."""
    obj = json.load(open(ruta))
    por_tabla = {}
    for r in recursos_de_archivo(obj):
        rt = r.get("resourceType")
        if not rt:
            continue
        tabla = nombre_tabla(rt)
        rid = f"{rt}/{r.get('id', '')}"
        por_tabla.setdefault(tabla, []).append(
            (rid, json.dumps(anonimizar(r)), ref_paciente(r)))
    total = 0
    for tabla, filas in por_tabla.items():
        asegurar_tabla(cur, tabla)
        execute_values(cur,
            f'INSERT INTO "{tabla}" (id, recurso, paciente_id) VALUES %s '
            f'ON CONFLICT (id) DO NOTHING', filas)
        total += len(filas)
    return total


def cargar_directorio(dsn, carpeta):
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    archivos = glob.glob(os.path.join(carpeta, "*.json"))
    total = sum(cargar_bundle(cur, f) for f in archivos)
    conn.commit()
    cur.close()
    conn.close()
    return len(archivos), total


if __name__ == "__main__":
    dsn = os.environ["DATABASE_URL"]
    carpeta = os.environ.get("CARPETA_FHIR", "/datos_fhir/bundles")
    n, r = cargar_directorio(dsn, carpeta)
    print(f"Cargados {n} archivos, {r} recursos repartidos por tabla.")
