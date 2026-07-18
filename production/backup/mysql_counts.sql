SET SESSION group_concat_max_len = 16777216;

SELECT GROUP_CONCAT(
  CONCAT(
    'SELECT ', QUOTE(TABLE_SCHEMA), ' AS table_schema, ',
    QUOTE(TABLE_NAME), ' AS table_name, COUNT(*) AS row_count FROM `',
    REPLACE(TABLE_SCHEMA, '`', '``'), '`.`',
    REPLACE(TABLE_NAME, '`', '``'), '`'
  )
  ORDER BY TABLE_SCHEMA, TABLE_NAME
  SEPARATOR ' UNION ALL '
)
INTO @auris_count_sql
FROM information_schema.TABLES
WHERE TABLE_SCHEMA IN ('auris_flow', 'keycloak', 'dagster')
  AND TABLE_TYPE = 'BASE TABLE';

SET @auris_count_sql = COALESCE(
  @auris_count_sql,
  'SELECT ''auris_flow'' AS table_schema, ''__no_tables__'' AS table_name, 0 AS row_count WHERE FALSE'
);

SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
PREPARE auris_count_statement FROM @auris_count_sql;
EXECUTE auris_count_statement;
DEALLOCATE PREPARE auris_count_statement;
COMMIT;
