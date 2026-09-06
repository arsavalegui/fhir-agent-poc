"""Prueba mínima y autocontenida del flujo de auto-corrección (reintento) de
`preguntar()`: sin base de datos ni LLM reales, con dobles simples. Corre con
`python3 test_agente.py` (sin frameworks)."""
import contextlib
import os
import re

import agente


@contextlib.contextmanager
def parchar_llm(llamar_llm):
    """Reemplaza `agente.system_prompt` (fijo, no relevante para las pruebas)
    y `agente.llamar_llm` por dobles, y restaura los originales al salir
    (incluso si la prueba falla) para que un test no deje estado que
    contamine al siguiente."""
    system_prompt_original = agente.system_prompt
    llamar_llm_original = agente.llamar_llm
    agente.system_prompt = lambda conn, fuente="fhir", tablas_permitidas=None: "SYSTEM DE PRUEBA"
    agente.llamar_llm = llamar_llm
    try:
        yield
    finally:
        agente.system_prompt = system_prompt_original
        agente.llamar_llm = llamar_llm_original


@contextlib.contextmanager
def parchar_respaldo(url, modelo, key="secreta"):
    """Configura el LLM de respaldo (los globales que lee proveedor_respaldo)
    y los restaura al salir."""
    previos = (agente.LLM_URL_RESPALDO, agente.LLM_MODELO_RESPALDO, agente.LLM_API_KEY_RESPALDO)
    agente.LLM_URL_RESPALDO, agente.LLM_MODELO_RESPALDO, agente.LLM_API_KEY_RESPALDO = url, modelo, key
    try:
        yield
    finally:
        (agente.LLM_URL_RESPALDO, agente.LLM_MODELO_RESPALDO,
         agente.LLM_API_KEY_RESPALDO) = previos


@contextlib.contextmanager
def parchar_env(**valores):
    """Pone (o quita, con None) variables de entorno y restaura al salir."""
    previos = {k: os.environ.get(k) for k in valores}

    def aplicar(pares):
        for clave, valor in pares.items():
            if valor is None:
                os.environ.pop(clave, None)
            else:
                os.environ[clave] = valor

    aplicar(valores)
    try:
        yield
    finally:
        aplicar(previos)


class ConnFalso:
    """La primera consulta ejecutada falla (columna inexistente, como si el
    LLM hubiera tratado un campo jsonb como columna); la segunda, ya
    corregida, tiene éxito."""
    def __init__(self):
        self.ejecuciones = 0

    def cursor(self):
        return CursorFalso(self)

    def rollback(self):
        pass


class CursorFalso:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *args):
        if sql.startswith("SET"):
            return
        self.conn.ejecuciones += 1
        if self.conn.ejecuciones == 1:
            raise Exception('column "p.gender" does not exist')
        self.description = [("total",)]
        self._fila = (8,)

    def fetchmany(self, n):
        return [self._fila]

    def close(self):
        pass


def test_preguntar_reintenta_y_corrige():
    conn = ConnFalso()
    respuestas = iter([
        '{"sql": "SELECT p.gender FROM patient p", "explicacion": "mal: p.gender no es columna"}',
        '{"sql": "SELECT count(*) AS total FROM patient", "explicacion": "corregido"}',
    ])
    with parchar_llm(lambda system, pregunta, temperature=0.1, proveedor=None: next(respuestas)):
        r = agente.preguntar(conn, "¿cuántos pacientes hay?", tablas_permitidas=["patient"])

    assert r["ok"] is True, r
    assert r["reintento"] is True, r
    assert r["sql"] == "SELECT count(*) AS total FROM patient", r
    assert r["filas"] == [{"total": 8}], r
    assert conn.ejecuciones == 2, "debe haber ejecutado dos veces: la fallida y la corregida"
    print("OK: preguntar() reintenta una vez con el error exacto y se corrige.")


def test_preguntar_sin_error_no_reintenta():
    conn = ConnFalso()
    conn.ejecuciones = 1  # ya "gastamos" la falla para que la única ejecución tenga éxito
    with parchar_llm(lambda system, pregunta, temperature=0.1, proveedor=None: '{"sql": "SELECT count(*) AS total FROM patient"}'):
        r = agente.preguntar(conn, "¿cuántos pacientes hay?", tablas_permitidas=["patient"])

    assert r["ok"] is True, r
    assert r["reintento"] is False, r
    print("OK: sin error de Postgres, no hay reintento.")


class CursorSiempreFalla:
    """Ambas ejecuciones (original y la corregida tras el reintento) fallan
    en Postgres: simula un SQL con el mismo tipo de error persistente."""
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, *args):
        if sql.startswith("SET"):
            return
        self.conn.ejecuciones += 1
        raise Exception('operator does not exist: text ->> unknown')

    def close(self):
        pass


class ConnSiempreFalla:
    def __init__(self):
        self.ejecuciones = 0

    def cursor(self):
        return CursorSiempreFalla(self)

    def rollback(self):
        pass


def test_preguntar_agota_reintento_y_descarta_primer_sql():
    """Cuando el reintento también falla, preguntar() debe: (a) reportar
    ok=False y reintento=True, y (b) devolver el SQL/error de la SEGUNDA
    corrida, no la primera (documenta el hueco: el SQL y el error de Postgres
    del primer intento no quedan en la respuesta)."""
    conn = ConnSiempreFalla()
    respuestas = iter([
        '{"sql": "SELECT m.recurso->>\'x\'->>\'text\' FROM medicationrequest m", "explicacion": "intento 1"}',
        '{"sql": "SELECT m.recurso->>\'y\'->>\'text\' FROM medicationrequest m", "explicacion": "intento 2 (mismo tipo de error)"}',
    ])
    with parchar_llm(lambda system, pregunta, temperature=0.1, proveedor=None: next(respuestas)):
        r = agente.preguntar(conn, "¿cuántos medicamentos distintos por paciente?")

    assert r["ok"] is False, r
    assert r["reintento"] is True, r
    assert "y" in r["sql"], "debe devolver el SQL del SEGUNDO intento, no el primero"
    assert "operator does not exist" in r["error"], r
    print("OK: cuando ambos intentos fallan, ok=False, reintento=True, y solo "
          "se conserva el SQL/error del segundo intento (el primero se pierde).")


def test_riesgo_join_cartesiano_detecta_el_bug_real():
    # el caso real reportado: 2 colecciones hijas unidas directo + agregados
    sql = ("SELECT p.paciente_id, COUNT(m.recurso->>'id') AS recetas, "
           "MAX(i.recurso->>'occurrenceDateTime')::date AS ultima_vacuna "
           "FROM patient p "
           "JOIN medicationrequest m ON p.paciente_id = m.paciente_id "
           "JOIN immunization i ON p.paciente_id = i.paciente_id "
           "WHERE EXISTS (SELECT 1 FROM procedure pr WHERE pr.paciente_id = p.paciente_id) "
           "GROUP BY 1 ORDER BY recetas DESC")
    assert agente._riesgo_join_cartesiano(sql), "debe detectar el JOIN cartesiano con agregados"
    print("OK: _riesgo_join_cartesiano detecta 2+ hijas con JOIN directo y agregado.")


def test_riesgo_join_cartesiano_no_marca_falsos_positivos():
    casos = {
        "un solo JOIN hijo + agregado (few-shot sancionado)":
            "SELECT p.paciente_id, count(*) AS emergencias FROM patient p "
            "JOIN encounter e ON p.paciente_id = e.paciente_id "
            "WHERE e.recurso->'class'->>'code' = 'EMER' GROUP BY 1",
        "2 hijas unidas pero SIN agregado (listado DISTINCT, few-shot sancionado)":
            "SELECT DISTINCT m.recurso->'medicationCodeableConcept'->>'text' AS medicamento "
            "FROM condition c JOIN medicationrequest m ON m.paciente_id = c.paciente_id "
            "WHERE c.recurso->'code'->>'text' ILIKE '%prediabetes%'",
        "subconsultas correlacionadas (few-shot ancla de alergias)":
            "SELECT p.recurso->>'gender', "
            "(SELECT count(*) FROM condition c WHERE c.paciente_id = p.paciente_id) AS condiciones, "
            "(SELECT count(*) FROM encounter e WHERE e.paciente_id = p.paciente_id "
            "AND e.recurso->'class'->>'code' = 'EMER') AS emergencias "
            "FROM patient p WHERE EXISTS "
            "(SELECT 1 FROM allergyintolerance a WHERE a.paciente_id = p.paciente_id)",
    }
    for nombre, sql in casos.items():
        assert not agente._riesgo_join_cartesiano(sql), f"falso positivo en: {nombre}"
    print("OK: _riesgo_join_cartesiano no rechaza los patrones ya sancionados en reglas.md.")


def test_preguntar_rechaza_join_cartesiano_y_reintenta():
    conn = ConnFalso()
    conn.ejecuciones = 1  # que la única ejecución real (la corregida) tenga éxito
    respuestas = iter([
        '{"sql": "SELECT p.paciente_id, COUNT(m.recurso->>\'id\') AS recetas, '
        'MAX(i.recurso->>\'occurrenceDateTime\')::date FROM patient p '
        'JOIN medicationrequest m ON p.paciente_id = m.paciente_id '
        'JOIN immunization i ON p.paciente_id = i.paciente_id GROUP BY 1", '
        '"explicacion": "mal: JOIN cartesiano"}',
        '{"sql": "SELECT count(*) AS total FROM patient", "explicacion": "corregido con CTE"}',
    ])
    with parchar_llm(lambda system, pregunta, temperature=0.1, proveedor=None: next(respuestas)):
        r = agente.preguntar(conn, "recetas y vacunas por paciente", tablas_permitidas=[
            "patient", "medicationrequest", "immunization"])

    assert r["ok"] is True, r
    assert r["reintento"] is True, r
    assert r["sql"] == "SELECT count(*) AS total FROM patient", r
    # conn.ejecuciones arrancó en 1: solo subió a 2, o sea UNA ejecución real.
    # El SQL cartesiano lo frenó validar(), nunca llegó a Postgres.
    assert conn.ejecuciones == 2, "el SQL cartesiano no debió ejecutarse"
    print("OK: preguntar() rechaza el JOIN cartesiano por validar() (sin ejecutarlo) "
          "y se corrige en el reintento.")


def test_arreglar_flechas_encadenadas():
    casos = [
        ("m.recurso->>'medicationCodeableConcept'->>'text'",
         "m.recurso->'medicationCodeableConcept'->>'text'"),
        ("i.recurso->>'vaccineCode'->>'text'", "i.recurso->'vaccineCode'->>'text'"),
        # 3 niveles: solo el último ->> se queda como ->>
        ("recurso->>'a'->>'b'->>'c'", "recurso->'a'->'b'->>'c'"),
        # ya correcto: no se toca
        ("recurso->'class'->>'code'", "recurso->'class'->>'code'"),
        ("recurso->>'gender'", "recurso->>'gender'"),
    ]
    for entrada, esperado in casos:
        resultado = agente.arreglar_flechas_encadenadas(entrada)
        assert resultado == esperado, f"{entrada!r} -> {resultado!r}, esperaba {esperado!r}"
    print("OK: arreglar_flechas_encadenadas corrige ->>'x'->> encadenado sin tocar lo que ya está bien.")


def test_preguntar_arregla_flechas_antes_de_ejecutar():
    conn = ConnFalso()
    conn.ejecuciones = 1  # que la única ejecución (ya arreglada) tenga éxito
    with parchar_llm(lambda system, pregunta, temperature=0.1, proveedor=None: (
        '{"sql": "SELECT count(*) AS total FROM patient WHERE '
        'recurso->>\'a\'->>\'b\' = \'x\'", "explicacion": "con flecha mala"}')):
        r = agente.preguntar(conn, "¿cuántos pacientes hay?", tablas_permitidas=["patient"])

    assert r["ok"] is True, r
    assert r["reintento"] is False, "se arregla antes de ejecutar, no debería hacer falta reintento"
    assert "->>'a'->'b'" not in r["sql"] and "->'a'->>'b'" in r["sql"], r["sql"]
    print("OK: preguntar() arregla la flecha encadenada antes de validar/ejecutar.")


def test_riesgo_join_cartesiano_no_cuenta_ctes_como_hijas():
    # 2 CTEs ya agregados por paciente_id + un agregado real en el nivel
    # superior (SUM sobre columnas de los CTEs): antes se contaban como 2
    # "hijas" y se rechazaba de más; los CTEs no deben contar.
    sql = ("WITH procedimientos AS (SELECT paciente_id, count(*) AS n FROM procedure GROUP BY 1), "
           "medicamentos AS (SELECT paciente_id, count(DISTINCT recurso->'medicationCodeableConcept'"
           "->>'text') AS n FROM medicationrequest GROUP BY 1) "
           "SELECT p.paciente_id, SUM(procedimientos.n) AS total FROM patient p "
           "JOIN procedimientos ON procedimientos.paciente_id = p.paciente_id "
           "JOIN medicamentos ON medicamentos.paciente_id = p.paciente_id GROUP BY 1")
    assert not agente._riesgo_join_cartesiano(sql), "los CTEs no deben contar como hijas"
    print("OK: _riesgo_join_cartesiano no cuenta los nombres de CTE como colecciones hijas.")


def test_preguntar_no_reintenta_dos_veces_si_llm_repite_sql():
    conn = ConnSiempreFalla()
    mismo_sql = '{"sql": "SELECT p.recurso FROM patient p JOIN procedure p ON true", "explicacion": "x"}'
    llamadas = []

    def llm_falso(system, pregunta, temperature=0.1, proveedor=None):
        llamadas.append(temperature)
        return mismo_sql

    with parchar_llm(llm_falso):
        r = agente.preguntar(conn, "¿cuántos procedimientos por paciente?")

    assert r["ok"] is False, r
    assert "repitió el mismo SQL" in r["error"], r
    assert llamadas == [0.1, 0.6], "el reintento debe usar más temperatura"
    # _intentar SÍ ejecuta el segundo SQL (por eso son 2); lo que no hay es un
    # tercer intento: al ver que el LLM repitió el SQL, preguntar() se rinde.
    assert conn.ejecuciones == 2, "un intento y su reintento, no más"
    print("OK: si el reintento repite el SQL (ya ejecutado y fallido), se reporta "
          "error claro y no hay un tercer intento.")


def test_validar_rechaza_una_segunda_sentencia():
    # el espacio antes del `;` esquivaba el `;.*\S` de PROHIBIDO
    error = agente.validar("SELECT 1 FROM patient ;COMMENT ON TABLE patient IS 'x'")
    assert error and "una sola sentencia" in error, error
    assert agente.validar("SELECT count(*) FROM patient;") is None, "el `;` final sí se permite"
    print("OK: validar() rechaza dos sentencias aunque el `;` traiga espacios alrededor.")


def test_validar_rechaza_consulta_sin_tabla():
    # sin FROM, tablas_en_sql queda vacío y el filtro de tablas no aplicaba
    for sql in ("SELECT 1", "SELECT version()"):
        error = agente.validar(sql, permitidas=["patient"])
        assert error and "no lee ninguna tabla" in error, (sql, error)
    print("OK: validar() rechaza un SELECT sin tabla (antes esquivaba el filtro de acceso).")


def test_validar_rechaza_funciones_de_sistema_e_into():
    # con FROM sí hay tabla permitida, así que el filtro de acceso no los para:
    # leen el servidor (o escriben) en vez de los datos
    casos = ["SELECT current_setting('listen_addresses') FROM patient",
             "SELECT pg_read_file('/etc/passwd') FROM patient",
             "SELECT pg_sleep(10) FROM patient",
             "SELECT recurso INTO copia FROM patient"]
    for sql in casos:
        error = agente.validar(sql, permitidas=["patient"])
        assert error and "no permitidas" in error, (sql, error)
    print("OK: validar() rechaza INTO y las funciones de sistema aunque haya un FROM válido.")


def test_validar_ve_las_tablas_tras_comillas_y_comentarios():
    coladas = ['SELECT o.recurso FROM patient p, "observation" o',
               "SELECT o.recurso FROM patient p, /*x*/ observation o",
               "SELECT o.recurso FROM patient p, --x\n observation o"]
    for sql in coladas:
        error = agente.validar(sql, permitidas=["patient"])
        assert error and "observation" in error, (sql, error)
    validos = ['SELECT count(*) FROM "patient"',
               'SELECT count(*) FROM patient p, "patient" q',
               "-- cuenta pacientes\nSELECT count(*) FROM patient /* ok */"]
    for sql in validos:
        assert agente.validar(sql, permitidas=["patient"]) is None, \
            (sql, agente.validar(sql, ["patient"]))
    print("OK: ni las comillas ni los comentarios esconden una tabla del filtro de acceso.")


def test_preguntar_ejecuta_el_sql_sin_comentarios():
    conn = ConnFalso()
    conn.ejecuciones = 1  # que la única ejecución tenga éxito
    with parchar_llm(lambda system, pregunta, temperature=0.1, proveedor=None: (
            '{"sql": "SELECT count(*) AS total FROM patient /* nota del modelo */"}')):
        r = agente.preguntar(conn, "¿cuántos pacientes hay?", tablas_permitidas=["patient"])

    assert r["ok"] is True, r
    assert "/*" not in r["sql"], r["sql"]
    print("OK: preguntar() ejecuta y devuelve el SQL ya sin comentarios.")


def test_tablas_en_sql_ignora_el_from_interno_de_extract():
    # el modelo usa EXTRACT para preguntas de años; el alias `p` de
    # `EXTRACT(YEAR FROM p.recurso->>'x')` se contaba como tabla no autorizada
    casos = ["SELECT count(*) FROM patient p WHERE "
             "EXTRACT(YEAR FROM p.recurso->>'birthDate') > 1999",
             "SELECT count(*) FROM patient p WHERE "
             "EXTRACT(YEAR FROM (p.recurso->>'birthDate')::date) > 1999",
             "SELECT TRIM(BOTH ' ' FROM p.recurso->>'gender') FROM patient p"]
    for sql in casos:
        assert agente.tablas_en_sql(sql) == {"patient"}, (sql, agente.tablas_en_sql(sql))
        assert agente.validar(sql, permitidas=["patient"]) is None, (sql, agente.validar(sql, ["patient"]))
    # un FROM de verdad, fuera de esas funciones, sigue contando
    real = "SELECT substring(o.recurso->>'x' FROM 1 FOR 3) FROM observation o"
    assert agente.tablas_en_sql(real) == {"observation"}, agente.tablas_en_sql(real)
    print("OK: tablas_en_sql no toma por tabla el FROM interno de EXTRACT/SUBSTRING/TRIM.")


def test_validar_ve_las_tablas_unidas_con_coma():
    # todas esconden `observation` tras una coma que no va pegada al FROM
    coladas = [
        "SELECT count(*) FROM patient p, observation o WHERE o.paciente_id = p.paciente_id",
        "SELECT o.recurso FROM (SELECT recurso FROM patient) s, observation o",
        "SELECT o.recurso FROM patient p JOIN patient q ON true, observation o",
        "SELECT o.recurso FROM patient p CROSS JOIN LATERAL "
        "jsonb_array_elements(p.recurso->'name') n, observation o",
        # coma dentro de una subconsulta: el nivel de adentro también se mira
        "SELECT s.n FROM (SELECT count(*) AS n FROM patient p, observation o) s",
    ]
    for sql in coladas:
        error = agente.validar(sql, permitidas=["patient"])
        assert error and "observation" in error, (sql, error)
    # lo que SÍ debe pasar: función tabular tras coma, LATERAL tras coma, y
    # una subconsulta cuya lista del SELECT trae comas (no son tablas)
    validos = [
        "SELECT n.value FROM patient p, jsonb_array_elements(p.recurso->'name') n",
        "SELECT n.value FROM patient p, LATERAL jsonb_array_elements(p.recurso->'name') n",
        "SELECT s.paciente_id FROM (SELECT paciente_id, gender FROM patient) s",
        "SELECT count(*) FROM patient p GROUP BY p.paciente_id, p.recurso->>'gender'",
    ]
    for sql in validos:
        assert agente.validar(sql, permitidas=["patient"]) is None, \
            (sql, agente.validar(sql, ["patient"]))
    print("OK: el coma-join cuenta como tabla en cualquier nivel, sin confundir funciones ni LATERAL.")


class CursorColumnasRepetidas:
    """Postgres nombra `count` a todo COUNT(*) sin alias y `?column?` a las
    expresiones sin nombre: una consulta puede traer varias iguales."""
    description = [("count",), ("count",), ("?column?",), ("?column?",), ("count",)]

    def execute(self, sql, *args):
        pass

    def fetchmany(self, n):
        return [(1, 2, 3, 4, 5)]

    def close(self):
        pass


class ConnColumnasRepetidas:
    def cursor(self):
        return CursorColumnasRepetidas()

    def rollback(self):
        pass


def test_ejecutar_desduplica_columnas_repetidas():
    r = agente._ejecutar(ConnColumnasRepetidas(), "SELECT count(*), count(*), 1, 2, count(*)")
    assert r["ok"] is True, r
    assert r["columnas"] == ["count", "count_2", "?column?", "?column?_2", "count_3"], r["columnas"]
    assert r["filas"] == [{"count": 1, "count_2": 2, "?column?": 3,
                           "?column?_2": 4, "count_3": 5}], r["filas"]
    print("OK: _ejecutar no pierde columnas con nombre repetido.")


def test_validar_no_toma_los_ctes_por_tablas_no_autorizadas():
    # caso g13: los nombres de CTE salen tras FROM/JOIN y se rechazaban como
    # "tablas no autorizadas: imm, pat" aunque el SQL fuera correcto
    sql = ("WITH pat AS (SELECT paciente_id FROM patient), "
           "imm AS (SELECT paciente_id, count(*) AS n FROM immunization GROUP BY 1) "
           "SELECT pat.paciente_id, imm.n FROM pat JOIN imm ON imm.paciente_id = pat.paciente_id")
    assert agente.validar(sql, permitidas=["patient", "immunization"]) is None, \
        agente.validar(sql, ["patient", "immunization"])
    print("OK: validar() no confunde los nombres de CTE con tablas no autorizadas.")


def test_validar_no_deja_que_un_cte_tape_su_propia_tabla():
    """Restar TODOS los nombres de CTE abría el filtro: basta bautizar el CTE
    igual que la tabla que lee para que desaparezca de la lista revisada."""
    casos = [
        "WITH observation AS (SELECT recurso FROM observation) "
        "SELECT o.recurso FROM patient p JOIN observation o ON true",
        # el CTE que tapa se declara DESPUÉS del que lee la tabla real
        "WITH a AS (SELECT recurso FROM observation), observation AS (SELECT 1) "
        "SELECT recurso FROM a",
    ]
    for sql in casos:
        error = agente.validar(sql, permitidas=["patient"])
        assert error and "observation" in error, (sql, error)
    assert agente.validar("WITH x AS (SELECT * FROM pg_shadow) SELECT * FROM x",
                          permitidas=["patient"]), "un CTE no blanquea una tabla prohibida"
    print("OK: un CTE con el nombre de una tabla no la esconde del filtro de acceso.")


def test_tablas_en_sql_ignora_lateral_y_funciones():
    sql = ("SELECT count(*) FROM immunization i CROSS JOIN LATERAL "
           "jsonb_array_elements(i.recurso->'protocolApplied') AS dosis")
    assert agente.tablas_en_sql(sql) == {"immunization"}, agente.tablas_en_sql(sql)
    assert agente.validar(sql, permitidas=["immunization"]) is None, agente.validar(sql, ["immunization"])
    print("OK: tablas_en_sql no confunde LATERAL ni jsonb_array_elements() con tablas.")


def test_riesgo_join_cartesiano_dentro_de_un_cte():
    # el patrón inflado escondido en el CTE: _sin_subconsultas lo borra del
    # nivel superior, así que solo se ve entrando al cuerpo del CTE
    sql = ("WITH mezcla AS (SELECT p.paciente_id, count(*) AS n FROM patient p "
           "JOIN medicationrequest m ON m.paciente_id = p.paciente_id "
           "JOIN immunization i ON i.paciente_id = p.paciente_id GROUP BY 1) "
           "SELECT paciente_id, n FROM mezcla ORDER BY n DESC")
    assert agente._riesgo_join_cartesiano(sql), "debe mirar dentro del cuerpo del CTE"
    print("OK: _riesgo_join_cartesiano detecta el JOIN cartesiano dentro de un CTE.")


def test_riesgo_join_cartesiano_reconoce_cte_con_columnas_y_materialized():
    # `nombre (cols) AS (` y `AS MATERIALIZED (`: si no se reconocen como CTE,
    # sus nombres cuentan como colecciones hijas y se rechaza de más
    sql = ("WITH conteos (paciente_id, n) AS "
           "(SELECT paciente_id, count(*) FROM procedure GROUP BY 1), "
           "vacunas (paciente_id, n) AS MATERIALIZED "
           "(SELECT paciente_id, count(*) FROM immunization GROUP BY 1) "
           "SELECT p.paciente_id, SUM(conteos.n + vacunas.n) AS total FROM patient p "
           "JOIN conteos ON conteos.paciente_id = p.paciente_id "
           "JOIN vacunas ON vacunas.paciente_id = p.paciente_id GROUP BY 1")
    assert agente._nombres_cte(sql) == {"conteos", "vacunas"}, agente._nombres_cte(sql)
    assert not agente._riesgo_join_cartesiano(sql), "los CTEs ya agregan por paciente: no es cartesiano"
    print("OK: _nombres_cte reconoce `nombre (cols) AS (` y `AS MATERIALIZED (`.")


class CursorCatalogo:
    def __init__(self, muestra):
        self.muestra = muestra
        self.filas = []
        self.tablas_leidas = []

    def execute(self, sql, *args):
        if "information_schema" in sql:
            self.filas = [(t,) for t in sorted(self.muestra)]
            return
        tabla = re.search(r'FROM "([^"]+)"', sql).group(1)
        self.tablas_leidas.append(tabla)
        self.filas = [(self.muestra[tabla],)]

    def fetchall(self):
        return self.filas

    def close(self):
        pass


class ConnCatalogo:
    """Tres tablas con una fila de muestra cada una: alcanza para probar el
    filtrado del catálogo sin Postgres."""
    MUESTRA = {"patient": {"gender": "male"},
               "immunization": {"vaccineCode": {"text": "influenza"}},
               "procedure": {"status": "completed"}}

    def __init__(self):
        self.cur = CursorCatalogo(self.MUESTRA)

    def cursor(self):
        return self.cur


def test_catalogo_campos_solo_lista_las_tablas_permitidas():
    agente._catalogo_cache.clear()
    conn = ConnCatalogo()
    texto = agente.catalogo_campos(conn, ["patient", "immunization"])
    assert "patient: recurso->>'gender'" in texto, texto
    assert "procedure" not in texto, "no debe ofrecerle al modelo una tabla no autorizada"
    assert conn.cur.tablas_leidas == ["immunization", "patient"], conn.cur.tablas_leidas
    completo = agente.catalogo_campos(ConnCatalogo())
    assert "procedure: recurso->>'status'" in completo, completo
    agente._catalogo_cache.clear()
    print("OK: catalogo_campos lista solo las tablas permitidas (y no muestrea las demás).")


def test_reintento_escala_al_llm_de_respaldo():
    conn = ConnFalso()
    respuestas = iter([
        '{"sql": "SELECT p.gender FROM patient p", "explicacion": "mal"}',
        '{"sql": "SELECT count(*) AS total FROM patient", "explicacion": "corregido"}',
    ])
    usados = []

    def llm_falso(system, pregunta, temperature=0.1, proveedor=None):
        usados.append(proveedor["modelo"] if proveedor else "principal")
        return next(respuestas)

    with parchar_llm(llm_falso), parchar_respaldo("http://127.0.0.1:8080/v1", "modelo-grande"):
        r = agente.preguntar(conn, "¿cuántos pacientes hay?", tablas_permitidas=["patient"])

    assert usados == ["principal", "modelo-grande"], usados
    assert r["ok"] is True and r["proveedor"] == "respaldo", r
    print("OK: con respaldo configurado, el reintento escala a ese LLM y se reporta en 'proveedor'.")


def test_reintento_sin_respaldo_usa_el_principal():
    conn = ConnFalso()
    respuestas = iter([
        '{"sql": "SELECT p.gender FROM patient p", "explicacion": "mal"}',
        '{"sql": "SELECT count(*) AS total FROM patient", "explicacion": "corregido"}',
    ])
    usados = []

    def llm_falso(system, pregunta, temperature=0.1, proveedor=None):
        usados.append(proveedor["modelo"] if proveedor else "principal")
        return next(respuestas)

    with parchar_llm(llm_falso), parchar_respaldo("", ""):
        r = agente.preguntar(conn, "¿cuántos pacientes hay?", tablas_permitidas=["patient"])

    assert usados == ["principal", "principal"], usados
    assert r["ok"] is True and r["proveedor"] == "local", r
    print("OK: sin respaldo configurado, el reintento sigue con el LLM principal.")


def test_verificar_llm_externo_exige_bandera():
    externa = "https://api.openai.com/v1"
    with parchar_env(LLM_URL=externa, LLM_URL_RESPALDO=None, PERMITIR_LLM_EXTERNO=None):
        try:
            agente.verificar_llm_externo()
            raise AssertionError("debía rechazar un LLM principal fuera de la máquina")
        except RuntimeError as e:
            assert "PERMITIR_LLM_EXTERNO" in str(e), e
    with parchar_env(LLM_URL=externa, LLM_URL_RESPALDO=None, PERMITIR_LLM_EXTERNO="1"):
        agente.verificar_llm_externo()  # con la bandera explícita, pasa
    with parchar_env(LLM_URL="http://localhost:11434/v1",
                     LLM_URL_RESPALDO="https://openrouter.ai/api/v1",
                     PERMITIR_LLM_EXTERNO=None):
        try:
            agente.verificar_llm_externo()
            raise AssertionError("el LLM de respaldo también se revisa")
        except RuntimeError:
            pass
    with parchar_env(LLM_URL="http://[::1]:11434/v1",
                     LLM_URL_RESPALDO="http://host.docker.internal:11434/v1",
                     PERMITIR_LLM_EXTERNO=None):
        agente.verificar_llm_externo()  # los dos locales: ni se entera
    print("OK: verificar_llm_externo bloquea URLs fuera de la máquina sin PERMITIR_LLM_EXTERNO=1.")


if __name__ == "__main__":
    test_preguntar_reintenta_y_corrige()
    test_preguntar_sin_error_no_reintenta()
    test_preguntar_agota_reintento_y_descarta_primer_sql()
    test_riesgo_join_cartesiano_detecta_el_bug_real()
    test_riesgo_join_cartesiano_no_marca_falsos_positivos()
    test_preguntar_rechaza_join_cartesiano_y_reintenta()
    test_arreglar_flechas_encadenadas()
    test_preguntar_arregla_flechas_antes_de_ejecutar()
    test_riesgo_join_cartesiano_no_cuenta_ctes_como_hijas()
    test_preguntar_no_reintenta_dos_veces_si_llm_repite_sql()
    test_validar_rechaza_una_segunda_sentencia()
    test_validar_rechaza_consulta_sin_tabla()
    test_ejecutar_desduplica_columnas_repetidas()
    test_validar_no_toma_los_ctes_por_tablas_no_autorizadas()
    test_validar_no_deja_que_un_cte_tape_su_propia_tabla()
    test_validar_rechaza_funciones_de_sistema_e_into()
    test_validar_ve_las_tablas_tras_comillas_y_comentarios()
    test_preguntar_ejecuta_el_sql_sin_comentarios()
    test_tablas_en_sql_ignora_el_from_interno_de_extract()
    test_validar_ve_las_tablas_unidas_con_coma()
    test_tablas_en_sql_ignora_lateral_y_funciones()
    test_riesgo_join_cartesiano_dentro_de_un_cte()
    test_riesgo_join_cartesiano_reconoce_cte_con_columnas_y_materialized()
    test_catalogo_campos_solo_lista_las_tablas_permitidas()
    test_reintento_escala_al_llm_de_respaldo()
    test_reintento_sin_respaldo_usa_el_principal()
    test_verificar_llm_externo_exige_bandera()
