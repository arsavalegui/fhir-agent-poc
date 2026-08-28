# Reglas del agente para la fuente FHIR

Reglas específicas al consultar datos clínicos FHIR. Complementan las reglas
generales del agente.

## Cómo consultar el jsonb FHIR

- El tipo de recurso está en la columna `tipo` (más rápido que leer el jsonb).
- Para campos anidados usa el operador de ruta. Ejemplos:
  - Género del paciente: `recurso->>'gender'`
  - Clase del encuentro: `recurso->'class'->>'code'`
  - Inicio del encuentro: `(recurso->'period'->>'start')::timestamptz`
  - Nombre del diagnóstico: `recurso->'code'->>'text'`
  - Vacuna: `recurso->'vaccineCode'->>'text'`

## Ejemplos de consultas correctas (few-shot)

Pregunta: ¿Cuántos pacientes hay?
```sql
SELECT count(*) AS pacientes FROM resources_anon WHERE tipo = 'Patient';
```

Pregunta: ¿Cuáles son los diagnósticos más comunes?
```sql
SELECT recurso->'code'->>'text' AS diagnostico, count(*) AS total
FROM resources_anon WHERE tipo = 'Condition'
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

Pregunta: ¿Cuántos ingresos de emergencia hubo?
```sql
SELECT count(*) AS emergencias FROM resources_anon
WHERE tipo = 'Encounter' AND recurso->'class'->>'code' = 'EMER';
```

Pregunta: ¿Cuántos pacientes hay por género?
```sql
SELECT recurso->>'gender' AS genero, count(*) AS total
FROM resources_anon WHERE tipo = 'Patient' GROUP BY 1 ORDER BY 2 DESC;
```

Pregunta: ¿Cuáles son las vacunas más aplicadas?
```sql
SELECT recurso->'vaccineCode'->>'text' AS vacuna, count(*) AS dosis
FROM resources_anon WHERE tipo = 'Immunization'
GROUP BY 1 ORDER BY 2 DESC LIMIT 10;
```

## Reglas de negocio

- "Ingreso", "admisión", "visita", "consulta" → recurso `Encounter`.
- "Enfermedad", "diagnóstico", "padecimiento" → recurso `Condition`.
- Al contar por categoría, ordena de mayor a menor y limita a 10 salvo que
  pidan todo.
- Nunca intentes devolver el nombre del paciente: está protegido por la vista.
