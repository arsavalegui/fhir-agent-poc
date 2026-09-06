"""Agente text-to-SQL: traduce una pregunta a SQL de PostgreSQL usando un LLM
(vía OmniRoute, OpenAI-compatible), la ejecuta en solo-lectura y devuelve la
respuesta + la consulta para verificar."""
import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

LLM_URL = os.environ.get("LLM_URL", "http://host.docker.internal:11434/v1")
MODELO = os.environ.get("LLM_MODELO", "qwen2.5-coder:3b")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "local")
# LLM de respaldo (opcional): solo entra en el reintento de auto-corrección,
# donde el modelo local ya falló una vez. Sin url Y modelo, no hay respaldo.
LLM_URL_RESPALDO = os.environ.get("LLM_URL_RESPALDO", "")
LLM_MODELO_RESPALDO = os.environ.get("LLM_MODELO_RESPALDO", "")
LLM_API_KEY_RESPALDO = os.environ.get("LLM_API_KEY_RESPALDO", "local")
CONFIG_DIR = os.environ.get("CONFIG_DIR", "/config")

HOSTS_LOCALES = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

# Solo se permite una única sentencia SELECT (o WITH ... SELECT); el `;` se
# revisa aparte en validar(), no aquí (ver el comentario de esa función).
# `into` escribe (SELECT ... INTO tabla) y las funciones listadas leen el
# servidor, no los datos: no hacen falta para responder ninguna pregunta.
PROHIBIDO = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant|"
                       r"revoke|copy|into|current_setting|set_config|pg_read_file|"
                       r"pg_read_binary_file|pg_ls_dir|pg_stat_file|pg_sleep|"
                       r"lo_import|lo_export|dblink)\b", re.IGNORECASE)


def verificar_llm_externo():
    """La pregunta y el catálogo viajan en el prompt: si el LLM (principal o de
    respaldo) no corre en esta máquina, los datos de pacientes salen de aquí.
    Exige la bandera explícita PERMITIR_LLM_EXTERNO=1. Se llama al importar,
    para que un .env mal puesto reviente al arrancar y no en la primera
    pregunta. Lee del entorno (no de los globales) para poder probarla."""
    if os.environ.get("PERMITIR_LLM_EXTERNO") == "1":
        return
    for nombre, defecto in (("LLM_URL", LLM_URL), ("LLM_URL_RESPALDO", LLM_URL_RESPALDO)):
        url = os.environ.get(nombre, defecto)
        if not url:
            continue
        # hostname es None si la URL no trae esquema: se rechaza (falla cerrado).
        if (urllib.parse.urlparse(url).hostname or "").lower() not in HOSTS_LOCALES:
            raise RuntimeError(
                f"{nombre}={url} apunta fuera de esta máquina y las preguntas "
                "llevan datos de pacientes en el prompt. Si es a propósito, "
                "pon PERMITIR_LLM_EXTERNO=1.")


verificar_llm_externo()

# Claves de "plomería" del formato FHIR (no son datos de negocio): se excluyen
# del catálogo de campos para no inflar el prompt con ruido.
CLAVES_PLOMERIA = {"resourceType", "id", "meta", "text", "div", "extension",
                    "modifierExtension", "implicitRules", "language", "contained"}
MUESTRA_POR_TABLA = 200    # filas que se leen para inferir los campos de cada tabla
MAX_CAMPOS_POR_TABLA = 25  # tope por tabla para no disparar el tamaño del prompt

# Cache en memoria del catálogo, por conjunto de tablas: así el system prompt
# es BYTE-IDÉNTICO entre preguntas con el mismo acceso (Ollama puede reusar su
# cache de prompt si no cambia una letra) y cada combinación de tablas
# permitidas guarda la suya.
_catalogo_cache = {}  # tupla de tablas -> texto del catálogo


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


def catalogo_campos(conn, permitidas=None):
    """Catálogo compacto `tabla: campo1, campo2, ...`, generado desde los datos
    (jsonb_object_keys sobre una muestra), no a mano. Cacheado en memoria; ver
    _catalogo_cache. Con `permitidas`, solo esas tablas: listar las 18 cuando
    el usuario autorizó 3 nada más confunde al modelo (consulta las que no
    debe y validar() lo rechaza)."""
    cur = conn.cursor()
    cur.execute("""SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name""")
    tablas = tuple(r[0] for r in cur.fetchall())
    if permitidas is not None:
        ok = {t.lower() for t in permitidas}
        tablas = tuple(t for t in tablas if t.lower() in ok)
    if tablas in _catalogo_cache:
        cur.close()
        return _catalogo_cache[tablas]
    lineas = []
    for tabla in tablas:
        campos = _campos_de_tabla(cur, tabla)
        lineas.append(f"{tabla}: {', '.join(campos)}" if campos else f"{tabla}: (sin muestra)")
    cur.close()
    texto = "\n".join(lineas)
    _catalogo_cache[tablas] = texto
    return texto


def system_prompt(conn, fuente="fhir", tablas_permitidas=None):
    return "\n\n".join(filter(None, [
        leer("agente_general.md"),
        "## DESCRIPCIÓN DE LA FUENTE DE DATOS\n\n" + leer("fuentes", fuente, "descripcion.md"),
        "## CATÁLOGO DE CAMPOS (inferido de los datos; usa estas expresiones "
        "EXACTAS, no las inventes ni las combines distinto)\n\n"
        + catalogo_campos(conn, tablas_permitidas),
        "## REGLAS PARA ESTA FUENTE\n\n" + leer("fuentes", fuente, "reglas.md"),
    ]))


def proveedor_local():
    """Los globales se leen en cada llamada a propósito: evaluar.py sobrescribe
    agente.MODELO para correr el golden set con otro modelo."""
    return {"url": LLM_URL, "modelo": MODELO, "key": LLM_API_KEY}


def proveedor_respaldo():
    """Config del LLM de respaldo, o None si no está configurado (hacen falta
    url y modelo; sin los dos no hay a dónde escalar)."""
    if LLM_URL_RESPALDO and LLM_MODELO_RESPALDO:
        return {"url": LLM_URL_RESPALDO, "modelo": LLM_MODELO_RESPALDO,
                "key": LLM_API_KEY_RESPALDO}
    return None


def llamar_llm(system, pregunta, temperature=0.1, proveedor=None):
    """`proveedor` = {"url", "modelo", "key"}; por defecto, el principal."""
    # Un proveedor saturado puede devolver 429; reintentamos con backoff. Los
    # que necesitan cuenta conectada responden 403/418: se reintenta igual por
    # si es transitorio, sin dormir.
    proveedor = proveedor or proveedor_local()
    ultimo = None
    for intento in range(6):
        body = json.dumps({
            "model": proveedor["modelo"], "stream": False, "temperature": temperature,
            # El endpoint OpenAI-compatible de Ollama IGNORA "options" (probado):
            # el num_ctx real se fija en el Modelfile del modelo (ver README,
            # `ollama create qwen2.5-coder-fhir:3b`). Mandamos igual esta clave
            # por si otro backend/proveedor sí la respeta; no hace daño si no.
            "options": {"num_ctx": 8192},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": pregunta}],
        }).encode()
        req = urllib.request.Request(proveedor["url"] + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {proveedor['key']}"})
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
                continue                 # reintenta de inmediato, sin dormir
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


# ponytail: un `--` dentro de un literal de cadena también se borra; el SQL
# resultante truena en Postgres y dispara el reintento, que es lo que quiero
# antes que dejar pasar un comentario que esconda una tabla.
SIN_COMENTARIO = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

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


# Tras FROM/JOIN: nombre de tabla, no llamada a función. El `\b` tras el
# grupo impide que el motor recorte el identificador para esquivar el
# lookahead (`jsonb_array_elements(` -> `jsonb_array_element` + `s(`).
TABLA_TRAS_FROM = re.compile(
    r"\b(?:from|join)\s+(?:lateral\s+)?\"?([a-zA-Z_][a-zA-Z0-9_]*)\b\"?(?!\s*\()",
    re.IGNORECASE)
# `LATERAL` es palabra clave, no tabla: `CROSS JOIN LATERAL jsonb_array_elements(...)`
# (el catálogo enseña esa función, así que el LLM la usa seguido).
NO_SON_TABLAS = {"lateral"}

# Fin de la lista de tablas de un FROM. Los JOIN no cortan: la coma puede
# venir después (`FROM a p JOIN a q ON true, observation o`).
FIN_LISTA_FROM = re.compile(
    r"\b(?:where|group|order|having|limit|offset|union|intersect|except|window|"
    r"fetch)\b", re.IGNORECASE)
# Tabla listada con coma dentro del FROM; misma regla que TABLA_TRAS_FROM
# (ni LATERAL ni llamadas a función).
TABLA_TRAS_COMA = re.compile(
    r",\s*(?!lateral\b)\"?([a-zA-Z_][a-zA-Z0-9_]*)\b\"?(?!\s*\()", re.IGNORECASE)


def _niveles(sql):
    """El SQL y el contenido de cada grupo de paréntesis, cada uno con sus
    grupos internos reducidos a `()`. Así la lista del FROM de un nivel se
    lee entera (una subconsulta o una función es UN item opaco) sin que las
    comas del nivel de adentro se cuelen en la del de afuera."""
    niveles, pila = [], [[]]
    for c in sql:
        if c == "(":
            pila[-1].append("()")
            pila.append([])
        elif c == ")" and len(pila) > 1:
            niveles.append("".join(pila.pop()))
        else:
            pila[-1].append(c)
    while pila:
        niveles.append("".join(pila.pop()))
    return niveles


def _tablas_con_coma(sql):
    """`FROM patient p, observation o`: la segunda tabla no va tras FROM ni
    JOIN, así que TABLA_TRAS_FROM no la ve y se colaba el acceso. Solo se
    miran las comas ENTRE el FROM y la siguiente palabra clave, para no
    confundirlas con las de la lista del SELECT."""
    nombres = set()
    for nivel in _niveles(sql):
        for m in re.finditer(r"\bfrom\b", nivel, re.IGNORECASE):
            fin = FIN_LISTA_FROM.search(nivel, m.end())
            lista = nivel[m.end():fin.start() if fin else len(nivel)]
            nombres.update(n.lower() for n in TABLA_TRAS_COMA.findall(lista))
    return nombres


# `EXTRACT(YEAR FROM x)`, `SUBSTRING(x FROM 1 FOR 3)`, `TRIM(BOTH ' ' FROM x)`,
# `OVERLAY(x PLACING y FROM 2)`: ese FROM es sintaxis de la función, no una
# tabla. `[^()]*` no cruza paréntesis, así que nunca alcanza un FROM de verdad.
FROM_DE_FUNCION = re.compile(
    r"(\b(?:extract|substring|trim|overlay)\s*\([^()]*)\bfrom\b", re.IGNORECASE)


def tablas_en_sql(sql):
    """Nombres que aparecen tras FROM/JOIN o listados con coma en el FROM,
    para validar el acceso. Ignora las llamadas a función
    (`FROM jsonb_array_elements(...)`), `LATERAL` y el FROM interno de
    EXTRACT y compañía."""
    sql = FROM_DE_FUNCION.sub(r"\1 ", sql)
    return ({m.lower() for m in TABLA_TRAS_FROM.findall(sql)}
            | _tablas_con_coma(sql)) - NO_SON_TABLAS


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


# Cabecera de un CTE, en sus tres formas: `nombre AS (`, `nombre (cols) AS (`
# y `nombre AS [NOT] MATERIALIZED (`. Termina JUSTO en el paréntesis que abre
# el cuerpo (m.end()-1 es su posición), con el SELECT en un lookahead para no
# confundir un CTE con cualquier otro `algo AS (...)`.
CABECERA_CTE = re.compile(
    r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*(?:\([^()]*\)\s*)?\bas\s*"
    r"(?:(?:not\s+)?materialized\s*)?\((?=\s*select\b)", re.IGNORECASE)


def _nombres_cte(sql):
    """Nombres definidos en `nombre AS (SELECT ...)`: ya vienen agregados por
    paciente_id (así lo enseña agente_general.md), no cuentan como colección
    hija para el chequeo de JOIN cartesiano."""
    return {m.group(1).lower() for m in CABECERA_CTE.finditer(sql)}


def _partes_cte(sql):
    """`[(nombre, cuerpo), ...]` en orden de declaración, y el resto de la
    consulta (todo menos los cuerpos), para analizar cada alcance por
    separado. Los CTEs anidados dentro de un cuerpo no salen en la lista:
    aparecen al volver a llamar a esta función sobre ese cuerpo."""
    ctes, resto, pos = [], [], 0
    for m in CABECERA_CTE.finditer(sql):
        if m.start() < pos:      # cabecera dentro de un cuerpo ya tomado
            continue
        profundidad, i = 1, m.end()
        while i < len(sql) and profundidad > 0:
            if sql[i] == "(":
                profundidad += 1
            elif sql[i] == ")":
                profundidad -= 1
            i += 1
        ctes.append((m.group(1).lower(), sql[m.end():i - 1]))
        resto.append(sql[pos:m.end()])
        pos = i - 1
    resto.append(sql[pos:])
    return ctes, " ".join(resto)


def _tablas_por_alcance(sql, visibles=frozenset()):
    """Tablas REALES que lee la consulta, respetando el alcance de los CTEs:
    dentro del cuerpo de un CTE solo tapan tablas los CTEs declarados ANTES.
    Restar todos los nombres de golpe abría el filtro de acceso: un CTE
    llamado `observation` borraba la tabla `observation` que él mismo lee."""
    ctes, resto = _partes_cte(sql)
    usadas, vistos = set(), set(visibles)
    for nombre, cuerpo in ctes:
        usadas |= _tablas_por_alcance(cuerpo, vistos)
        vistos.add(nombre)
    return usadas | (tablas_en_sql(resto) - vistos)


def _riesgo_nivel(fragmento, ctes):
    """2+ colecciones "hijas" (cualquier tabla distinta de `patient` y de los
    CTEs de la consulta) unidas con JOIN/FROM directo en ESTE nivel, junto con
    un agregado (COUNT/SUM/AVG/MIN/MAX): la firma exacta de un conteo inflado
    por producto cartesiano."""
    nivel = _sin_subconsultas(fragmento)
    if not AGREGADO.search(nivel):
        return False
    return len(tablas_en_sql(nivel) - {"patient"} - ctes) >= 2


def _riesgo_join_cartesiano(sql):
    """Revisa el nivel superior Y el cuerpo de cada CTE: el patrón inflado se
    puede esconder dentro del CTE (_sin_subconsultas los borra del nivel
    superior, así que ahí no se ve). Las subconsultas correlacionadas sí
    quedan fuera: ya agregan cada colección por separado antes de unirla."""
    ctes = _nombres_cte(sql)
    cuerpos = [cuerpo for _, cuerpo in _partes_cte(sql)[0]]
    return _riesgo_nivel(sql, ctes) or any(_riesgo_nivel(c, ctes) for c in cuerpos)


def validar(sql, permitidas=None):
    # Sin comentarios: escondían tablas de todos los chequeos de abajo
    # (`FROM patient p, /*x*/ observation o`). _intentar() ejecuta el mismo
    # SQL limpio.
    sql = SIN_COMENTARIO.sub(" ", sql or "")
    # admite "WITH ... SELECT" (CTE): las reglas recomiendan CTEs agregados
    # por paciente_id para evitar JOINs encadenados que inflan conteos.
    if not sql or not re.match(r"(?is)^\s*(with\b.*)?select\b", sql):
        return "La consulta generada no es un SELECT."
    # Un `;` al final es cosmético; cualquier otro son 2+ sentencias. Antes se
    # colaba `SELECT 1 ;COMMENT ON TABLE patient IS 'x'` por el espacio previo.
    # ponytail: un `;` dentro de un literal de cadena también rechaza (falla
    # cerrado, igual que PROHIBIDO); haría falta un parser SQL para afinarlo.
    if ";" in re.sub(r";\s*$", "", sql):
        return "La consulta debe ser una sola sentencia."
    if PROHIBIDO.search(sql):
        return "La consulta contiene operaciones no permitidas."
    # Los nombres de los CTE también salen tras FROM/JOIN, pero no son tablas
    # que autorizar; lo que sí se autoriza es la tabla que lee su cuerpo.
    usadas = _tablas_por_alcance(sql)
    if not usadas:
        # Sin FROM no hay tabla que autorizar y el filtro de abajo nunca
        # aplica: así pasaba `SELECT current_setting('data_directory')`.
        return "La consulta no lee ninguna tabla; solo se permite consultar datos."
    if _riesgo_join_cartesiano(sql):
        return ("La consulta une 2 o más colecciones 1-a-muchos con JOIN/FROM "
                "directo y calcula un agregado (COUNT/SUM/AVG/MIN/MAX): así se "
                "multiplican filas y el resultado sale inflado. Usa una "
                "subconsulta correlacionada o un CTE que agregue cada colección "
                "por separado (por paciente_id) antes de unirlas.")
    if permitidas is not None:
        ok = {t.lower() for t in permitidas}
        prohibidas = usadas - ok
        if prohibidas:
            return (f"La consulta intenta acceder a tablas no autorizadas: "
                    f"{', '.join(sorted(prohibidas))}. Autorizadas: {', '.join(sorted(ok)) or 'ninguna'}.")
    return None


def _columnas_unicas(cols):
    """`count, count, count` -> `count, count_2, count_3`. Postgres permite
    columnas con el mismo nombre (dos subconsultas escalares sin alias, o dos
    `?column?`); el dict de abajo las colapsaría y se perdería el valor de
    todas menos la última."""
    salida, usados = [], set()
    for col in cols:
        nombre, n = col, 1
        while nombre in usados:   # el while, y no un contador, por si el SQL
            n += 1                # ya trae una columna llamada `count_2`
            nombre = f"{col}_{n}"
        usados.add(nombre)
        salida.append(nombre)
    return salida


def _ejecutar(conn, sql):
    try:
        cur = conn.cursor()
        cur.execute("SET statement_timeout = 8000")
        cur.execute(sql)
        cols = _columnas_unicas([c[0] for c in cur.description])
        filas = [dict(zip(cols, r)) for r in cur.fetchmany(200)]
        cur.close()
        conn.rollback()  # nunca dejamos transacción abierta escribiendo
        return {"ok": True, "columnas": cols, "filas": filas}
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return {"ok": False, "error": f"Error al ejecutar: {e}"}


def _intentar(conn, system, pregunta, tablas_permitidas, temperature=0.1, proveedor=None):
    """Un intento completo: LLM -> validar -> ejecutar. `validar()` (SELECT,
    tablas prohibidas, riesgo de JOIN cartesiano) y `_ejecutar()` (error real
    de Postgres) comparten el mismo resultado {ok, error} para que ambos
    disparen el mismo reintento de abajo con la misma pista al LLM."""
    crudo = llamar_llm(system, pregunta, temperature, proveedor)
    sql, explicacion = extraer_sql(crudo)
    sql = arreglar_flechas_encadenadas(sql)
    sql = SIN_COMENTARIO.sub(" ", sql) if sql else sql  # se ejecuta lo mismo que se valida
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
    system = system_prompt(conn, fuente, tablas_permitidas) + extra
    phash = hashlib.sha256(system.encode()).hexdigest()[:12]

    sql, explicacion, crudo, resultado = _intentar(conn, system, pregunta, tablas_permitidas)
    reintento = False
    proveedor = "local"
    if not resultado["ok"]:
        # Auto-corrección: un solo reintento con más temperatura (a 0.1 el
        # LLM tiende a repetir el mismo SQL fallido letra por letra) + pistas
        # concretas sobre los dos errores más comunes al unir varias tablas.
        # Si hay LLM de respaldo, el reintento escala a él: el local ya
        # demostró que no le sale, repetirle la pregunta rinde poco.
        reintento = True
        respaldo = proveedor_respaldo()
        proveedor = "respaldo" if respaldo else "local"
        correccion = (f"Esta consulta SQL falló:\n{sql}\n\nError: {resultado['error']}\n\n"
                      # Sin ejemplos de nombres: cuando la pista sugería "pat, proc, med,
                      # imm", el modelo los usaba como nombres de CTE y chocaban con los
                      # alias de tabla (visto en g13).
                      "Corrígela: usa un alias distinto por cada tabla y no repitas un "
                      "nombre entre tablas, alias y CTEs; recuerda que gender es "
                      "'male'/'female' (no 'M'/'F'). Responde de nuevo, en el mismo "
                      "formato JSON.")
        sql2, explicacion2, crudo2, resultado2 = _intentar(
            conn, system, pregunta + "\n\n" + correccion, tablas_permitidas,
            temperature=0.6, proveedor=respaldo)
        if sql2 == sql:
            return {"ok": False, "sql": sql, "crudo": crudo2, "reintento": True,
                    "prompt_hash": phash, "proveedor": proveedor,
                    "error": f"El LLM repitió el mismo SQL tras el reintento: {resultado['error']}"}
        sql, explicacion, crudo, resultado = sql2, explicacion2, crudo2, resultado2

    if not resultado["ok"]:
        return {"ok": False, "error": resultado["error"], "sql": sql, "crudo": crudo,
                "reintento": reintento, "prompt_hash": phash, "proveedor": proveedor}
    return {"ok": True, "sql": sql, "explicacion": explicacion,
            "columnas": resultado["columnas"], "filas": resultado["filas"],
            "n": len(resultado["filas"]), "reintento": reintento,
            "prompt_hash": phash, "proveedor": proveedor}
