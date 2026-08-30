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
  carpeta entrada/ ──► watcher ──┐          Navegador ─┐
   (dejas un .json)               │                     │ preguntar
                                  ▼                     ▼
                            PostgreSQL  ◄──  Agente text-to-SQL ──► Ollama local
        vista resources_anon (PII) ─┘         (API FastAPI)         (qwen2.5-coder:3b)
              └── tabla resources (jsonb crudo)   └── respuesta + la query SQL
```

- **Postgres 16** con `jsonb` + índice GIN. **Una tabla por tipo de recurso FHIR**
  (`patient`, `encounter`, `condition`, `observation`...), cada una con `id`,
  `recurso` (jsonb crudo), `paciente_id`. Las crea el pipeline solas cuando llega
  un recurso de ese tipo.
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

Requiere **Ollama** corriendo en el host con el modelo descargado:
```bash
ollama serve            # o el servicio systemd --user
ollama pull qwen2.5-coder:3b
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
├── sql/schema.sql                   # documenta el modelo (tablas dinámicas)
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
límites. Es más lento (~10-15 s por pregunta en CPU) pero es 100% tuyo. Para más
velocidad/calidad, apunta `LLM_URL`/`LLM_MODELO` a un modelo más grande o de paga
sin cambiar el resto de la arquitectura.
