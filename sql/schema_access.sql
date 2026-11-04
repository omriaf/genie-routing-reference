-- Databricks notebook source
-- Traversal only: leaf MODIFY / EXECUTE privileges belong to App bindings.
-- EXECUTE IMMEDIATE avoids requiring the newer GRANT IDENTIFIER grammar.
-- All identifiers are quoted; embedded backticks in principals are escaped.
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
