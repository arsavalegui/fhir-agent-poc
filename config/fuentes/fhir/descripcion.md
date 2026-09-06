# Descripción de la fuente de datos: FHIR R4

Los datos son registros clínicos en formato **HL7 FHIR R4** (estándar de salud),
generados sintéticamente con Synthea (pacientes ficticios).

## Estructura en la base de datos: UNA TABLA POR TIPO DE RECURSO

Cada tipo de recurso FHIR vive en su propia tabla, nombrada con el tipo en
minúsculas. Todas tienen la misma forma:

- `recurso` (jsonb): el recurso FHIR crudo completo. El identificador único
  del recurso NO es una columna aparte: está dentro, en `recurso->>'id'`.
- `paciente_id` (text): uuid del paciente al que pertenece.
- `cargado_at` (timestamptz).

Las tablas se crean solas cuando llega un archivo con ese tipo. Las más comunes:

- **patient** — pacientes. En `recurso`: `gender`, `birthYear` (ya anonimizado).
  El nombre, domicilio y teléfono NO están (se quitan al cargar, capa PII).
- **encounter** — ingresos/consultas/visitas. `recurso->'class'->>'code'`
  (`EMER`=emergencia, `AMB`=ambulatorio, `IMP`=hospitalizado, `WELLNESS`),
  `recurso->'period'->>'start'` y `...->>'end'`, `recurso->'type'->0->>'text'`.
- **condition** — diagnósticos/enfermedades. `recurso->'code'->>'text'` (nombre),
  `recurso->>'onsetDateTime'`.
- **observation** — mediciones (signos vitales, laboratorio).
  `recurso->'code'->>'text'`, `recurso->'valueQuantity'->>'value'`,
  `recurso->'valueQuantity'->>'unit'`.
- **procedure** — procedimientos. `recurso->'code'->>'text'`.
- **immunization** — vacunas. `recurso->'vaccineCode'->>'text'`,
  `recurso->>'occurrenceDateTime'`.
- **medicationrequest** — recetas. `recurso->'medicationCodeableConcept'->>'text'`.
- **allergyintolerance** — alergias. `recurso->'code'->>'text'`.

Puede haber más tablas (careplan, careteam, goal, organization, practitioner,
claim, diagnosticreport, ...). Si dudas qué tablas existen, están todas listadas
en el mensaje de acceso a datos.

## Notas importantes

- TODAS las tablas (incluida `patient`) tienen la columna `paciente_id` con el
  mismo uuid sin prefijo. Para unir tablas usa SIEMPRE:
  `JOIN patient p ON p.paciente_id = otra.paciente_id` (y entre dos recursos:
  `a.paciente_id = b.paciente_id`). NUNCA unas por `recurso->>'id'` (es el id
  interno de cada recurso, no el del paciente, y no hay columna `id`).
- Los campos clínicos viven dentro del jsonb `recurso`: escribe
  `p.recurso->>'gender'`, nunca `p.gender`.
- Los datos son **históricos** (fechas ~1950-2019). Preguntas con "hoy" o
  "últimas 72h" probablemente no devuelvan filas.
- Para "cuántos pacientes" usa `patient`; para "ingresos" usa `encounter`.
- **Los valores de texto de los datos están en INGLÉS**, aunque la pregunta
  venga en español: descripciones de condiciones, medicamentos y vacunas,
  `gender` (`male`/`female`, nunca `M`/`F`) y clases de encuentro (`EMER`,
  `AMB`). Al filtrar, **traduce el literal al inglés**:
  - "obesidad" → `ILIKE '%obesity%'`
  - "mujeres" → `= 'female'`
  - "bronquitis" → `ILIKE '%bronchitis%'`
  - "emergencia" → `= 'EMER'`
