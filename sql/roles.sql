-- Rol de solo lectura para el agente (endpoints que solo consultan). Nunca
-- puede INSERT/UPDATE/DELETE/CREATE/DROP; la ingesta (api/ingesta.py,
-- watcher.py) sigue usando el rol `fhir` normal.
--
-- Uso: psql -U fhir -d fhir_db -v pw='la_contraseña' -f sql/roles.sql
-- (la contraseña llega como variable psql :pw, nunca queda escrita aquí).
--
-- Idempotente: seguro de re-ejecutar sobre una base ya inicializada.
--
-- gotcha: psql NO sustituye variables (:'pw') dentro de bloques DO $$ ... $$
-- (la interpolación no entra en texto dollar-quoted), así que el CREATE/ALTER
-- ROLE se arma como texto con \gexec en vez de un IF/ELSE en PL/pgSQL.
SELECT 'CREATE ROLE fhir_lector LOGIN PASSWORD ' || quote_literal(:'pw') AS ddl
    WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fhir_lector') \gexec

SELECT 'ALTER ROLE fhir_lector PASSWORD ' || quote_literal(:'pw') AS ddl
    WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fhir_lector') \gexec

-- GRANT CONNECT necesita el nombre de la base como identificador, no como
-- variable normal; se resuelve en runtime con current_database() para no
-- hardcodear POSTGRES_DB aquí.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO fhir_lector', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO fhir_lector;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO fhir_lector;

-- Las tablas FHIR nacen dinámicas: api/ingesta.py crea una tabla nueva la
-- primera vez que llega un resourceType distinto, con el dueño de la conexión
-- que corre este script (current_user, no hardcodeado: en un volumen limpio
-- con otro POSTGRES_USER, "FOR ROLE fhir" fallaría porque ese rol no existe).
-- Sin este DEFAULT PRIVILEGES, cada tabla nueva quedaría invisible para
-- fhir_lector hasta correr el GRANT SELECT a mano.
DO $$
BEGIN
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public GRANT SELECT ON TABLES TO fhir_lector', current_user);
END
$$;

-- Cinturón y tirantes: aunque el rol solo tiene SELECT, esto bloquea
-- cualquier escritura aunque algún GRANT quede mal puesto a futuro.
ALTER ROLE fhir_lector SET default_transaction_read_only = on;
