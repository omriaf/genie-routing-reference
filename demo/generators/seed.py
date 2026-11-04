# Databricks notebook source
# ruff: noqa: F821 -- spark and dbutils are provided by Databricks notebooks.
"""Rerun only in a dedicated sample schema: replaces the three sample tables."""

import sys

dbutils.widgets.text("catalog", "")
dbutils.widgets.text("schema", "")
dbutils.widgets.text("generators_dir", "")
sys.path.insert(0, dbutils.widgets.get("generators_dir"))

from generate import seed_sql

for statement in seed_sql(dbutils.widgets.get("catalog"), dbutils.widgets.get("schema")).split(";"):
    if statement.strip():
        spark.sql(statement)
