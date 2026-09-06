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
- **Seguridad**: el agente solo puede generar `SELECT` (validado); solo consulta las
  tablas autorizadas en el sidebar; timeout de 8 s; la transacción siempre se revierte.

## Capa PII (privacidad)

- El recurso `Patient` se **anonimiza al entrar** (la ingesta quita `name`,
  `telecom`, `address`, `identifier`, `contact`, y deja solo el año de nacimiento).
  La tabla `patient` nunca guarda identificadores directos.
- Los demás recursos referencian al paciente por uuid (seudónimo), no por nombre.
- **Producción**: para detectar PII en texto libre (notas clínicas), el siguiente
  paso es integrar [Microsoft Presidio](https://github.com/microsoft/presidio)
  (open source, MIT).

## Cómo correr

```bash
cp .env.example .env   # pon una contraseña de Postgres
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
│   └── migrar_quitar_id.sql          # migración: quita columna id → índice único
├── api/
│   ├── main.py                      # FastAPI (chat, subir, tablas)
│   ├── agente.py                    # text-to-SQL + control de acceso
│   ├── ingesta.py                   # FHIR → tabla por tipo (auto-crea)
│   ├── watcher.py                   # auto-ingesta del buzón
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
