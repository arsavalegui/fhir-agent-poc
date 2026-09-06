"""Agente text-to-SQL: traduce una pregunta a SQL de PostgreSQL usando un LLM
(vía OmniRoute, OpenAI-compatible), la ejecuta en solo-lectura y devuelve la
respuesta + la consulta para verificar."""
import hashlib
import json
import os
import re
import time
import urllib.request

LLM_URL = os.environ.get("LLM_URL", "http://host.docker.internal:11434/v1")
MODELO = os.environ.get("LLM_MODELO", "qwen2.5-coder:3b")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

# Solo se permite una única sentencia SELECT (o WITH ... SELECT).
PROHIBIDO = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|"
                       r"revoke|copy|;.*\S)\b", re.IGNORECASE)

# Claves de "plomería" del formato FHIR (no son datos de negocio): se excluyen
# del catálogo de campos para no inflar el prompt con ruido.
CLAVES_PLOMERIA = {"resourceType", "id", "meta", "text", "div", "extension",
                    "modifierExtension", "implicitRules", "language", "contained"}
MUESTRA_POR_TABLA = 200    # filas que se leen para inferir los campos de cada tabla
MAX_CAMPOS_POR_TABLA = 25  # tope por tabla para no disparar el tamaño del prompt

# Cache en memoria del catálogo: se regenera solo si cambia el conjunto de
# tablas, así el system prompt es BYTE-IDÉNTICO entre preguntas (Ollama puede
# reusar su cache de prompt si no cambia una letra).
_catalogo_cache = {"tablas": None, "texto": None}


def leer(*partes):
    ruta = os.path.join(CONFIG_DIR, *partes)
    return open(ruta, encoding="utf-8").read() if os.path.exists(ruta) else ""


def _campos_de_tabla(cur, tabla):
    """Expresiones jsonb LISTAS PARA COPIAR (no solo el nombre del campo):
    escalar -> `recurso->>'campo'`; objeto anidado -> `recurso->'campo'->>'sub'`
    (con un solo `->>` al final: encadenar dos `->>` es inválido en Postgres,
    porque el primero ya devuelve texto); arreglo -> `jsonb_array_elements(
    recurso->'campo')`. Se infiere de los datos, nunca a mano."""
    cur.execute(f'SELECT recurso FROM "{tabla}" LIMIT {MUESTRA_POR_TABLA}')
    campos = {}  # clave -> set de subclaves ("[]" si es arreglo, vacío si es escalar)
    for (recurso,) in cur.fetchall():
        for clave, valor in (recurso or {}).items():
            if clave in CLAVES_PLOMERIA:
                continue
            if isinstance(valor, dict):
                # OJO: no filtramos CLAVES_PLOMERIA aquí dentro. "text" es
                # plomería a nivel raíz (Resource.text/Narrative), pero es
                # justo el campo clínico útil dentro de un CodeableConcept
                # (code.text, vaccineCode.text) — filtrarlo aquí lo perdería.
                campos.setdefault(clave, set()).update(valor.keys())
            elif isinstance(valor, list):
                campos.setdefault(clave, set()).add("[]")
            else:
                campos.setdefault(clave, set())
    salida = []
    for clave in sorted(campos):
        subs = campos[clave]
        if "[]" in subs:
            salida.append(f"jsonb_array_elements(recurso->'{clave}')")
        elif subs:
            salida.extend(f"recurso->'{clave}'->>'{sub}'" for sub in sorted(subs))
        else:
            salida.append(f"recurso->>'{clave}'")
    return salida[:MAX_CAMPOS_POR_TABLA]


def catalogo_campos(conn):
    """Catálogo compacto `tabla: campo1, campo2, ...` para todas las tablas,
    generado desde los datos (jsonb_object_keys sobre una muestra), no a
    mano. Cacheado en memoria; ver _catalogo_cache."""
    cur = conn.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name""")
    tablas = tuple(r[0] for r in cur.fetchall())
    if tablas == _catalogo_cache["tablas"]:
        cur.close()
        return _catalogo_cache["texto"]
    lineas = []
    for tabla in tablas:
        campos = _campos_de_tabla(cur, tabla)
        lineas.append(f"{tabla}: {', '.join(campos)}" if campos else f"{tabla}: (sin muestra)")
    cur.close()
    texto = "\n".join(lineas)
    _catalogo_cache["tablas"], _catalogo_cache["texto"] = tablas, texto
    return texto


def system_prompt(conn, fuente="fhir"):
    return "\n\n".join(filter(None, [
        leer("agente_general.md"),
        "## DESCRIPCIÓN DE LA FUENTE DE DATOS\n\n" + leer("fuentes", fuente, "descripcion.md"),
        "## CATÁLOGO DE CAMPOS (inferido de los datos; usa estas expresiones "
        "EXACTAS, no las inventes ni las combines distinto)\n\n"
        + catalogo_campos(conn),
        "## REGLAS PARA ESTA FUENTE\n\n" + leer("fuentes", fuente, "reglas.md"),
    ]))


def llamar_llm(system, pregunta, temperature=0.1):
    # El pool de modelos gratis de OmniRoute puede devolver 429 si está
    # saturado; reintentamos con backoff y con modelos de respaldo.
    # Cadena de failover que cruza VARIOS proveedores de OmniRoute, no solo
    # el pool keyless. Los que necesiten cuenta conectada en el dashboard se
    # saltan solos (403/418); en cuanto conectes proveedores, se aprovechan.
    modelos = [MODELO]  # modelo local (Ollama); es siempre nuestro, no se agota
    ultimo = None
    for intento in range(6):
        modelo = modelos[min(intento, len(modelos) - 1)]
        body = json.dumps({
            "model": modelo, "stream": False, "temperature": temperature,
            # El endpoint OpenAI-compatible de Ollama IGNORA "options" (probado):
            # el num_ctx real se fija en el Modelfile del modelo (ver README,
            # `ollama create qwen2.5-coder-fhir:3b`). Mandamos igual esta clave
            # por si otro backend/proveedor sí la respeta; no hace daño si no.
            "options": {"num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": pregunta}],
        }).encode()
        req = urllib.request.Request(LLM_URL + "/chat/completions", data=body,
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
            if e.code in (400, 403, 404, 418, 502, 503):  # proveedor no disponible/sin conectar
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
            # strict=False: tolera saltos de línea literales dentro del string sql
            o = json.loads(m.group(0), strict=False)
            if o.get("sql"):
                return o["sql"].strip(), o.get("explicacion", "")
        except json.JSONDecodeError:
            pass
    m = re.search(r"```(?:sql)?\s*(select.*?)```", texto, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip(), ""
    # [^"] corta antes de la comilla que cierra el string JSON; no tragar el resto
    m = re.search(r"(select\b[^\"]*)", texto, re.IGNORECASE)
    return (m.group(1).strip().rstrip(";"), "") if m else (None, "")


DOBLE_FLECHA = re.compile(r"->>('(?:[^'\\]|\\.)*')\s*->>")


def arreglar_flechas_encadenadas(sql):
    """Corrige mecánicamente el error más común del LLM con jsonb: encadenar
    `->>'a'->>'b'` (inválido en Postgres — el primer `->>` ya da texto plano,
    que no se puede volver a indexar). El catálogo ya enseña la forma
    correcta (`->'a'->>'b'`), pero un modelo de 3B a veces la ignora incluso
    tras el reintento (visto en pruebas); esto lo arregla sin depender de que
    el LLM lo entienda. Dos `->>'x'->>` seguidos se convierten en `->'x'->>`;
    se repite hasta que no queden encadenados (para cadenas de 3+ niveles)."""
    if not sql:
        return sql
    anterior = None
    while sql != anterior:
        anterior = sql
        sql = DOBLE_FLECHA.sub(r"->\1->>", sql)
    return sql


def tablas_en_sql(sql):
    """Nombres que aparecen tras FROM/JOIN, para validar el acceso."""
    return {m.lower() for m in re.findall(
        r"\b(?:from|join)\s+\"?([a-zA-Z_][a-zA-Z0-9_]*)\"?", sql, re.IGNORECASE)}


AGREGADO = re.compile(r"\b(count|sum|avg|min|max)\s*\(", re.IGNORECASE)


def _sin_subconsultas(sql):
    """Quita subconsultas/CTEs: cada grupo entre paréntesis que contiene un
    SELECT en cualquier nivel de anidamiento (incluidas llamadas a función
    internas, como COUNT(*)) se reemplaza por un espacio, para poder
    inspeccionar solo el nivel superior de la consulta. Las llamadas a
    función que SÍ quedan en el nivel superior (COUNT(x), MAX(x)) no se
    tocan. ponytail: heurística por conteo de paréntesis, no un parser SQL
    real; no distingue un "select" dentro de un literal de cadena."""
    salida = []
    i, n = 0, len(sql)
    while i < n:
        if sql[i] != "(":
            salida.append(sql[i])
            i += 1
            continue
        profundidad, j = 1, i + 1
        while j < n and profundidad > 0:
            if sql[j] == "(":
                profundidad += 1
            elif sql[j] == ")":
                profundidad -= 1
            j += 1
        grupo = sql[i:j]
        salida.append(" " if re.search(r"\bselect\b", grupo, re.IGNORECASE) else grupo)
        i = j
    return "".join(salida)


def _nombres_cte(sql):
    """Nombres definidos en `nombre AS (SELECT ...)`: ya vienen agregados por
    paciente_id (así lo enseña agente_general.md), no cuentan como colección
    hija para el chequeo de JOIN cartesiano."""
    return {n.lower() for n in re.findall(
        r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s+as\s*\(\s*select\b", sql, re.IGNORECASE)}


def _riesgo_join_cartesiano(sql):
    """2+ colecciones "hijas" (cualquier tabla distinta de `patient` y de los
    CTEs de la propia consulta) unidas con JOIN/FROM directo en el nivel
    superior, junto con un agregado (COUNT/SUM/AVG/MIN/MAX): la firma exacta
    de un conteo inflado por producto cartesiano. Las subconsultas y CTEs
    quedan fuera del análisis porque ya agregan cada colección por separado
    antes de unirla."""
    nivel_superior = _sin_subconsultas(sql)
    if not AGREGADO.search(nivel_superior):
        return False
    hijas = tablas_en_sql(nivel_superior) - {"patient"} - _nombres_cte(sql)
    return len(hijas) >= 2


def validar(sql, permitidas=None):
    # admite "WITH ... SELECT" (CTE): las reglas recomiendan CTEs agregados
    # por paciente_id para evitar JOINs encadenados que inflan conteos.
    if not sql or not re.match(r"(?is)^\s*(with\b.*)?select\b", sql):
        return "La consulta generada no es un SELECT."
    if PROHIBIDO.search(sql):
        return "La consulta contiene operaciones no permitidas."
    if _riesgo_join_cartesiano(sql):
        return ("La consulta une 2 o más colecciones 1-a-muchos con JOIN/FROM "
                "directo y calcula un agregado (COUNT/SUM/AVG/MIN/MAX): así se "
                "multiplican filas y el resultado sale inflado. Usa una "
                "subconsulta correlacionada o un CTE que agregue cada colección "
                "por separado (por paciente_id) antes de unirlas.")
    if permitidas is not None:
        ok = {t.lower() for t in permitidas}
        usadas = tablas_en_sql(sql)
        prohibidas = usadas - ok
        if prohibidas:
            return (f"La consulta intenta acceder a tablas no autorizadas: "
                    f"{', '.join(sorted(prohibidas))}. Autorizadas: {', '.join(sorted(ok)) or 'ninguna'}.")
    return None


def _ejecutar(conn, sql):
    try:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = 8000")
        cur.execute(sql)
        cols = [c[0] for c in cur.description]
        filas = [dict(zip(cols, r)) for r in cur.fetchmany(200)]
        cur.close()
        conn.rollback()  # nunca dejamos transacción abierta escribiendo
        return {"ok": True, "columnas": cols, "filas": filas}
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return {"ok": False, "error": f"Error al ejecutar: {e}"}


def _intentar(conn, system, pregunta, tablas_permitidas, temperature=0.1):
    """Un intento completo: LLM -> validar -> ejecutar. `validar()` (SELECT,
    tablas prohibidas, riesgo de JOIN cartesiano) y `_ejecutar()` (error real
    de Postgres) comparten el mismo resultado {ok, error} para que ambos
    disparen el mismo reintento de abajo con la misma pista al LLM."""
    crudo = llamar_llm(system, pregunta, temperature)
    sql, explicacion = extraer_sql(crudo)
    sql = arreglar_flechas_encadenadas(sql)
    error = validar(sql, tablas_permitidas)
    resultado = {"ok": False, "error": error} if error else _ejecutar(conn, sql)
    return sql, explicacion, crudo, resultado


def preguntar(conn, pregunta, fuente="fhir", tablas_permitidas=None):
    extra = ""
    if tablas_permitidas is not None:
        lista = ", ".join(tablas_permitidas) if tablas_permitidas else "(ninguna)"
        extra = ("\n\n## ACCESO A DATOS (obligatorio)\n"
                 f"SOLO puedes consultar estas tablas/vistas: {lista}. "
                 "No uses ninguna otra. Si la pregunta requiere una tabla no "
                 "autorizada, explícalo en la explicación y no la consultes.")
    system = system_prompt(conn, fuente) + extra
    phash = hashlib.sha256(system.encode()).hexdigest()[:12]

    sql, explicacion, crudo, resultado = _intentar(conn, system, pregunta, tablas_permitidas)
    reintento = False
    if not resultado["ok"]:
        # Auto-corrección: un solo reintento con más temperatura (a 0.1 el
        # LLM tiende a repetir el mismo SQL fallido letra por letra) + pistas
        # concretas sobre los dos errores más comunes al unir varias tablas.
        reintento = True
        correccion = (f"Esta consulta SQL falló:\n{sql}\n\nError: {resultado['error']}\n\n"
                      "Corrígela: usa un alias distinto por cada tabla (p. ej. pat, proc, "
                      "med, imm — nunca repitas el mismo alias en dos tablas), y recuerda "
                      "que gender es 'male'/'female' (no 'M'/'F'). Responde de nuevo, en "
                      "el mismo formato JSON.")
        sql2, explicacion2, crudo2, resultado2 = _intentar(
            conn, system, pregunta + "\n\n" + correccion, tablas_permitidas, temperature=0.6)
        if sql2 == sql:
            return {"ok": False, "sql": sql, "crudo": crudo2, "reintento": True,
                    "prompt_hash": phash,
                    "error": f"El LLM repitió el mismo SQL tras el reintento: {resultado['error']}"}
        sql, explicacion, crudo, resultado = sql2, explicacion2, crudo2, resultado2

    if not resultado["ok"]:
        return {"ok": False, "error": resultado["error"], "sql": sql, "crudo": crudo,
                "reintento": reintento, "prompt_hash": phash}
    return {"ok": True, "sql": sql, "explicacion": explicacion,
            "columnas": resultado["columnas"], "filas": resultado["filas"],
            "n": len(resultado["filas"]), "reintento": reintento, "prompt_hash": phash}
