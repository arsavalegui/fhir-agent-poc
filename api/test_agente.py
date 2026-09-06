"""Prueba mínima y autocontenida del flujo de auto-corrección (reintento) de
`preguntar()`: sin base de datos ni LLM reales, con dobles simples. Corre con
`python3 test_agente.py` (sin frameworks)."""
import contextlib

import agente


@contextlib.contextmanager
def parchar_llm(llamar_llm):
    """Reemplaza `agente.system_prompt` (fijo, no relevante para las pruebas)
    y `agente.llamar_llm` por dobles, y restaura los originales al salir
    (incluso si la prueba falla) para que un test no deje estado que
    contamine al siguiente."""
    system_prompt_original = agente.system_prompt
    llamar_llm_original = agente.llamar_llm
    agente.system_prompt = lambda conn, fuente="fhir": "SYSTEM DE PRUEBA"
    agente.llamar_llm = llamar_llm
    try:
        yield
    finally:
        agente.system_prompt = system_prompt_original
        agente.llamar_llm = llamar_llm_original


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
    with parchar_llm(lambda system, pregunta, temperature=0.1: next(respuestas)):
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
    with parchar_llm(lambda system, pregunta, temperature=0.1: '{"sql": "SELECT count(*) AS total FROM patient"}'):
        r = agente.preguntar(conn, "¿cuántos pacientes hay?", tablas_permitidas=["patient"])

    assert r["ok"] is True, r
    assert r["reintento"] is False, r
    print("OK: sin error de Postgres, no hay reintento.")


class CursorSiempreFalla:
    """Ambas ejecuciones (original y la corregida tras el reintento) fallan
    en Postgres: simula un SQL con el mismo tipo de error persistente."""
    def execute(self, sql, *args):
        if sql.startswith("SET"):
            return
        raise Exception('operator does not exist: text ->> unknown')

    def close(self):
        pass


class ConnSiempreFalla:
    def cursor(self):
        return CursorSiempreFalla()

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
    with parchar_llm(lambda system, pregunta, temperature=0.1: next(respuestas)):
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
    with parchar_llm(lambda system, pregunta, temperature=0.1: next(respuestas)):
        r = agente.preguntar(conn, "recetas y vacunas por paciente", tablas_permitidas=[
            "patient", "medicationrequest", "immunization"])

    assert r["ok"] is True, r
    assert r["reintento"] is True, r
    assert "JOIN" not in r["sql"].upper() or r["sql"] == "SELECT count(*) AS total FROM patient", r
    print("OK: preguntar() rechaza el JOIN cartesiano por validar() y se corrige en el reintento.")


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
    with parchar_llm(lambda system, pregunta, temperature=0.1: (
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

    def llm_falso(system, pregunta, temperature=0.1):
        llamadas.append(temperature)
        return mismo_sql

    with parchar_llm(llm_falso):
        r = agente.preguntar(conn, "¿cuántos procedimientos por paciente?")

    assert r["ok"] is False, r
    assert "repitió el mismo SQL" in r["error"], r
    assert llamadas == [0.1, 0.6], "el reintento debe usar más temperatura"
    print("OK: si el reintento repite el SQL, se reporta error claro (con más temperatura) sin re-ejecutar.")


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
