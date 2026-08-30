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
    tablas_permitidas: list[str] | None = None


@app.get("/", response_class=HTMLResponse)
def home():
    return open("static/index.html", encoding="utf-8").read()


@app.get("/api/estado")
def estado():
    """Cuenta filas sumando TODAS las tablas de tipos FHIR (una por tipo)."""
    conn = conexion()
    cur = conn.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name""")
    tablas = [r[0] for r in cur.fetchall()]
    tipos, total = {}, 0
    for t in tablas:
        try:
            cur.execute(f'SELECT count(*) FROM "{t}"')
            n = cur.fetchone()[0]
            tipos[t] = n; total += n
        except Exception:
            conn.rollback()
    cur.close(); conn.close()
    return {"total_recursos": total, "por_tipo": tipos, "tablas": len(tablas)}


@app.get("/api/tablas")
def tablas():
    """Lista tablas y vistas del esquema public, con conteo de filas, para que
    el usuario elija a cuáles puede acceder el agente."""
    conn = conexion()
    cur = conn.cursor()
    cur.execute("""
        SELECT table_name, table_type FROM information_schema.tables
        WHERE table_schema = 'public' ORDER BY table_type, table_name""")
    filas = cur.fetchall()
    out = []
    for nombre, tipo in filas:
        try:
            cur.execute(f'SELECT count(*) FROM "{nombre}"')
            n = cur.fetchone()[0]
        except Exception:
            conn.rollback(); n = None
        out.append({"nombre": nombre,
                    "tipo": "vista" if tipo == "VIEW" else "tabla",
                    "filas": n})
    cur.close(); conn.close()
    return {"tablas": out}


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
        return agente.preguntar(conn, p.texto, p.fuente, p.tablas_permitidas)
    finally:
        conn.close()


app.mount("/static", StaticFiles(directory="static"), name="static")
