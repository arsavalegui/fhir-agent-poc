-- Migración: quita la columna `id` (PK "Tipo/uuid") de todas las tablas FHIR
-- y la reemplaza por un índice único sobre (recurso->>'id'), la expresión
-- que ahora usa el ON CONFLICT de la ingesta (api/ingesta.py). El orden de
-- columnas que queda es recurso, paciente_id, cargado_at.
--
-- Re-ejecutable: recorre TODAS las tablas del esquema public que tengan el
-- patrón de api/ingesta.py:asegurar_tabla (columnas id + recurso), sin
-- hardcodear los nombres de tipo FHIR; si ya se corrió, no hace nada.
DO $$
DECLARE
    tabla text;
BEGIN
    FOR tabla IN
        SELECT c.table_name
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
          AND c.column_name = 'id'
          AND EXISTS (
              SELECT 1 FROM information_schema.columns r
              WHERE r.table_schema = 'public' AND r.table_name = c.table_name
                AND r.column_name = 'recurso')
    LOOP
        EXECUTE format(
            'CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I ((recurso->>''id''))',
            tabla || '_recurso_id_uniq', tabla);
        EXECUTE format('ALTER TABLE %I DROP COLUMN IF EXISTS id', tabla);
        RAISE NOTICE 'migrada tabla %', tabla;
    END LOOP;
END $$;
