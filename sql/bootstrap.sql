-- Databricks notebook source
-- Parameters come from the compiled customer configuration, never SQL interpolation.
CREATE SCHEMA IF NOT EXISTS IDENTIFIER('`' || :catalog || '`.`' || :schema || '`');

-- COMMAND ----------

CREATE TABLE IF NOT EXISTS IDENTIFIER('`' || :catalog || '`.`' || :schema || '`.`routing_traces`') (
  request_ts TIMESTAMP,
  trace_id STRING,
  question STRING,
  status STRING,
  domain STRING,
  confidence DOUBLE,
  candidates STRING,
  route STRING,
  genie_space STRING,
  uc_function STRING,
  reasoning STRING,
  latency_ms BIGINT,
  fail_loud BOOLEAN,
  answer STRING,
  error STRING,
  failed_route STRING,
  mlflow_trace_ok BOOLEAN,
  trace_note STRING,
  defs_commit_sha STRING,
  git_definition_used BOOLEAN,
  matched_definition_term STRING,
  enrichment_method STRING,
  answering_genie STRING,
  config_fingerprint STRING
) USING DELTA;
