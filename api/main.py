"""API del POC: subir bundles FHIR, preguntar en lenguaje natural, ver la query."""
import json
import os
import tempfile

import psycopg2
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agente
import ingesta

DSN = os.environ["DATABASE_URL"]
app = FastAPI(title="Agente de datos FHIR (POC)")


def conexion():
    return psycopg2.connect(DSN)


class Pregunta(BaseModel):
    texto: str
    fuente: str = "fhir"


@app.get("/", response_class=HTMLResponse)
def home():
    return open("static/index.html", encoding="utf-8").read()


@app.get("/api/estado")
def estado():
    conn = conexion()
    cur = conn.cursor()
    cur.execute("SELECT tipo, count(*) FROM resources GROUP BY tipo ORDER BY 2 DESC")
    tipos = {t: n for t, n in cur.fetchall()}
    cur.execute("SELECT count(*) FROM resources")
    total = cur.fetchone()[0]
    cur.close(); conn.close()
    return {"total_recursos": total, "por_tipo": tipos}


@app.get("/api/config")
def get_config():
    d = agente.CONFIG_DIR
    lee = lambda *p: open(os.path.join(d, *p), encoding="utf-8").read() if os.path.exists(os.path.join(d, *p)) else ""
    return {
        "general": lee("agente_general.md"),
        "descripcion": lee("fuentes", "fhir", "descripcion.md"),
        "reglas": lee("fuentes", "fhir", "reglas.md"),
    }


@app.post("/api/subir")
async def subir(archivo: UploadFile = File(...)):
    contenido = await archivo.read()
    try:
        bundle = json.loads(contenido)
    except json.JSONDecodeError:
        return JSONResponse({"ok": False, "error": "El archivo no es JSON válido."}, 400)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(bundle, f)
        ruta = f.name
    conn = conexion()
    cur = conn.cursor()
    n = ingesta.cargar_bundle(cur, ruta)
    conn.commit(); cur.close(); conn.close()
    os.unlink(ruta)
    return {"ok": True, "recursos_cargados": n, "archivo": archivo.filename}


@app.post("/api/preguntar")
def preguntar(p: Pregunta):
    conn = conexion()
    try:
        return agente.preguntar(conn, p.texto, p.fuente)
    finally:
        conn.close()


app.mount("/static", StaticFiles(directory="static"), name="static")
