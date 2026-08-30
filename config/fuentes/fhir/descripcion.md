# Descripción de la fuente de datos: FHIR R4

Los datos son registros clínicos en formato **HL7 FHIR R4** (estándar de salud),
generados sintéticamente con Synthea (pacientes ficticios).

## Estructura en la base de datos: UNA TABLA POR TIPO DE RECURSO

Cada tipo de recurso FHIR vive en su propia tabla, nombrada con el tipo en
minúsculas. Todas tienen la misma forma:

- `id` (text): identificador único del recurso.
- `recurso` (jsonb): el recurso FHIR crudo completo.
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

- Un recurso se relaciona con su paciente por `paciente_id` (el mismo uuid está en
  la tabla `patient.id` sin el prefijo, y en las demás en `paciente_id`). Para
  unir: `JOIN patient p ON p.id = otra.paciente_id` — ojo: `patient.id` viene como
  `Patient/<uuid>`, y `paciente_id` como `<uuid>`; compara con
  `split_part(p.id,'/',2) = otra.paciente_id` si necesitas unir.
- Los datos son **históricos** (fechas ~1950-2019). Preguntas con "hoy" o
  "últimas 72h" probablemente no devuelvan filas.
- Para "cuántos pacientes" usa `patient`; para "ingresos" usa `encounter`.
