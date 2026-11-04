# Install with existing customer data

This path references customer resources and creates router infrastructure. Do
not run the synthetic seed or import demo agents. Read [README](../README.md),
[security](SECURITY.md), and the [configuration contract](../customer_config/README.md).

## 1. Prerequisites and checkout

Use macOS or Linux with Python 3.11+, Git, uv, jq, and Databricks CLI 1.0+.
The workspace needs Unity Catalog, Databricks Apps, Genie, model serving,
MLflow, AI/BI dashboards, and a Pro or Serverless SQL warehouse. Your terminal
needs access to public source and package indexes; the App needs outbound HTTPS
to the GitHub API and the selected Databricks services. Confirm regional and
organizational availability with the workspace owner.

Replace `<PUBLIC_GIT_URL>` with this public repository's clone URL. Choose an
empty local destination. These setup commands create a local environment:

```bash
git clone "<PUBLIC_GIT_URL>" genie-routing-reference
cd genie-routing-reference
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e '.[test]'
databricks version --profile "<PROFILE>"
```

Select `<PROFILE>` yourself; never ask an assistant to infer it from defaults,
environment variables or available profiles. If it does not exist, create the
named profile with browser OAuth using your workspace URL:

```bash
databricks auth login --host "<WORKSPACE_URL>" --profile "<PROFILE>"
databricks current-user me --profile "<PROFILE>"
```

Check the identity and workspace privately. Do not paste raw identity output
into a public issue. No command in this guide relies on a default profile.

## 2. Inventory dependencies and permissions

Record the following non-secret inventory before editing YAML. Use the
workspace UI for existing resources; retain exact spellings and quoted IDs.

| Inventory | Configuration / requirement |
| --- | --- |
| Customer label and unused App name | `customer.name`, `customer.app_name`; App name uses lowercase letters, digits and hyphens |
| Existing UC catalog and dedicated router schema name | `workspace.catalog`, `workspace.router_schema`; schema/table will be initialized if absent |
| Pro or Serverless warehouse ID | `workspace.warehouse_id`; installer needs `CAN_MANAGE` or ownership |
| Existing compatible chat classifier endpoint name | `workspace.classifier_endpoint`; installer needs `CAN_MANAGE` |
| New bundle-owned absolute MLflow experiment path | `workspace.mlflow_experiment`; avoid adopting an unrelated existing experiment |
| Each existing Genie Agent | `name`, `description`, 32-character hexadecimal `space_id`, `routing_guidance`; installer needs `CAN_MANAGE` |
| Each approved UC table function | Logical `name`, exact `full_name`, agent `domain`, `description`, `aliases`; use `uc_functions: []` for Genie-only routing |
| Private definitions location | Repository URL, branch, relative Markdown folder, polling interval |
| Existing secret metadata | `secret_scope`, `secret_key`; these are names, never token values |
| Routing policy and optional readers | Confidence/overlap thresholds, certified-function preference, existing App/dashboard groups or users |

All function FQNs must use the configured catalog. Catalog/schema/function
components allow letters, digits and underscores, beginning with a letter or
underscore; quoted SQL identifiers and hyphens are unsupported. A function
domain must match an agent's logical name. Do not invent resource IDs.

The installer needs catalog `USE_CATALOG` and `MANAGE` or ownership; add
`CREATE_SCHEMA` when the router schema is missing. Existing router/function
schemas need `USE_SCHEMA` and `MANAGE` or ownership; add `CREATE_TABLE` for a
missing trace table. An existing trace table needs `MODIFY` and `MANAGE` or
ownership and the exact compatible Delta schema. Functions need `EXECUTE` and
`MANAGE` or ownership. Secret scope ACLs must record installer `MANAGE`.
Preflight requires readable ACL metadata and a direct identity/group match;
unexposed admin authority and nested-group inheritance are not guessed.

Also obtain workspace rights to create/manage Apps, jobs, dashboards and the
experiment, and to run bootstrap SQL/grants. These are not all proven by
preflight. The Genie execution identity separately needs warehouse use,
catalog/schema traversal, and `SELECT` on its source tables. Verify each
existing agent's sample question and each approved function in the workspace
before installation. [Security](SECURITY.md) separates these identities.

## 3. Private GitHub Markdown and read-only credential

Create or choose a separate **private** GitHub definitions repository and a
reviewed branch. Populate the chosen relative folder with UTF-8 `.md` files.
Each file requires a nonempty body and these YAML front-matter fields; replace
placeholders with logical keys from your inventory:

```yaml
---
name: <METRIC_KEY>
domain: <DOMAIN_NAME>
aliases: [<BUSINESS_PHRASE>]
certified_function: <FUNCTION_LOGICAL_NAME>
---
Describe the governed calculation, scope, exclusions, and interpretation here.
```

Use `certified_function: null` for terms that enrich a Genie question. Otherwise
the function must be allowlisted in YAML under the same domain. Names and
aliases must be unique across the folder after whitespace/case normalization.
At least one valid document is required for startup. A definition's body is
context; it cannot register executable SQL or expand the function allowlist.
Commit and push the reviewed files to the selected branch before starting the
App. Local Markdown files are not a runtime source.

In GitHub Settings → Developer settings → Personal access tokens → Fine-grained
tokens, the **human operator** creates a token with the repository owner as
resource owner, **Only select repositories** restricted to the one private
definitions repository, **Contents: Read-only**, and GitHub's required metadata
read access. Set an expiry and obtain organization approval/SSO authorization
if required. Do not grant write access or broad repository access.

The human stores the token using the organization's secret-management process.
If a new Databricks-backed scope is needed, the human can create it first.
The following `put-secret` command is **human-only**, in a private terminal
without session recording or shell tracing. Enter the token only at the CLI's
interactive secret prompt. An assistant must never run this interactive step,
request the token, capture its input, or read it back. Existing keys are
overwritten, so confirm the scope/key before writing.

```bash
databricks secrets create-scope "<SECRET_SCOPE>" --profile "<PROFILE>"
databricks secrets put-secret "<SECRET_SCOPE>" "<SECRET_KEY>" --profile "<PROFILE>"
```

Skip scope creation when reusing a scope; an existing scope is not an error to
work around by changing names. Have its owner grant installer `MANAGE` if
needed. An assistant may verify **metadata only**, with these commands:

```bash
databricks secrets list-secrets "<SECRET_SCOPE>" --profile "<PROFILE>"
databricks secrets list-acls "<SECRET_SCOPE>" --profile "<PROFILE>"
```

Confirm the key name exists and the installer has recorded `MANAGE`. These
checks do not prove the credential is valid or that GitHub permits the App's
initial fetch; the live startup/smoke check does that. Never retrieve a secret
value for verification. [Security](SECURITY.md) covers rotation and retention.

## 4. Complete the local configuration

```bash
cp customer_config/customer.example.yml customer_config/customer.yml
```

Edit only the ignored `customer_config/customer.yml`. Replace all sample
customer/workspace/repository/agent/function/reader values with your inventory.
Remove sample agents/functions you do not use; keep at least one agent. Set
`permissions.app: []` and `permissions.dashboard: []` unless adding explicit
existing readers. Every optional principal entry uses exactly one `group_name`
or `user_name`. Quote IDs, use numbers for thresholds, integers for polling,
and YAML booleans for flags. Do not add hosts, profiles, passwords or tokens.
See the [field contract](../customer_config/README.md) for every required field.

## 5. Compile, preflight and strict validation

Run in this order; stop on any nonzero exit. Compilation is offline and
atomically stages configuration, App sources and resource YAML together.
Preflight recompiles and checks selected installer metadata/authority without
reading secret values. Customer mode always omits `--demo`.

```bash
python -m setup.compile_config --config customer_config/customer.yml --output .generated
python -m setup.preflight --config customer_config/customer.yml --output .generated --profile "<PROFILE>"
python -m setup.bundle validate --strict --target customer --profile "<PROFILE>"
python -m json.tool .generated/summary.json
```

Read the redacted counts in `summary.json`; resource counts represent
dependencies as well as package objects, not newly created Genie Agents.
Inspect `.generated/customer.resources.yml` locally to confirm exact bindings
and grant recipients. Do not post raw runtime configuration or validation
output: it can contain workspace metadata. Keep strict warnings as failures.

**Stop for deployment approval now.** Show the human the redacted summary,
preflight result, strict validation exit/result with sensitive metadata
redacted, selected profile/target confirmation, and proposed schema/table,
App, job, experiment, dashboard and grant changes. Get explicit approval before
the first bootstrap or deployment write. A changed config requires recompiling,
rechecking and renewed approval. Validation is not a live runtime smoke test.

## 6. Bootstrap, deploy and start after approval

The initial bootstrap creates the schema/table before the App trace-table
binding is deployed. The bundle job then grants App traversal and optional
dashboard-reader access after the App principal exists. Both bootstraps are
rerunnable; neither replaces customer tables or functions.

```bash
python -m setup.bootstrap --config .generated/runtime_config.json --profile "<PROFILE>"
python -m setup.bundle deploy --target customer --profile "<PROFILE>"
python -m setup.bundle run --resource router_bootstrap --target customer --profile "<PROFILE>"
python -m setup.bundle run --resource router_app --target customer --profile "<PROFILE>"
databricks bundle summary --target customer --profile "<PROFILE>"
```

Record bundle-managed resource identities and the source revision privately.
Deploying the App resource alone does not start its code; the final App run is
required. Use the summary's App and dashboard links for the following checks.

## 7. Customer smoke checks

Open the App's `/health` through an authenticated workspace session. Expect
HTTP 200, `ready: true`, `status: ready`, a configuration fingerprint and a valid
definitions revision. Delta tracing can report `not_ready` until the first
request writes a trace; MLflow initializes at startup. A 503 or degraded status
requires investigation. Check both trace sinks again after the first request
and require both to be ready before handoff.

Connect an MCP client that supports Databricks Apps OAuth to the App URL's
`/mcp` endpoint. Sign in as an intended App reader and discover `ask_analytics`.
Call it with `{"question": "<APPROVED_TEST_QUESTION>"}`. Use non-sensitive test
questions and record expected outcomes before testing:

| Check | Expected evidence |
| --- | --- |
| One known question per configured Genie domain without a certified alias | `status=answered`, `route=genie`, correct logical `domain` and existing Genie ID |
| One exact governed alias per approved function | `route=uc_function`, correct configured function FQN and known result; skip only if the function list is empty |
| A definition with `certified_function: null` | Genie answer enriched with the matched term and `enrichment_method=exact_alias` |
| An explicitly undefined metric and an unrelated question | `route=fail_loud`, `status=undefined_term` or `no_route`; no invented answer |
| Inspect the resulting trace | Matching `trace_id`, `config_fingerprint`, `defs_commit_sha`, route and latency in Delta and the configured MLflow experiment |
| Open the dashboard as a configured reader | Trace queries succeed with that reader's warehouse/data permissions |
| Update a definition body through reviewed Git changes | After `poll_seconds`, revision changes without App redeployment; function target stays allowlisted |

Live classifier decisions can vary; an unexpected route is a failed smoke
check to investigate. Do not weaken thresholds simply to make a test pass.
Keep a private install record of checks and failures. See
[Operations](OPERATIONS.md) for update, troubleshooting and cleanup. Destroying
the bundle does not remove customer dependencies or the SQL-created trace data.
