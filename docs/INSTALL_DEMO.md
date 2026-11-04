# Install the synthetic demonstration

This opt-in path creates synthetic retail, support and marketing tables and
three certified functions in a dedicated schema. It separately imports three
Genie Agents, then deploys the router with the demo overlay. Keep this schema
isolated from customer data. Read [README](../README.md) and [Security](SECURITY.md).

## 1. Prerequisites and checkout

Use macOS or Linux, Python 3.11+, Git, uv, jq and Databricks CLI 1.0+.
The workspace must support Unity Catalog, Apps, Genie, model serving, MLflow,
AI/BI dashboards, a Pro or Serverless SQL warehouse, and serverless notebook
jobs for the optional reseeding job. The catalog, warehouse and classifier
endpoint must already exist. App outbound HTTPS to GitHub must be allowed.
The installer needs the delegation permissions listed in
[the customer guide](INSTALL_CUSTOMER.md), plus DDL rights to create/replace
the dedicated demo tables/functions and permission to create Genie Agents in
the selected workspace folder.

Replace `<PUBLIC_GIT_URL>` with the public clone URL and choose `<PROFILE>`
yourself. Never let an assistant choose a profile or use an ambient default.

```bash
git clone "<PUBLIC_GIT_URL>" genie-routing-reference
cd genie-routing-reference
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e '.[test]'
databricks version --profile "<PROFILE>"
```

If necessary, create that named profile through browser OAuth, then confirm
the selected identity privately:

```bash
databricks auth login --host "<WORKSPACE_URL>" --profile "<PROFILE>"
databricks current-user me --profile "<PROFILE>"
```

## 2. Choose the namespace and private definitions repository

Choose an existing `<CATALOG>`, a new dedicated `<DEMO_SCHEMA>`, existing
`<WAREHOUSE_ID>`, existing `<CLASSIFIER_ENDPOINT>`, unused absolute
`<EXPERIMENT_PATH>`, and `<GENIE_PARENT_PATH>` beginning `/Workspace/`.
Catalog/schema identifiers support letters, digits and underscores, starting
with a letter or underscore. Confirm the warehouse is Pro or Serverless.

Set up a separate private GitHub definitions repository, a fine-grained token
restricted to that repository with **Contents: Read-only**, and a Databricks
secret through [customer guide step 3](INSTALL_CUSTOMER.md). That step is
human-only for the token input. Assistants collect only the repository URL,
branch, `secret_scope` and `secret_key` names. Commit and push the supplied
`demo/definitions/*.md` into that repository at the identical
`demo/definitions/` path on `<DEFINITIONS_BRANCH>`. The App fetches these files
from GitHub, not the source checkout.

## 3. Prepare locally, seed synthetic data, and create Genie Agents

The following command is offline. Replace every placeholder; preparation
produces invalid space-ID markers intentionally. Do not compile yet.

```bash
python -m demo.generators.prepare \
  --catalog "<CATALOG>" --schema "<DEMO_SCHEMA>" \
  --warehouse-id "<WAREHOUSE_ID>" \
  --classifier-endpoint "<CLASSIFIER_ENDPOINT>" \
  --experiment-path "<EXPERIMENT_PATH>" \
  --repository-url "<DEFINITIONS_REPOSITORY_URL>" --branch "<DEFINITIONS_BRANCH>" \
  --secret-scope "<SECRET_SCOPE>" --secret-key "<SECRET_KEY>" \
  --parent-path "<GENIE_PARENT_PATH>" --output .generated-demo-setup
```

It renders `.generated-demo-setup/demo.yml`, `seed.sql`, and three
`*.create.json` requests. The folder is ignored by Git. Inspect `seed.sql`
locally: it creates the schema and **replaces** `orders` (360 rows),
`support_tickets` (90 rows), `campaign_daily` (360 rows), and table functions
`monthly_revenue`, `resolution_hours`, `conversion_rate`. All rows are
deterministic synthetic data for January–March 2025. Never seed into an
existing business schema. Re-running preparation overwrites the rendered
configuration, including inserted agent IDs.

Get explicit **demo setup approval** for these DDL replacements and three
external Genie creations. This is separate from router deployment approval.
After approval, open `seed.sql` in the Databricks SQL editor of the selected
workspace, select the chosen warehouse and run the entire script. Confirm all
three tables and functions exist and query successfully before creating agents.

Inspect each generated request, especially warehouse, namespace, parent folder
and the decoded `serialized_space`. That field is a JSON string containing the
version-2 Genie definition. Then import once per domain:

```bash
jq '.serialized_space | fromjson' .generated-demo-setup/retail.create.json
jq '.serialized_space | fromjson' .generated-demo-setup/support.create.json
jq '.serialized_space | fromjson' .generated-demo-setup/marketing.create.json
databricks workspace mkdirs "<GENIE_PARENT_PATH>" --profile "<PROFILE>"
databricks genie create-space --json @.generated-demo-setup/retail.create.json --profile "<PROFILE>" --output json > .generated-demo-setup/retail.created.json
databricks genie create-space --json @.generated-demo-setup/support.create.json --profile "<PROFILE>" --output json > .generated-demo-setup/support.created.json
databricks genie create-space --json @.generated-demo-setup/marketing.create.json --profile "<PROFILE>" --output json > .generated-demo-setup/marketing.created.json
jq -er '.space_id' .generated-demo-setup/retail.created.json
jq -er '.space_id' .generated-demo-setup/support.created.json
jq -er '.space_id' .generated-demo-setup/marketing.created.json
```

Stop if any import fails. Preserve successful responses; retry only the failed
domain. Re-running a successful create command creates a duplicate agent.
Each returned `space_id` must be a 32-character hexadecimal string. In
`.generated-demo-setup/demo.yml`, replace `REPLACE_RETAIL_SPACE_ID`,
`REPLACE_SUPPORT_SPACE_ID` and `REPLACE_MARKETING_SPACE_ID` with the respective
returned IDs, as **quoted YAML strings**. Never substitute a made-up ID.
The compiler and DAB do not create agents or discover their IDs.

Verify each imported Agent in the Genie UI using its included sample question.
Confirm its source table and selected warehouse. Its execution identity needs
warehouse `CAN_USE`, UC traversal and `SELECT` on source data; the installer
needs `CAN_MANAGE` to delegate App access. Review the rendered App name and
experiment path for collisions, routing thresholds and optional reader lists.
The rendered config uses `poll_seconds: 30` and the three sample function FQNs.

## 4. Compile, preflight and strict validation

Use `--demo` for both compile and preflight; otherwise preflight recompiles in
customer mode and removes the demo overlay. Stop on any failure.

```bash
python -m setup.compile_config --config .generated-demo-setup/demo.yml --output .generated --demo
python -m setup.preflight --config .generated-demo-setup/demo.yml --output .generated --demo --profile "<PROFILE>"
python -m setup.bundle validate --strict --target customer --profile "<PROFILE>"
python -m json.tool .generated/summary.json
```

Inspect the redacted summary: `demo` must be true and `demo_resource_counts`
must include the reseeding job. Review bindings and grants locally in
`.generated/customer.resources.yml`. Demo compilation includes demo source
uploads; customer compilation excludes them. No seed job runs automatically.

**Stop for deployment approval now.** Show the human the redacted summary,
preflight result, strict validation exit/result with workspace metadata
redacted, selected profile/target and the proposed router resources/grants.
Explain that `demo_seed` can replace the sample tables/functions if explicitly
run. Obtain approval for bootstrap and deployment. Configuration changes
invalidate this approval and require recompilation and validation.

## 5. Deploy and start after approval

```bash
python -m setup.bootstrap --config .generated/runtime_config.json --profile "<PROFILE>"
python -m setup.bundle deploy --target customer --profile "<PROFILE>"
python -m setup.bundle run --resource router_bootstrap --target customer --profile "<PROFILE>"
python -m setup.bundle run --resource router_app --target customer --profile "<PROFILE>"
databricks bundle summary --target customer --profile "<PROFILE>"
```

The first bootstrap initializes the trace table; the bundle bootstrap grants
App traversal and optional dashboard-reader access after App creation. The App
run starts its code. Record the resource links from the bundle summary privately.

For a later **explicitly approved reseed** only, the optional notebook job is:

```bash
python -m setup.bundle run --resource demo_seed --target customer --profile "<PROFILE>"
```

Its run identity needs DDL privileges on the dedicated sample schema. It does
not create Genie Agents, change their IDs or load definitions into GitHub.

## 6. Demo smoke checks

Use an authenticated session to open the App's `/health`; expect HTTP 200,
`ready: true`, `status: ready` and a definitions revision. Delta tracing may be
`not_ready` until the first request; MLflow initializes at startup. After that
request check both trace sinks again and require both to be ready. Connect an
MCP client supporting Databricks Apps OAuth to the App
URL's `/mcp`, sign in as an intended reader and discover `ask_analytics`.
Call it with a JSON argument such as
`{"question": "Compare completed orders by channel"}`.

| Question / action | Expected result |
| --- | --- |
| Compare completed orders by channel | `route=genie`, `domain=retail`, `status=answered` |
| Describe ticket volume by priority | `route=genie`, `domain=support`, `enrichment_method=exact_alias` |
| Compare sessions and spend by channel | `route=genie`, `domain=marketing` |
| What is monthly revenue? | `route=uc_function`, configured `monthly_revenue` FQN, three monthly rows |
| Show average resolution hours | `route=uc_function`, configured `resolution_hours` FQN, positive result |
| Show purchase conversion rate | `route=uc_function`, configured `conversion_rate` FQN, values between zero and one |
| What is the engagement efficiency index? | `route=fail_loud`, `status=undefined_term` or `no_route` |
| What is the weather forecast? | `route=fail_loud`, `status=no_route` |
| Update the heading/body of `monthly_revenue.md` through reviewed Git changes, then wait at least 30 seconds and ask again | New `defs_commit_sha`, same approved function, no redeployment |

Verify trace IDs, route, configuration fingerprint, definitions revision and
latency in both Delta and the configured MLflow experiment. Open the dashboard
as an intended reader. Any unexpected route, error, 503 or degraded sink fails
the smoke check; model wording can vary. Follow [Operations](OPERATIONS.md)
for troubleshooting, updates and cleanup. Bundle destruction leaves synthetic
SQL objects and separately created Genie Agents for explicit owner cleanup.
