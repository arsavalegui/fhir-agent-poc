#!/usr/bin/env python3
"""Evaluador del agente text-to-SQL contra el golden set de `evals/golden.json`.

Para cada caso: le hace la pregunta al agente, ejecuta la `sql_verdad` escrita
a mano y compara los DOS RESULTADOS (no los dos SQL). Dos consultas distintas
que devuelven lo mismo cuentan como correctas; es lo único que importa.

    docker compose exec api python evaluar.py
    docker compose exec api python evaluar.py --solo g13_tres_colecciones
    docker compose exec api python evaluar.py --modelo qwen2.5-coder:7b --json /app/r.json

`--json` escribe DENTRO del contenedor: sácalo con
`docker cp fhir-agent-poc-api-1:/app/r.json .`
"""
import argparse
import json
import os
import time
from collections import Counter
from decimal import Decimal

import psycopg2

import agente

GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evals", "golden.json")
MAX_FILAS = 200  # mismo tope que agente._ejecutar (fetchmany(200))


def normalizar(v):
    """Valor comparable entre la consulta del LLM y la de verdad.

    Números a float redondeado (un `->>'birthYear'` sin castear devuelve el
    texto '2001' y con `::int` devuelve 2001: misma respuesta, y el modelo
    puede elegir cualquiera de las dos). None se queda como None. El resto,
    texto sin espacios de más.
    """
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float, Decimal)):
        return round(float(v), 6)
    texto = " ".join(str(v).split())
    try:
        return round(float(texto), 6)
    except ValueError:
        return texto


def multiconjunto(filas):
    """Multiconjunto de tuplas normalizadas: el orden de las filas no importa,
    pero sí cuántas veces aparece cada una."""
    return Counter(tuple(normalizar(v) for v in fila) for fila in filas)


def ejecutar_verdad(conn, sql):
    cur = conn.cursor()
    try:
        cur.execute("SET statement_timeout = 8000")
        cur.execute(sql)
        filas = cur.fetchmany(MAX_FILAS)
        return [list(f) for f in filas]
    finally:
        cur.close()
        conn.rollback()  # nunca dejar una transacción abierta o abortada


def motivo_invalido(filas, caso):
    """Una `sql_verdad` que no devuelve filas hace que el caso pase sin probar
    nada: dos resultados vacíos comparan iguales. Casi siempre es un golden mal
    escrito, así que el caso se marca INVÁLIDO y sale del pass rate. Si el
    vacío ES la respuesta correcta, ponle `"permite_vacio": true` al caso."""
    if not filas and not caso.get("permite_vacio"):
        return "la verdad no devuelve filas"
    return ""


def comparar(esperado, obtenido):
    """(ok, motivo). El motivo distingue el fallo real (valores mal) del casi
    acierto (mismas columnas en otro orden), que es lo que hay que leer para
    saber si vale la pena reformular la pregunta o arreglar el prompt."""
    if esperado == obtenido:
        return True, ""
    n_esp, n_obt = sum(esperado.values()), sum(obtenido.values())
    if n_esp != n_obt:
        return False, f"{n_obt} filas, se esperaban {n_esp}"
    cols_esp = {len(t) for t in esperado}
    cols_obt = {len(t) for t in obtenido}
    if cols_esp != cols_obt:
        return False, f"columnas {sorted(cols_obt)}, se esperaban {sorted(cols_esp)}"
    ordenar = lambda t: tuple(sorted(t, key=repr))
    if Counter(map(ordenar, esperado)) == Counter(map(ordenar, obtenido)):
        return False, "mismos valores, columnas en otro orden"
    return False, "valores distintos"


def correr(casos, permitidas, dsn):
    conn_agente = psycopg2.connect(dsn)
    conn_verdad = psycopg2.connect(dsn)
    resultados = []
    try:
        for caso in casos:
            base = {"id": caso["id"], "dificultad": caso["dificultad"],
                    "sql_verdad": caso["sql_verdad"], "invalido": False,
                    "ok": False, "reintento": False, "latencia_s": 0.0,
                    "sql_generada": None, "prompt_hash": None,
                    "filas_esperadas": None, "filas_obtenidas": None}

            # La verdad va PRIMERO: si el golden está roto no tiene caso gastar
            # 15 s de LLM, y así el caso se marca inválido aunque el agente falle.
            try:
                filas_verdad = ejecutar_verdad(conn_verdad, caso["sql_verdad"])
                motivo = motivo_invalido(filas_verdad, caso)
            except Exception as e:  # noqa: BLE001  un golden con typo no tumba la corrida
                motivo = f"sql_verdad falló: {e}"[:70]
            if motivo:
                resultados.append({**base, "invalido": True, "motivo": motivo})
                imprimir_fila(resultados[-1])
                continue

            t0 = time.monotonic()
            try:
                r = agente.preguntar(conn_agente, caso["pregunta"], "fhir", permitidas)
            except Exception as e:  # noqa: BLE001  el LLM caído no debe tumbar la corrida
                r = {"ok": False, "error": f"excepción: {e}", "reintento": False, "sql": None}
            latencia = time.monotonic() - t0

            if r["ok"]:
                esperado = multiconjunto(filas_verdad)
                obtenido = multiconjunto(fila.values() for fila in r["filas"])
                ok, motivo = comparar(esperado, obtenido)
                n_esp, n_obt = sum(esperado.values()), sum(obtenido.values())
            else:
                ok, motivo = False, str(r.get("error", ""))[:70]
                n_esp, n_obt = len(filas_verdad), None

            resultados.append({
                **base, "ok": ok, "reintento": bool(r.get("reintento")),
                "latencia_s": round(latencia, 1), "motivo": motivo,
                "sql_generada": r.get("sql"), "prompt_hash": r.get("prompt_hash"),
                "filas_esperadas": n_esp, "filas_obtenidas": n_obt,
            })
            imprimir_fila(resultados[-1])
    finally:
        conn_agente.close()
        conn_verdad.close()
    return resultados


FORMATO = "{id:<26} {dif:<8} {estado:<8} {rei:<4} {lat:>7}  {motivo}"


def imprimir_fila(r):
    print(FORMATO.format(id=r["id"], dif=r["dificultad"],
                         estado="INVALIDO" if r["invalido"] else ("OK" if r["ok"] else "FALLA"),
                         rei="sí" if r["reintento"] else "no",
                         lat=f'{r["latencia_s"]}s', motivo=r["motivo"]), flush=True)


def autocheck():
    """Chequeo del comparador, sin base de datos ni LLM: `python evaluar.py --autocheck`."""
    assert normalizar("2001") == normalizar(2001) == 2001.0, "texto jsonb vs int casteado"
    assert normalizar("  Viral  sinusitis ") == "Viral sinusitis", "espacios de más"
    assert normalizar(Decimal("101.13141411327")) == 101.131414, "numeric de Postgres"
    assert normalizar(None) is None
    a = multiconjunto([["male", 6], ["female", 2]])
    assert comparar(a, multiconjunto([["female", "2"], ["male", "6"]])) == (True, ""), "orden de filas no importa"
    assert comparar(a, multiconjunto([[6, "male"], [2, "female"]]))[1] == "mismos valores, columnas en otro orden"
    assert comparar(a, multiconjunto([["male", 6]]))[1] == "1 filas, se esperaban 2"
    assert comparar(a, multiconjunto([["male", 7], ["female", 2]]))[1] == "valores distintos"
    assert comparar(multiconjunto([[1], [1]]), multiconjunto([[1], [2]]))[1] == "valores distintos", "multiconjunto, no conjunto"
    assert comparar(Counter(), Counter()) == (True, ""), "dos vacíos comparan iguales: por eso hace falta motivo_invalido()"
    assert motivo_invalido([], {}) == "la verdad no devuelve filas"
    assert motivo_invalido([], {"permite_vacio": True}) == ""
    assert motivo_invalido([[1]], {}) == ""
    print("OK: normalizar(), comparar() y motivo_invalido() se portan bien.")


def main():
    p = argparse.ArgumentParser(description="Evalúa el agente contra el golden set.")
    p.add_argument("--modelo", help="sobrescribe agente.MODELO para esta corrida")
    p.add_argument("--solo", help="ids separados por coma, p. ej. g01_total_pacientes,g13_tres_colecciones")
    p.add_argument("--json", dest="ruta_json", help="guarda los resultados en esta ruta (dentro del contenedor)")
    p.add_argument("--autocheck", action="store_true", help="prueba el comparador y sale (sin DB ni LLM)")
    args = p.parse_args()

    if args.autocheck:
        return autocheck()

    if args.modelo:
        agente.MODELO = args.modelo  # llamar_llm lee el global en cada llamada

    golden = json.load(open(GOLDEN, encoding="utf-8"))
    casos = golden["casos"]
    if args.solo:
        pedidos = [s.strip() for s in args.solo.split(",") if s.strip()]
        casos = [c for c in casos if c["id"] in pedidos]
        faltan = set(pedidos) - {c["id"] for c in casos}
        if faltan:
            p.error(f"ids que no están en el golden set: {', '.join(sorted(faltan))}")
    if not casos:
        p.error("no hay casos que correr")

    permitidas = golden["tablas_permitidas"]
    print(f"modelo: {agente.MODELO}   casos: {len(casos)}   "
          f"tablas permitidas: {len(permitidas)}\n")
    print(FORMATO.format(id="id", dif="dific.", estado="estado", rei="reint",
                         lat="lat", motivo="motivo"))
    print("-" * 100)

    resultados = correr(casos, permitidas, os.environ["DATABASE_URL"])

    # Los inválidos son golden roto, no fallo del modelo: fuera del pass rate.
    validos = [r for r in resultados if not r["invalido"]]
    invalidos = [r for r in resultados if r["invalido"]]
    aciertos, total = sum(1 for r in validos if r["ok"]), len(validos)
    print("-" * 100)
    print(f"pass rate: {aciertos}/{total} = {100 * aciertos / total:.0f}%" if total
          else "pass rate: sin casos válidos")
    if invalidos:
        print(f"INVÁLIDOS (fuera del pass rate): {', '.join(r['id'] for r in invalidos)}")
    for dif in ["facil", "media", "dificil"]:
        grupo = [r for r in validos if r["dificultad"] == dif]
        if grupo:
            print(f"  {dif:<8} {sum(1 for r in grupo if r['ok'])}/{len(grupo)}")
    print(f"latencia total: {sum(r['latencia_s'] for r in resultados):.0f}s"
          f"   reintentos: {sum(1 for r in resultados if r['reintento'])}")
    # El system prompt debe ser BYTE-IDÉNTICO en todos los casos (misma lista de
    # tablas permitidas) para que Ollama reuse su cache de prompt. Si hay más de
    # un hash, algo lo está cambiando entre preguntas y la latencia lo paga.
    hashes = {r["prompt_hash"] for r in resultados if r["prompt_hash"]}
    if len(hashes) > 1:
        print(f"AVISO: el system prompt cambió entre casos ({len(hashes)} hashes distintos)")

    if args.ruta_json:
        with open(args.ruta_json, "w", encoding="utf-8") as f:
            json.dump({"modelo": agente.MODELO, "aciertos": aciertos, "total": total,
                       "resultados": resultados}, f, ensure_ascii=False, indent=2)
        print(f"resultados en {args.ruta_json} (dentro del contenedor; "
              f"sácalo con docker cp)")


if __name__ == "__main__":
    main()
