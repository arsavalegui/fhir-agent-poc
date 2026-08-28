# Descripción de la fuente de datos: FHIR R4

Los datos son registros clínicos en formato **HL7 FHIR R4** (estándar de salud
de EE.UU.), generados sintéticamente con Synthea (pacientes ficticios).

## Estructura en la base de datos

Todo vive en la vista `resources_anon`, una fila por recurso FHIR:

- `id` (text): identificador único del recurso.
- `tipo` (text): tipo de recurso FHIR (`resourceType`).
- `paciente_id` (text): uuid del paciente al que pertenece el recurso.
- `recurso` (jsonb): el recurso FHIR crudo completo.

## Tipos de recurso disponibles y sus campos clave (dentro de `recurso`)

- **Patient** — un paciente. Campos: `gender`, `birthYear` (año de nacimiento,
  ya anonimizado). El nombre y domicilio están protegidos (no disponibles).
- **Encounter** — un ingreso / consulta / visita. Campos:
  `class.code` (tipo: `EMER`=emergencia, `AMB`=ambulatorio, `IMP`=hospitalizado,
  `WELLNESS`), `period.start` y `period.end` (timestamptz), `type[0].text`
  (motivo de la visita), `subject.reference` (paciente).
- **Condition** — un diagnóstico / enfermedad. Campos: `code.text` (nombre de la
  enfermedad), `clinicalStatus`, `onsetDateTime`, `subject.reference`.
- **Observation** — una medición (signos vitales, laboratorio). Campos:
  `code.text`, `valueQuantity.value`, `valueQuantity.unit`, `effectiveDateTime`.
- **Procedure** — un procedimiento realizado. Campo: `code.text`.
- **Immunization** — una vacuna aplicada. Campos: `vaccineCode.text`,
  `occurrenceDateTime`.
- **MedicationRequest** — receta de medicamento. Campo:
  `medicationCodeableConcept.text`.
- **AllergyIntolerance** — una alergia. Campo: `code.text`.

## Notas importantes

- Los datos son **históricos** (fechas desde ~1950 hasta 2019). Preguntas de
  fechas relativas como "hoy" o "últimas 72 horas" muy probablemente no
  devuelvan filas, porque no hay datos recientes. Para rangos, conviene usar
  fechas absolutas o el máximo presente en los datos.
- Un paciente puede tener muchos Encounters, Conditions, Observations, etc.
- Para contar "pacientes" usa `tipo = 'Patient'`; para "ingresos" usa
  `tipo = 'Encounter'`.
