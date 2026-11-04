# Optional synthetic analytics demo

Follow the canonical [demo installation guide](../docs/INSTALL_DEMO.md) for
checkout, environment setup, private definitions and secret setup, synthetic
SQL deployment, Genie import, configuration, compile/preflight/strict validation,
approval, deployment and smoke checks. The [customer-data guide](../docs/INSTALL_CUSTOMER.md)
uses existing resources without seeding synthetic data.

This directory supplies linked data for three domains, certified Unity Catalog
table functions, governed Markdown and importable Genie definitions. Genie
Agents remain external dependencies: the installer creates them separately and
inserts the returned space IDs into the rendered configuration.

| Domain | Table | Certified function |
| --- | --- | --- |
| Retail | `orders` (360 rows) | `monthly_revenue()` |
| Support | `support_tickets` (90 rows) | `resolution_hours()` |
| Marketing | `campaign_daily` (360 rows) | `conversion_rate()` |

Data covers January through March 2025 and is deterministic. Tickets reference
existing orders. Channel purchases reconcile with completed orders. Paid social
has more sessions and spend but lower conversion than direct traffic. Dates in
the sample tables are ISO strings. Amounts are synthetic currency units.

## Asset map

- [config/demo.yml](config/demo.yml) is an offline template. Preparation renders
  an ignored `.generated-demo-setup/demo.yml` with deliberately invalid space-ID
  markers; only actual Genie create responses complete that config.
- [generators/prepare.py](generators/prepare.py) produces rendered YAML, complete
  `seed.sql` and three version-2 Genie create requests without credentials.
- [generators/generate.py](generators/generate.py) produces deterministic rows
  and namespace-qualified SQL. [sql/functions.sql](sql/functions.sql) holds
  the three certified table function bodies.
- [genie/README.md](genie/README.md) describes import/export request structure;
  the installation guide provides the authoritative commands and approval gates.
- `definitions/` supplies four governed Markdown files. Copy them into the
  configured private repository at `demo/definitions/`; the App fetches GitHub
  rather than reading local checkout files.
- [resources/demo.job.yml](resources/demo.job.yml) adds `demo_seed` only when
  both compile and preflight use `--demo`. No job runs merely on deployment.

The initial seed and later reseed job **replace the three named sample tables
and functions**. Use a dedicated schema and obtain approval before running
these actions. Preparing again overwrites inserted IDs; importing again creates
additional agents. Preserve successful import responses for reuse and cleanup.

Production compilation excludes the entire demo tree from DAB synchronization.
Demo compilation includes it with the optional reseed job. Recompiling without
`--demo` removes the overlay from output but leaves seeded objects and external
agents intact. See [Operations](../docs/OPERATIONS.md) for cleanup and retained
trace-data boundaries.

After the documented environment setup, run the offline asset contracts:

```bash
python -m pytest tests/test_demo_contract.py -v
```

These execute real generators, metric queries, compiler, parser and routing
against synthetic external service boundaries. Databricks DDL, authenticated
validation/import/deployment and live model decisions still require workspace
smoke checks using a human-selected profile.
