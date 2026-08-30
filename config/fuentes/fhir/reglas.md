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

## Reglas de negocio

- "Ingreso/admisión/visita/consulta" → tabla `encounter`.
- "Enfermedad/diagnóstico/padecimiento" → tabla `condition`.
- Al contar por categoría, ordena de mayor a menor y limita a 10 salvo que pidan
  todo.
- Consulta solo tablas que existan y estén autorizadas (van en el mensaje de
  acceso a datos). No inventes nombres de tabla.
- Nunca intentes devolver el nombre del paciente: no está en la tabla `patient`.
