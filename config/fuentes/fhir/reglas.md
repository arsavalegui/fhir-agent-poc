# Reglas del agente para la fuente FHIR

Reglas específicas al consultar datos clínicos FHIR (una tabla por tipo de
recurso). Complementan las reglas generales.

## Cómo consultar

- El nombre de la tabla ES el tipo de recurso en minúsculas: `patient`,
  `encounter`, `condition`, `observation`, `procedure`, `immunization`, etc.
- Los campos anidados salen del jsonb con `->` y `->>`. Ejemplos:
  - Género: `recurso->>'gender'`
  - Clase del encuentro: `recurso->'class'->>'code'`
  - Nombre del diagnóstico: `recurso->'code'->>'text'`
  - Vacuna: `recurso->'vaccineCode'->>'text'`

## Ejemplos correctos (few-shot)

Pregunta: ¿Cuántos pacientes hay?
```sql
SELECT count(*) AS pacientes FROM patient;
```

Pregunta: ¿Cuáles son los diagnósticos más comunes?
```sql
SELECT recurso->'code'->>'text' AS diagnostico, count(*) AS total
FROM condition GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

Pregunta: ¿Cuántos ingresos de emergencia hubo?
```sql
SELECT count(*) AS emergencias FROM encounter
WHERE recurso->'class'->>'code' = 'EMER';
```

Pregunta: ¿Cuántos pacientes hay por género?
```sql
SELECT recurso->>'gender' AS genero, count(*) AS total
FROM patient GROUP BY 1 ORDER BY 2 DESC;
```

Pregunta: ¿Cuáles son las vacunas más aplicadas?
```sql
SELECT recurso->'vaccineCode'->>'text' AS vacuna, count(*) AS dosis
FROM immunization GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

Pregunta: ¿Cuántos ingresos de emergencia tuvo cada paciente?
```sql
SELECT p.paciente_id, count(*) AS emergencias
FROM patient p
JOIN encounter e ON p.paciente_id = e.paciente_id
WHERE e.recurso->'class'->>'code' = 'EMER'
GROUP BY 1 ORDER BY 2 DESC;
```

Pregunta: ¿Qué medicamentos toman los pacientes con Prediabetes?
```sql
SELECT DISTINCT m.recurso->'medicationCodeableConcept'->>'text' AS medicamento
FROM condition c
JOIN medicationrequest m ON m.paciente_id = c.paciente_id
WHERE c.recurso->'code'->>'text' ILIKE '%prediabetes%';
```

Pregunta: ¿Cuántas emergencias y cuántas recetas tiene cada paciente con Prediabetes?
(3+ tablas: cada relación se cuenta en su propia subconsulta, NO con JOINs encadenados)
```sql
SELECT DISTINCT c.paciente_id,
  (SELECT count(*) FROM encounter e
    WHERE e.paciente_id = c.paciente_id
      AND e.recurso->'class'->>'code' = 'EMER') AS emergencias,
  (SELECT count(*) FROM medicationrequest m
    WHERE m.paciente_id = c.paciente_id) AS recetas
FROM condition c
WHERE c.recurso->'code'->>'text' ILIKE '%prediabetes%';
```

Pregunta: Para cada paciente con al menos una alergia, dime género, año de
nacimiento, cuántas condiciones, cuántos encuentros de emergencia y fecha de
su última vacuna, ordenado por número de encuentros de emergencia.
("al menos una" → EXISTS, no JOIN; cada agregado en su propia subconsulta
correlacionada por paciente_id; campos siempre leídos de `recurso`)
```sql
SELECT p.recurso->>'gender' AS genero,
       p.recurso->>'birthYear' AS anio_nacimiento,
       (SELECT count(*) FROM condition c
         WHERE c.paciente_id = p.paciente_id) AS condiciones,
       (SELECT count(*) FROM encounter e
         WHERE e.paciente_id = p.paciente_id
           AND e.recurso->'class'->>'code' = 'EMER') AS emergencias,
       (SELECT max((i.recurso->>'occurrenceDateTime')::date) FROM immunization i
         WHERE i.paciente_id = p.paciente_id) AS ultima_vacuna
FROM patient p
WHERE EXISTS (
  SELECT 1 FROM allergyintolerance a WHERE a.paciente_id = p.paciente_id
)
ORDER BY emergencias DESC;
```

## Reglas de negocio

- "Ingreso/admisión/visita/consulta" → tabla `encounter`.
- "Enfermedad/diagnóstico/padecimiento" → tabla `condition`.
- "Procedimiento/cirugía/intervención" → tabla `procedure` (NO `encounter`:
  un encuentro es la visita/consulta en sí, no lo que se le hizo al paciente).
- Al contar por categoría, ordena de mayor a menor y limita a 10 salvo que pidan
  todo.
- Consulta solo tablas que existan y estén autorizadas (van en el mensaje de
  acceso a datos). No inventes nombres de tabla.
- Nunca intentes devolver el nombre del paciente: no está en la tabla `patient`.
- Todo JOIN entre tablas va por `paciente_id = paciente_id` (existe en TODAS,
  incluida `patient`). No hay columna `id`; el identificador del recurso vive
  en `recurso->>'id'` pero NO sirve para unir tablas (es el id del recurso,
  no el del paciente). Tampoco inventes columnas que no están en el esquema
  (`encounterId` no existe).
- Para filtrar por enfermedad/diagnóstico SIEMPRE une con `condition`;
  `patient` no tiene campos de diagnóstico.
- Al combinar DOS O MÁS tablas de detalle del mismo paciente (encounter,
  medicationrequest, condition...), NUNCA las encadenes con JOINs directos:
  multiplica filas (producto cartesiano) y los conteos salen inflados. Cuenta
  cada tabla en su propia subconsulta correlacionada por `paciente_id`, como en
  el ejemplo de emergencias y recetas.
- "Al menos uno/una de X" (alergia, diagnóstico, vacuna...) → `EXISTS (SELECT 1
  FROM tabla_x WHERE tabla_x.paciente_id = p.paciente_id)`, nunca un JOIN (un
  JOIN duplica la fila del paciente si tiene más de un registro en `tabla_x`).
