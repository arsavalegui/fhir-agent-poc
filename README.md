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
- **El agente devuelve la query**: no es una caja negra. Cada respuesta viene con
  el SQL exacto que la produjo, para que lo corras en Postgres y compruebes.
- **Estandarización**: la config se parte en tres, igual que en lemut:
  1. Comportamiento general del agente (aplica a cualquier fuente).
  2a. Descripción de la fuente de datos (qué es, su estructura).
  2b. Reglas del agente para esa fuente.
  Para un cliente de finanzas en vez de salud, solo cambias 2a y 2b.

## Arquitectura

```
Navegador ─┐
           │  subir JSON / preguntar
           ▼
      API (FastAPI)  ──►  Agente text-to-SQL  ──►  OmniRoute (LLM gratis, host)
           │                                            (genera el SELECT)
           ▼
      PostgreSQL  ◄── vista resources_anon (capa PII) ── tabla resources (jsonb crudo)
```

- **Postgres 16** con `jsonb` + índice GIN. Tabla `resources` (crudo) y vista
  `resources_anon` que enmascara identificadores del recurso `Patient`.
- **API FastAPI** con UI: preguntar, subir JSON, y ver la configuración.
- **LLM vía OmniRoute** (gateway de modelos gratis en el host, puerto 20128).
  Solo genera la query; nunca toca los datos directamente.
- **Seguridad**: el agente solo puede generar `SELECT` (validado); timeout de 8 s;
  la transacción siempre se revierte.

## Capa PII (privacidad)

- La vista `resources_anon` borra `name`, `telecom`, `address`, `identifier`,
  `contact`, etc. del recurso `Patient`, y reduce la fecha de nacimiento a solo el
  año. El agente consulta SIEMPRE esta vista.
- Los demás recursos referencian al paciente por uuid (seudónimo), no por nombre.
- **Producción**: para detectar PII en texto libre (notas clínicas), el siguiente
  paso es integrar [Microsoft Presidio](https://github.com/microsoft/presidio)
  (open source, MIT).

## Cómo correr

```bash
cp .env.example .env   # pon una contraseña de Postgres
docker compose up -d --build
# cargar los bundles FHIR de ejemplo:
docker exec fhir-agent-poc-api-1 python ingesta.py
# abrir http://localhost:8010
```

Requiere que **OmniRoute** esté corriendo en el host (`systemctl --user status
omniroute`) para el paso del LLM.

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
├── sql/schema.sql                   # tabla jsonb + vista PII
├── api/
│   ├── main.py                      # FastAPI
│   ├── agente.py                    # text-to-SQL
│   ├── ingesta.py                   # bundle FHIR → filas
│   └── static/index.html            # UI
└── datos_fhir/bundles/              # 8 pacientes Synthea
```

## Nota sobre el LLM

El paso text-to-SQL usa el pool de modelos gratis de OmniRoute, que puede
devolver `429` si está saturado (el agente reintenta con backoff y modelos de
respaldo). Para uso intensivo o producción, conviene un modelo de paga barato
(ej. Gemini Flash-Lite) — la arquitectura no cambia, solo la variable
`OMNIROUTE_MODELO` / el endpoint.
