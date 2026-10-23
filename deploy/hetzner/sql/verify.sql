-- Open Persona: post-restore verification facts, one `key|value` per row.
--
-- Run as the superuser `persona` (bin/restore.sh, bin/deploy.sh). Prints counts and schema
-- facts only, never row contents. The superuser bypasses RLS, so the row counts are the
-- whole table, every tenant.

SELECT 'tables', count(*)::text
  FROM pg_tables WHERE schemaname = 'public';

SELECT 'forced_rls_tables', count(*)::text
  FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relrowsecurity AND c.relforcerowsecurity;

SELECT 'policies', count(*)::text
  FROM pg_policies WHERE schemaname = 'public';

SELECT 'persona_app_superuser', rolsuper::text FROM pg_roles WHERE rolname = 'persona_app';
SELECT 'persona_app_bypassrls', rolbypassrls::text FROM pg_roles WHERE rolname = 'persona_app';

SELECT 'persona_app_tables_without_select', count(*)::text
  FROM pg_tables t
 WHERE t.schemaname = 'public'
   AND NOT has_table_privilege('persona_app', format('%I.%I', t.schemaname, t.tablename), 'SELECT');

-- persona_platform: the api's cross-tenant role. It must never be a superuser, and it must
-- hold DML on every table. The CASE keeps the privilege check from running (and erroring)
-- on a database where the role does not exist yet.
SELECT 'persona_platform_superuser', rolsuper::text FROM pg_roles WHERE rolname = 'persona_platform';

SELECT 'persona_platform_tables_without_dml', count(*)::text
  FROM pg_tables t
 WHERE t.schemaname = 'public'
   AND CASE WHEN EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'persona_platform')
            -- One check per privilege: a comma list would be true if ANY were held.
            THEN NOT (has_table_privilege('persona_platform', format('%I.%I', t.schemaname, t.tablename), 'SELECT')
                  AND has_table_privilege('persona_platform', format('%I.%I', t.schemaname, t.tablename), 'INSERT')
                  AND has_table_privilege('persona_platform', format('%I.%I', t.schemaname, t.tablename), 'UPDATE')
                  AND has_table_privilege('persona_platform', format('%I.%I', t.schemaname, t.tablename), 'DELETE'))
            ELSE true END;

SELECT 'vector_extension', extversion FROM pg_extension WHERE extname = 'vector';

SELECT 'vector_columns', count(*)::text
  FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_type ty ON ty.oid = a.atttypid
 WHERE n.nspname = 'public' AND ty.typname = 'vector' AND a.attnum > 0 AND NOT a.attisdropped
   AND c.relkind = 'r';

SELECT 'hnsw_indexes', count(*)::text
  FROM pg_indexes WHERE schemaname = 'public' AND indexdef ILIKE '%USING hnsw%';

SELECT 'alembic_revision', string_agg(version_num, ',') FROM alembic_version;

SELECT 'server_version', current_setting('server_version');

-- Per-table row counts (counts only).
SELECT 'rows.' || t.table_name,
       (xpath('/row/c/text()',
              query_to_xml(format('SELECT count(*) AS c FROM %I.%I', t.table_schema, t.table_name),
                           false, true, '')))[1]::text
  FROM information_schema.tables t
 WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
 ORDER BY 1;
