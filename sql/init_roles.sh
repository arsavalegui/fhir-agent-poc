#!/bin/bash
# Se monta en /docker-entrypoint-initdb.d/ (solo corre en un volumen nuevo,
# vacío). Ejecuta sql/roles.sql, montado aparte en /sql/roles.sql para que el
# entrypoint de postgres no lo tome como script propio y lo corra sin :pw.
set -e

if [ -z "$POSTGRES_PASSWORD_LECTURA" ]; then
    echo "init_roles.sh: falta POSTGRES_PASSWORD_LECTURA, no se crea fhir_lector" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v pw="$POSTGRES_PASSWORD_LECTURA" -f /sql/roles.sql
