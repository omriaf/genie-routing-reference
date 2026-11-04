# Optional synthetic analytics demo

This opt-in sample supplies linked data for retail, support, and marketing,
three certified Unity Catalog functions, governed Markdown, and importable Genie
Agent definitions. Genie Agents remain external dependencies: the installer
creates them, then inserts the returned IDs into the rendered configuration.
The production compiler and runtime use their existing contracts unchanged.

| Domain | Table | Certified function |
| --- | --- | --- |
| Retail | `orders` (360 rows) | `monthly_revenue()` |
| Support | `support_tickets` (90 rows) | `resolution_hours()` |
| Marketing | `campaign_daily` (360 rows) | `conversion_rate()` |

Data covers January through March 2025 and is deterministic. Tickets reference
existing orders. Channel purchases reconcile with completed orders. Paid social
has more sessions and spend but lower conversion than direct traffic. Dates in
the sample tables are ISO strings. Amounts are synthetic currency units.

## Prepare locally

Run from the repository root with Python 3.11+ and the package dependencies
installed (`python -m pip install -e '.[test]'`). Choose an existing catalog and
a dedicated sample schema. Both must use the identifier subset documented in
[the configuration contract](../customer_config/README.md). All sample objects
and router traces use this configured catalog/schema. There are no baked-in
catalogs, schemas, warehouse IDs, workspace hosts, or Genie IDs.

Set these shell variables to your choices. `DEMO_PROFILE` must be the profile
you explicitly selected for this workspace. Preparation itself is offline and
does not use that profile or obtain credentials.

```bash
DEMO_PROFILE='<your-selected-profile>'
DEMO_CATALOG='<existing-catalog>'
DEMO_SCHEMA='<dedicated-sample-schema>'
DEMO_WAREHOUSE_ID='<sql-warehouse-id>'
DEMO_CLASSIFIER_ENDPOINT='<existing-classifier-endpoint>'
DEMO_EXPERIMENT_PATH='/Shared/sample-analytics-router'
DEMO_DEFINITIONS_REPOSITORY='https://github.com/<owner>/<repository>'
DEMO_SECRET_SCOPE='<existing-secret-scope>'
DEMO_SECRET_KEY='<existing-github-token-key-name>'
DEMO_PARENT_PATH='/Workspace/Shared/sample-analytics-agents'
DEMO_OUTPUT='.generated-demo-setup'

python -m demo.generators.prepare \
  --catalog "$DEMO_CATALOG" --schema "$DEMO_SCHEMA" \
  --warehouse-id "$DEMO_WAREHOUSE_ID" \
  --classifier-endpoint "$DEMO_CLASSIFIER_ENDPOINT" \
  --experiment-path "$DEMO_EXPERIMENT_PATH" \
  --repository-url "$DEMO_DEFINITIONS_REPOSITORY" --branch main \
  --secret-scope "$DEMO_SECRET_SCOPE" --secret-key "$DEMO_SECRET_KEY" \
  --parent-path "$DEMO_PARENT_PATH" --output "$DEMO_OUTPUT"
```

This renders [config/demo.yml](config/demo.yml) into
`.generated-demo-setup/demo.yml`, plus `seed.sql` and three `*.create.json`
requests. The output folder is already ignored by Git. The checked-in YAML is
a template; the compiler consumes its rendered copy. Its `REPLACE_*_SPACE_ID`
markers intentionally fail configuration validation until replaced with real
IDs. Re-running preparation overwrites these files, including any inserted IDs.

The warehouse and classifier endpoint must already exist. The chosen profile
must have the grants and delegation authority required by production preflight.
The GitHub secret scope/key must already reference a repository credential;
only the names are passed here. Store credentials through your organization's
existing process. The sample does not create, retrieve, or print secret values.

## Seed data, then import Genie Agents

1. Open `.generated-demo-setup/seed.sql` in the Databricks SQL editor in the
   selected workspace. Select the warehouse supplied above and run the entire
   script. It creates the sample schema, three Delta tables, and three table
   functions. The installer needs catalog traversal, schema creation if absent,
   and table/function creation and replacement privileges in that schema.
2. Follow [the exact Genie import flow](genie/README.md). It uses the three
   generated requests, creates agents under your chosen workspace folder, and
   returns workspace-specific IDs. Insert each returned `space_id` into its
   matching entry in `.generated-demo-setup/demo.yml`. This is the local,
   rendered copy of `demo/config/demo.yml`.
3. Commit the supplied `demo/definitions/*.md` to your chosen GitHub repository
   and branch at `demo/definitions/`. Preserve the production front-matter keys:
   `name`, `domain`, `aliases`, and `certified_function`. Functions are logical
   keys from the config allowlist; `null` supplies context for a Genie route.
   The runtime loads this folder from GitHub, so local files alone do not make
   the App ready.

`seed.sql` and the demo job both **replace the three sample tables and sample
functions**. Use a dedicated schema and keep unrelated objects under other
names. The initial SQL-editor run lets you create the external Genie Agents
before compilation; the optional bundle job supports later reruns. No job runs
merely because you compile or deploy.

## Compile and deploy

```bash
python -m setup.compile_config --config "$DEMO_OUTPUT/demo.yml" \
  --output .generated --demo
python -m setup.preflight --config "$DEMO_OUTPUT/demo.yml" \
  --output .generated --demo --profile "$DEMO_PROFILE"
python -m setup.bootstrap --config .generated/runtime_config.json \
  --profile "$DEMO_PROFILE"
python -m setup.bundle validate --strict --target customer --profile "$DEMO_PROFILE"
python -m setup.bundle deploy --target customer --profile "$DEMO_PROFILE"
python -m setup.bundle run --resource router_bootstrap --target customer \
  --profile "$DEMO_PROFILE"
python -m setup.bundle run --resource router_app --target customer \
  --profile "$DEMO_PROFILE"
```

Grant the App principal access to run the imported agents through the generated
App bindings; the installer must be able to delegate that access. Ensure the
Genie execution identity has warehouse `CAN_USE`, catalog/schema traversal, and
`SELECT` on its source table. The bootstrap grants App schema traversal, and the
certified-function bindings supply `EXECUTE`. Configure optional App/dashboard
readers in the rendered YAML using the production permissions contract.

To reseed after deployment, run the optional job explicitly. It requires
serverless notebook jobs in the workspace and executes as the job's configured
run identity, which needs the same sample DDL privileges as the installer:

```bash
python -m setup.bundle run --resource demo_seed --target customer \
  --profile "$DEMO_PROFILE"
```

Only compiler `--demo` includes `demo_seed` in the generated resource document.
The generated fragment also controls DAB synchronization: production excludes
every `demo/` file from upload, while demo mode includes the demo source tree,
including the notebook's Python helper and SQL file. Recompiling changes both
the resource overlay and upload set together.
Omitting it (including on preflight) emits production resources only. Recompile
without the flag to remove the overlay from generated output. Existing sample
data and external agents remain until the installer removes them. Ordinary
production compilation requires no demo preparation and does not read the demo
resource file.

## Smoke checks

Call the App's `ask_analytics` MCP tool and inspect the returned route/status.
The classifier still runs once for every accepted question; exact wording of
live model answers can vary.

| Path | Question or action | Expected result |
| --- | --- | --- |
| Retail Genie | Compare completed orders by channel | `route=genie`, domain `retail` |
| Support Genie with Markdown context | Describe ticket volume by priority | `route=genie`, domain `support`, `exact_alias` enrichment |
| Marketing Genie | Compare sessions and spend by channel | `route=genie`, domain `marketing` |
| Certified retail | What is monthly revenue? | `route=uc_function`, logical function `monthly_revenue` |
| Certified support | Show average resolution hours | `route=uc_function`, logical function `resolution_hours` |
| Certified marketing | Show purchase conversion rate | `route=uc_function`, logical function `conversion_rate` |
| Undefined metric | What is the engagement efficiency index? | `route=fail_loud`, `status=undefined_term` or `no_route` |
| Outside domains | What is the weather forecast? | `route=fail_loud`, `status=no_route` |
| Git refresh | Change the `Retail revenue` heading/body in `monthly_revenue.md`, commit and push, wait for the 30-second poll, then ask about monthly revenue again | New `defs_commit_sha`, same approved function, no redeployment |

For Git refresh, `/health` exposes the definitions revision and readiness. A
malformed document or undeclared function reference preserves the last valid
snapshot and marks definitions degraded. An invalid initial repository load
prevents readiness. Inspect routing traces for `git_definition_used`,
`enrichment_method`, and the matched term. This sample adds no second source of
runtime routing metadata.

Run offline contracts with `python -m pytest tests/test_demo_contract.py -v`.
They use the real compiler, parser, routing, and Git refresh logic with fake
external service responses. SQL aggregate semantics are checked locally with
SQLite; Databricks-specific DDL and live Genie/model behavior still need the
installer's workspace smoke checks.
