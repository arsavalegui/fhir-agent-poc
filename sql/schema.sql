-- Almacén de datos "dinámico" estilo variante de KQL.
-- El JSON crudo (cualquier recurso FHIR) se guarda tal cual en jsonb;
-- NO se aplana. Se consulta con operadores jsonb (->, ->>, @>, jsonpath).

CREATE TABLE IF NOT EXISTS resources (
  id          TEXT PRIMARY KEY,          -- resourceType/id (único)
  tipo        TEXT NOT NULL,             -- resourceType (Patient, Encounter, ...)
  paciente_id TEXT,                      -- uuid del paciente al que pertenece
  fuente      TEXT NOT NULL DEFAULT 'fhir',
  recurso     JSONB NOT NULL,            -- el recurso crudo, sin tocar
  cargado_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Índice GIN: consultas por contención (@>) y por claves sobre el jsonb.
CREATE INDEX IF NOT EXISTS resources_recurso_gin ON resources USING gin (recurso);
CREATE INDEX IF NOT EXISTS resources_tipo_idx     ON resources (tipo);
CREATE INDEX IF NOT EXISTS resources_paciente_idx ON resources (paciente_id);

-- Vista anonimizada (capa PII básica): enmascara identificadores directos
-- del recurso Patient. Los demás recursos referencian al paciente por uuid
-- (seudónimo), no por nombre, así que ya son seudonimizados.
-- El agente consulta SIEMPRE esta vista, nunca la tabla cruda.
CREATE OR REPLACE VIEW resources_anon AS
SELECT
  id, tipo, paciente_id, fuente, cargado_at,
  CASE WHEN tipo = 'Patient' THEN
    (recurso - 'name' - 'telecom' - 'address' - 'identifier'
             - 'photo' - 'contact' - 'communication')
    || jsonb_build_object('birthYear', left(recurso->>'birthDate', 4))
  ELSE recurso END AS recurso
FROM resources;
