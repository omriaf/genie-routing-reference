-- Databricks notebook source
-- Only explicitly configured dashboard readers receive these grants.
DECLARE OR REPLACE VARIABLE grant_statement STRING;

-- COMMAND ----------

SET VAR grant_statement = 'GRANT USE CATALOG ON CATALOG `'
  || replace(:catalog, '`', '``') || '` TO `'
  || replace(:principal, '`', '``') || '`';

-- COMMAND ----------

EXECUTE IMMEDIATE grant_statement;

-- COMMAND ----------

SET VAR grant_statement = 'GRANT USE SCHEMA ON SCHEMA `'
  || replace(:catalog, '`', '``') || '`.`' || replace(:schema, '`', '``')
  || '` TO `' || replace(:principal, '`', '``') || '`';

-- COMMAND ----------

EXECUTE IMMEDIATE grant_statement;

-- COMMAND ----------

SET VAR grant_statement = 'GRANT SELECT ON TABLE `'
  || replace(:catalog, '`', '``') || '`.`' || replace(:schema, '`', '``')
  || '`.`routing_traces` TO `' || replace(:principal, '`', '``') || '`';

-- COMMAND ----------

EXECUTE IMMEDIATE grant_statement;
