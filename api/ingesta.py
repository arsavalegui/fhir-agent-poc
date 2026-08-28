"""Ingesta de bundles FHIR: aplana cada bundle en filas de la tabla resources.
El JSON del recurso se guarda CRUDO en jsonb, sin transformar."""
import json
import os
import glob
import psycopg2
from psycopg2.extras import execute_values


def ref_paciente(recurso):
    """Extrae el uuid del paciente que referencia el recurso."""
    if recurso.get("resourceType") == "Patient":
        return recurso.get("id")
    for campo in ("subject", "patient"):
        ref = recurso.get(campo, {}).get("reference", "")
        if ref:
            return ref.split(":")[-1].split("/")[-1]
    return None


def cargar_bundle(cur, ruta):
    bundle = json.load(open(ruta))
    filas = []
    for entry in bundle.get("entry", []):
        r = entry.get("resource")
        if not r:
            continue
        rid = f"{r['resourceType']}/{r.get('id', '')}"
        filas.append((rid, r["resourceType"], ref_paciente(r), "fhir", json.dumps(r)))
    execute_values(cur,
        "INSERT INTO resources (id, tipo, paciente_id, fuente, recurso) VALUES %s "
        "ON CONFLICT (id) DO NOTHING", filas)
    return len(filas)


def cargar_directorio(dsn, carpeta):
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    total = 0
    archivos = glob.glob(os.path.join(carpeta, "*.json"))
    for f in archivos:
        total += cargar_bundle(cur, f)
    conn.commit()
    cur.close()
    conn.close()
    return len(archivos), total


if __name__ == "__main__":
    dsn = os.environ["DATABASE_URL"]
    carpeta = os.environ.get("CARPETA_FHIR", "/datos_fhir/bundles")
    n, r = cargar_directorio(dsn, carpeta)
    print(f"Cargados {n} bundles, {r} recursos.")
