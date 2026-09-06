# Agente de datos FHIR — POC

Prueba de concepto: subes JSON crudo (FHIR de salud, o cualquier otro), se guarda
como **dato dinámico** en PostgreSQL (`jsonb`, el equivalente open-source al tipo
`dynamic` de KQL — **sin aplanar nada**), y un **agente en lenguaje natural**
responde preguntas devolviéndote además la **consulta SQL** para verificar.

Es el mismo patrón de [lemut_n8n](https://github.com/arsavalegui/lemut_n8n) (bot
RAG) pero generalizado a datos estructurados: **la arquitectura es fija, por
cliente solo cambia la configuración del agente**.

## Idea clave

- **Sin trabajo humano de aplanar**: el problema de siempre con archivos complejos
  (FHIR, y su anidamiento infinito) es tener que aplanarlos antes de analizar.
  Aquí el JSON entra crudo a `jsonb` y se consulta con `->`, `->>`, `@>`, jsonpath.
- **No hay columnas de negocio**: cada tabla es una colección de documentos JSON
  de un tipo; la única columna con datos es `recurso` (jsonb). El agente sabe
  que TODO campo se lee de ahí (`recurso->>'campo'`), nunca como columna suelta.
- **Catálogo de campos generado desde los datos**: en vez de mantener a mano la
  lista de campos por tabla, `api/agente.py` la infiere muestreando `recurso`
  (`jsonb_object_keys`) y la inyecta en el prompt. Se cachea en memoria y solo
  se regenera si cambia el conjunto de tablas, para que el prompt sea
  **byte-idéntico** entre preguntas (Ollama reaprovecha su cache de prompt).
- **Auto-corrección de un intento**: si Postgres rechaza el SQL generado, el
  agente le manda al LLM el SQL que falló + el error exacto y le pide
  corregirlo una sola vez antes de rendirse.
- **El agente devuelve la query**: no es una caja negra. Cada respuesta viene con
  el SQL exacto que la produjo, para que lo corras en Postgres y compruebes.
- **Estandarización**: la config se parte en tres, igual que en lemut:
  1. Comportamiento general del agente (aplica a cualquier fuente).
  2a. Descripción de la fuente de datos (qué es, su estructura).
  2b. Reglas del agente para esa fuente.
  Para un cliente de finanzas en vez de salud, solo cambias 2a y 2b.

## Arquitectura

```
  carpeta entrada/ ──► watcher ──┐          Navegador ─┐
   (dejas un .json)               │                     │ preguntar
                                  ▼                     ▼
                            PostgreSQL  ◄──  Agente text-to-SQL ──► Ollama local
   1 tabla por tipo de recurso ─┘         (API FastAPI)      (qwen2.5-coder-fhir:3b)
        (recurso jsonb crudo)                          └── respuesta + la query SQL
```

- **Postgres 16** con `jsonb` + índice GIN. **Una tabla por tipo de recurso FHIR**
  (`patient`, `encounter`, `condition`, `observation`...), cada una con
  `recurso` (jsonb crudo), `paciente_id`, `cargado_at`. Las crea el pipeline
  solas cuando llega un recurso de ese tipo. No hay columna `id`: el
  identificador del recurso vive en `recurso->>'id'`, con un índice único
  sobre esa expresión (hace de PK para el `ON CONFLICT` de la ingesta).
- **Buzón de auto-ingesta** (patrón landing folder): dejas un `.json` en
  `datos_fhir/entrada/` y el servicio `watcher` lo carga solo y lo mueve a
  `procesados/`. Detecta el tipo por el campo `resourceType` DENTRO del JSON (no
  por el nombre del archivo), **crea la tabla si no existe** y hace append si ya
  existe. Maneja Bundles (muchos recursos) y recursos sueltos.
- **API FastAPI** con UI de chat: sidebar con la vista de la base y **selector de
  acceso** a tablas (por ahora todas activas por defecto; las tablas nuevas se
  auto-activan), instrucciones del agente, y un solo chat sin historial que se
  borra al cerrar.
- **LLM 100% local** con [Ollama](https://ollama.com) + modelo `qwen2.5-coder:3b`
  (corre en tu CPU, gratis, sin registro, sin límites). Solo genera la query;
  nunca toca los datos. Se puede apuntar a otro backend con `LLM_URL`/`LLM_MODELO`.
- **Seguridad**: el agente solo puede generar `SELECT` (validado, ver sección abajo);
  solo consulta las tablas autorizadas en el sidebar; timeout de 8 s; la transacción
  siempre se revierte.
- **Rol de Postgres de solo lectura**: `/api/preguntar` y los GET (`/api/estado`,
  `/api/tablas`) usan el rol `fhir_lector` (`sql/roles.sql`), que solo tiene
  `GRANT SELECT`; la ingesta y el watcher siguen con el rol `fhir` normal, que sí
  escribe. La barrera real son los GRANTs (probado: el rol no puede
  INSERT/CREATE/DROP ni con `default_transaction_read_only` apagado); ese flag es
  un cinturón extra, no la protección principal. Como las tablas nacen dinámicas en
  la ingesta, `sql/roles.sql` usa `ALTER DEFAULT PRIVILEGES` para que las tablas
  nuevas ya salgan con `SELECT` otorgado a `fhir_lector` sin correr nada a mano.
  `POSTGRES_PASSWORD_LECTURA` es obligatoria en `.env`: `docker-compose.yml` falla
  rápido al arrancar si falta. `sql/init_roles.sh` crea el rol solo, pero nada más
  en un volumen de Postgres **nuevo** (corre como script de
  `/docker-entrypoint-initdb.d`); sobre un volumen que ya existía hay que correr
  `sql/roles.sql` a mano una vez: `psql -U fhir -d fhir_db -v pw='la_contraseña' -f sql/roles.sql`.

## Capa PII (privacidad)

- El recurso `Patient` se **anonimiza al entrar** (la ingesta quita `name`,
  `telecom`, `address`, `identifier`, `contact`, y deja solo el año de nacimiento).
  La tabla `patient` nunca guarda identificadores directos.
- Los recursos clínicos (encuentros, diagnósticos, observaciones...) referencian
  al paciente por uuid (seudónimo), no por nombre. **Ojo**: esto no cubre a todo
  el bundle — ver el pendiente de `practitioner`/`organization` más abajo.
- **Producción**: para detectar PII en texto libre (notas clínicas), el siguiente
  paso es integrar [Microsoft Presidio](https://github.com/microsoft/presidio)
  (open source, MIT).

## Cómo correr

```bash
cp .env.example .env   # pon POSTGRES_PASSWORD y POSTGRES_PASSWORD_LECTURA (las dos son obligatorias)
docker compose up -d --build
# cargar los bundles FHIR de ejemplo (o deja archivos en datos_fhir/entrada/):
docker exec fhir-agent-poc-api-1 python ingesta.py
# abrir http://localhost:8010
```

**Subir más datos** (dos formas): dejar un `.json` FHIR en `datos_fhir/entrada/`
y el watcher lo ingiere solo; o correr `python ingesta.py` para recargar toda la
carpeta `bundles/`.

Requiere **Ollama** corriendo en el host con el modelo descargado, y una
variante propia con más contexto (el prompt real mide ~5.1k tokens — catálogo
de campos + reglas — y el `qwen2.5-coder:3b` base carga con `num_ctx` 4096; el
endpoint OpenAI-compatible de Ollama ignora el `num_ctx` que manda
`api/agente.py` en cada llamada, así que hay que grabarlo en el Modelfile):
```bash
ollama serve            # o el servicio systemd --user
ollama pull qwen2.5-coder:3b
ollama create qwen2.5-coder-fhir:3b -f Modelfile   # num_ctx 8192, una sola vez
```

## Datos de prueba

8 pacientes sintéticos de [Synthea](https://github.com/synthetichealth/synthea)
(FHIR R4, datos ficticios) en `datos_fhir/bundles/`. 2614 recursos: pacientes,
ingresos, diagnósticos, observaciones, procedimientos, vacunas, recetas.

## Preguntas de ejemplo

- ¿Cuántos pacientes hay?
- ¿Cuáles son los diagnósticos más comunes?
- ¿Cuántos ingresos de emergencia hubo?
- ¿Cuántos pacientes hay por género?
- ¿Cuáles son las vacunas más aplicadas?

## Evaluación (golden set)

`api/evals/golden.json` tiene 15 preguntas con su **SQL de verdad escrita a
mano y verificada contra la base**. `api/evaluar.py` le hace cada pregunta al
agente y compara **los resultados**, no los SQL: dos consultas distintas que
devuelven lo mismo cuentan como correctas.

```bash
docker compose exec api python evaluar.py                      # las 15
docker compose exec api python evaluar.py --solo g13_tres_colecciones
docker compose exec api python evaluar.py --modelo qwen2.5-coder:7b
docker compose exec api python evaluar.py --json /app/r.json   # sacar con docker cp
docker compose exec api python evaluar.py --autocheck          # prueba el comparador, sin LLM
```

Comparación: multiconjunto de tuplas normalizadas (números como float, texto
sin espacios de más), así que el **orden de las filas no importa** y `'2001'`
de un `->>` cuenta igual que `2001` casteado a `int`. Sí importan cuántas
columnas devuelves y en qué orden, por eso las preguntas del golden set piden
explícitamente qué columnas quieren.

Resultado con `qwen2.5-coder-fhir:3b` local (CPU): **13/15 (87%)**, latencia
total de la corrida 228 s, 2 reintentos de auto-corrección. Por dificultad:
fácil 5/5, media 6/7, difícil 2/3.

Los 2 fallos restantes son estables y del modelo, no del validador ni de la
config del agente:

- `max()` sobre un valor de texto sin castear a `::numeric` (compara texto en
  vez de número).
- Contar filas de `condition` en vez de pacientes distintos, en una pregunta
  que pide cuántos pacientes tienen cierto diagnóstico.

Qué sigue si hace falta subir el pass rate: un few-shot puntual en
`reglas.md` para cada patrón, o pasar a un modelo más grande (ver "Nota sobre
el LLM").

## Seguridad de las consultas (`validar()`)

Antes de ejecutar cualquier SQL que arma el LLM, `api/agente.py` lo pasa por
`validar()`. Rechaza:

- Más de una sentencia (cualquier `;` que no sea el final cosmético).
- Cualquier cosa que no sea `SELECT` o `WITH ... SELECT`.
- Palabras clave de escritura o de sistema: `INSERT/UPDATE/DELETE/DROP/ALTER/
  CREATE/TRUNCATE/GRANT/REVOKE/COPY/INTO`, `current_setting`, `pg_read_file`,
  `pg_sleep`, `dblink`, etc.
- Tablas no autorizadas, calculado por alcance: cada cuerpo de CTE se valida
  restando solo los CTE declarados antes de él (un CTE con nombre de tabla
  real ya no la esconde, ni al revés).
- Coma-join contado como tabla en cada nivel de paréntesis, y JOIN cartesiano
  (2+ colecciones distintas de `patient` unidas por JOIN/FROM directo junto
  con un agregado), tanto en el nivel superior como dentro de cualquier CTE.

Acepta sin falsos positivos: `LATERAL jsonb_array_elements`, el `FROM`
interno de `EXTRACT`/`SUBSTRING`/`TRIM`, y CTEs con lista de columnas o
`MATERIALIZED`.

Corte conocido: los comentarios (`--` y `/* */`) se quitan antes de validar,
y se ejecuta ese mismo texto sin comentarios. Un `--` dentro de un literal de
cadena también se borra, así que no solo falla la validación: cambia en
silencio qué SQL se ejecuta. Haría falta un parser SQL completo para
diferenciar un `--` real de uno dentro de una cadena.

## Estructura del repo

```
fhir-agent-poc/
├── docker-compose.yml
├── config/
│   ├── agente_general.md            # 1 · comportamiento general
│   └── fuentes/fhir/
│       ├── descripcion.md           # 2a · descripción de la fuente
│       └── reglas.md                # 2b · reglas del agente
├── sql/
│   ├── schema.sql                    # solo referencia: las tablas reales las crea api/ingesta.py, una por resourceType
│   ├── migrar_quitar_id.sql          # migración: quita columna id → índice único
│   ├── roles.sql                     # rol fhir_lector de solo lectura (GRANT SELECT + default privileges)
│   └── init_roles.sh                 # corre roles.sql en un volumen de Postgres nuevo
├── api/
│   ├── main.py                      # FastAPI (chat, subir, tablas)
│   ├── agente.py                    # text-to-SQL + control de acceso
│   ├── ingesta.py                   # FHIR → tabla por tipo (auto-crea)
│   ├── watcher.py                   # auto-ingesta del buzón
│   ├── evaluar.py                   # evalúa el agente contra el golden set
│   ├── evals/golden.json            # 15 preguntas + SQL de verdad verificada
│   ├── test_agente.py               # suite con dobles del LLM y de la conexión
│   └── static/index.html            # UI de chat + sidebar
└── datos_fhir/
    ├── bundles/                     # 8 pacientes Synthea
    ├── entrada/                     # buzón: deja aquí .json nuevos
    ├── procesados/                  # ya ingeridos
    └── errores/                     # los que fallaron
```

## Nota sobre el LLM

El paso text-to-SQL corre **local** con Ollama (`qwen2.5-coder:3b`), gratis y sin
límites. Es más lento (~10-15 s por pregunta en CPU, más si la máquina tiene
otra carga) pero es 100% tuyo. Para más velocidad/calidad, apunta
`LLM_URL`/`LLM_MODELO` a un modelo más grande o de paga sin cambiar el resto
de la arquitectura.

Un modelo de 3B **imita el patrón de los few-shots de `reglas.md`, no razona
las reglas en prosa**: preguntas que combinan tablas en un patrón ya cubierto
por un ejemplo (p. ej. "al menos una alergia" + varios conteos) salen
correctas de forma consistente; combinaciones nuevas de tablas 1-a-muchos
pueden todavía encadenar JOINs e inflar conteos aunque la regla esté escrita.
`api/agente.py` cubre lo mecánico (JOIN cartesiano rechazado, `->>`
encadenado corregido solo, reintento con más temperatura), pero preguntas que
combinan **3 o más colecciones** siguen fallando seguido con SQL válido mas
resultados vacíos o incorrectos (alias repetido entre tablas, valores mal
escritos como `'F'` en vez de `'female'`) — un modelo de 3B no siempre tiene
para sostener esa composición. Si eso importa para producción, las opciones
son: un modelo local más grande (7B+, con más RAM/CPU) o un modelo de paga vía
API, apuntando `LLM_URL`/`LLM_MODELO` sin cambiar el resto de la arquitectura.
No escala agregar un few-shot por cada combinación posible de tablas.

**Por qué no un 7B local en esta máquina**: sin GPU, un modelo de 7B corriendo
en CPU con un prompt de ~5k tokens (catálogo de campos + reglas) sube la
latencia a un punto donde deja de ser práctico para un chat. En este hardware
el 3B es el techo razonable; para más calidad sin comprar máquina, la opción
es un respaldo externo (ver abajo), no un modelo local más grande.

**Escalada al respaldo en el reintento**: si el primer intento falla,
`llamar_llm` reintenta con más temperatura; si además `LLM_URL_RESPALDO` y
`LLM_MODELO_RESPALDO` están puestos en `.env` (y `LLM_API_KEY_RESPALDO` si el
proveedor la pide), ese reintento se manda al respaldo en vez de repetirle la
pregunta al modelo local, que ya demostró que no le salió. Ejemplo con Groq:
```
LLM_URL_RESPALDO=https://api.groq.com/openai/v1
LLM_MODELO_RESPALDO=llama-3.3-70b-versatile
LLM_API_KEY_RESPALDO=<tu key>
```

**Qué sale de la máquina** si usas un respaldo externo: la pregunta del
usuario, el catálogo de campos (nombres de campo, no valores) y, en el
reintento, el SQL que falló más el mensaje de error de Postgres. Nunca salen
filas de datos: el LLM solo genera SQL, nunca ve resultados de la base.
`verificar_llm_externo()` bloquea el arranque si `LLM_URL` o
`LLM_URL_RESPALDO` apuntan fuera de esta máquina, salvo que pongas
`PERMITIR_LLM_EXTERNO=1` en `.env` a propósito.

## Pendientes

- **La capa PII no cubre todo el bundle**: solo se anonimiza `Patient` al
  ingerir. Verificado contra los bundles de `datos_fhir/bundles/`:
  `practitioner` y `organization` sí traen `name`, `telecom` y `address` sin
  enmascarar. (`explanationofbenefit` se revisó también y, en los datos
  actuales, no trae esos campos directos — solo referencias por uuid —, así
  que no aplica el mismo problema.) Antes de cargar datos reales habría que
  extender `anonimizar()` en `api/ingesta.py` a `practitioner` y
  `organization`.
- **Recursos sin `id` se duplican al recargar**: el `ON CONFLICT` de la
  ingesta depende de `recurso->>'id'`; un recurso sin ese campo no choca con
  nada y cada recarga de la misma carpeta lo vuelve a insertar.
- **`validar()` rechaza `ILIKE '%into%'` dentro de un literal**: la palabra
  `into` está prohibida sin distinguir si aparece dentro de una cadena de
  texto o como palabra clave SQL real, así que una pregunta que necesite
  filtrar por un valor que contenga "into" se rechazaría de forma incorrecta.
