"""Agente text-to-SQL: traduce una pregunta a SQL de PostgreSQL usando un LLM
(vía OmniRoute, OpenAI-compatible), la ejecuta en solo-lectura y devuelve la
respuesta + la consulta para verificar."""
import json
import os
import re
import time
import urllib.request

OMNIROUTE_URL = os.environ.get("OMNIROUTE_URL", "http://host.docker.internal:20128/v1")
MODELO = os.environ.get("OMNIROUTE_MODELO", "auto/best-coding")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

# Solo se permite una única sentencia SELECT.
PROHIBIDO = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|"
                       r"revoke|copy|;.*\S)\b", re.IGNORECASE)


def leer(*partes):
    ruta = os.path.join(CONFIG_DIR, *partes)
    return open(ruta, encoding="utf-8").read() if os.path.exists(ruta) else ""


def system_prompt(fuente="fhir"):
    return "\n\n".join(filter(None, [
        leer("agente_general.md"),
        "## DESCRIPCIÓN DE LA FUENTE DE DATOS\n\n" + leer("fuentes", fuente, "descripcion.md"),
        "## REGLAS PARA ESTA FUENTE\n\n" + leer("fuentes", fuente, "reglas.md"),
    ]))


def llamar_llm(system, pregunta):
    # El pool de modelos gratis de OmniRoute puede devolver 429 si está
    # saturado; reintentamos con backoff y con modelos de respaldo.
    # Cadena de failover que cruza VARIOS proveedores de OmniRoute, no solo
    # el pool keyless. Los que necesiten cuenta conectada en el dashboard se
    # saltan solos (403/418); en cuanto conectes proveedores, se aprovechan.
    modelos = [MODELO, "auto/best-coding", "auto/coding", "auto/best-chat",
               "ddgw/gpt-5.4-mini", "ddgw/claude-haiku-4-5", "tllm/GPT_5_4",
               "felo/felo-chat", "oc/deepseek-v4-flash-free", "oc/big-pickle"]
    ultimo = None
    for intento in range(6):
        modelo = modelos[min(intento, len(modelos) - 1)]
        body = json.dumps({
            "model": modelo, "stream": False, "temperature": 0.1,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": pregunta}],
        }).encode()
        req = urllib.request.Request(OMNIROUTE_URL + "/chat/completions", data=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            ultimo = e
            if e.code == 429:            # saturado: espera y reintenta
                time.sleep(3 + intento * 4)
                continue
            if e.code in (400, 403, 418, 502, 503):  # proveedor no disponible/sin conectar
                continue                 # salta al siguiente modelo de inmediato
            raise
        except Exception as e:           # timeout u otro: prueba el siguiente
            ultimo = e
            continue
    raise ultimo


def extraer_sql(texto):
    """El LLM debe responder JSON {sql, explicacion}; toleramos variantes."""
    m = re.search(r"\{.*\}", texto, re.DOTALL)
    if m:
        try:
            o = json.loads(m.group(0))
            if o.get("sql"):
                return o["sql"].strip(), o.get("explicacion", "")
        except json.JSONDecodeError:
            pass
    m = re.search(r"```(?:sql)?\s*(select.*?)```", texto, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip(), ""
    m = re.search(r"(select\b.*)", texto, re.IGNORECASE | re.DOTALL)
    return (m.group(1).strip().rstrip(";"), "") if m else (None, "")


def validar(sql):
    if not sql or not sql.lower().lstrip().startswith("select"):
        return "La consulta generada no es un SELECT."
    if PROHIBIDO.search(sql):
        return "La consulta contiene operaciones no permitidas."
    return None


def preguntar(conn, pregunta, fuente="fhir"):
    crudo = llamar_llm(system_prompt(fuente), pregunta)
    sql, explicacion = extraer_sql(crudo)
    error = validar(sql)
    if error:
        return {"ok": False, "error": error, "sql": sql, "crudo": crudo}
    try:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = 8000")
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        filas = [dict(zip(cols, r)) for r in cur.fetchmany(200)]
        cur.close()
        conn.rollback()  # nunca dejamos transacción abierta escribiendo
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return {"ok": False, "error": f"Error al ejecutar: {e}", "sql": sql}
    return {"ok": True, "sql": sql, "explicacion": explicacion,
            "columnas": cols, "filas": filas, "n": len(filas)}
