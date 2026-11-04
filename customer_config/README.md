# Customer configuration

`customer_config/` is the only customer-editable area. Copy
`customer.example.yml` to `customer.yml` here, then replace every placeholder
with your resource references. `customer.yml` is ignored by Git. The example's
`acme_catalog`, `abc123`, and `01abc…` identifiers are synthetic and do not
provide access to resources.

Use Python 3.11 or newer. The contract is available as
`setup.models.RouterPackageConfig.model_validate(mapping)` or
`setup.models.load_customer_config(pathlib.Path("customer_config/customer.yml"))`.
Loading validates local YAML only; it does not connect to Databricks or GitHub,
select credentials, or check whether referenced resources exist.

All sections below are required except `permissions`. Unknown fields are
rejected in every section and list entry, and duplicate YAML mapping keys are
rejected. Values have strict types: quote resource IDs, use YAML booleans for
flags, and use numbers for thresholds and polling intervals. Required text
cannot be blank.

## Customer and workspace

| Field | Meaning and validation |
| --- | --- |
| `customer.name` | Customer label, such as `acme`. |
| `customer.app_name` | Name for the router App: 1–63 lowercase letters, digits, or hyphens; starts with a letter and ends with a letter or digit. |
| `workspace.catalog` | Existing Unity Catalog catalog. Also the only allowed catalog for configured functions. |
| `workspace.router_schema` | Router-owned schema name to be initialized by the package in a later deployment step. |
| `workspace.warehouse_id` | Existing SQL warehouse ID, supplied as a nonempty alphanumeric string of at most 128 characters. |
| `workspace.classifier_endpoint` | Existing serving endpoint name; letters, digits, `_`, `.`, and `-`, up to 255 characters. |
| `workspace.mlflow_experiment` | Absolute Databricks workspace experiment path, for example `/Shared/acme-analytics-router`; no empty components or traversal. This is a workspace path, not a local filesystem path. |

Catalog, schema, and each component of a function FQN use the supported safe
identifier subset: a letter or underscore followed by letters, digits, or
underscores, up to 255 characters per component. Quoted identifiers, hyphens,
spaces, SQL expressions, and wildcard names are unsupported. Names are
preserved exactly; functions must use the same catalog spelling as
`workspace.catalog`.

## GitHub Markdown definitions

| Field | Meaning and validation |
| --- | --- |
| `definitions.repository_url` | HTTPS `github.com/owner/repository` URL, optionally ending in `.git`. Credentials, query strings, fragments, ports, and subfolder URLs are rejected. Reference your private definitions repository. |
| `definitions.branch` | Nonblank branch name to poll. |
| `definitions.markdown_path` | Repository-relative Markdown folder, such as `definitions` or `definitions/metrics`; `.` means repository root. Absolute paths, empty components, backslashes, and traversal are rejected. |
| `definitions.secret_scope` | Name of an existing Databricks secret scope. |
| `definitions.secret_key` | Key of the existing secret containing the read-only GitHub token. This is the key name, never the token value. |
| `definitions.poll_seconds` | Positive integer refresh interval in seconds; for example `90`. |

Secret scope and key identifiers allow letters, digits, `_`, `-`, `.`, and `@`,
up to 128 characters. Create the secret separately using your organization's
credential process. The runtime will use a resource binding to access the
secret; this configuration loader never retrieves it. Use a read-only GitHub
credential restricted to the definitions repository.

Never put tokens, passwords, private keys, or other secret values in YAML,
examples, tests, logs, generated files, or commits. Fields such as `token`,
`password`, `secret_value`, and `github_token` are rejected, including in nested
entries. String validation errors hide input values; callers must also avoid
logging raw input or `ValidationError.errors()` input payloads. Validation
cannot determine whether arbitrary free-text content contains a secret.

## Routing policy

| Field | Meaning and validation |
| --- | --- |
| `routing.confidence_threshold` | Minimum classification confidence, a finite number from `0` through `1`, inclusive. Example: `0.75`. |
| `routing.overlap_threshold` | Overlap threshold for enrichment, a finite number from `0` through `1`, inclusive. Example: `0.70`. |
| `routing.prefer_certified_functions` | Boolean preference for a configured certified function when policy allows it. Example: `true`. |

## Existing Genie Agents

`genie_agents` is a list with at least one entry. Add as many existing agents as
your routing domains require; no business domains are hardcoded.

| Entry field | Meaning and validation |
| --- | --- |
| `name` | Unique logical domain: a lowercase letter followed by lowercase letters, digits, or underscores, up to 63 characters. |
| `description` | Business purpose used to describe the domain. |
| `space_id` | Existing Genie space ID: exactly 32 hexadecimal characters. The example's `01abc…` values are placeholders; the ellipsis is not valid in a real ID. |
| `routing_guidance` | Instructions describing which questions belong to this agent. |

An agent's `name` defines its domain. Duplicate names therefore also create
duplicate domains and are rejected. The package references customer Genie
Agents; it does not create or alter them.

## Existing approved Unity Catalog functions

`uc_functions` is a variable-length list of existing approved functions. Use
`[]` for Genie-only routing. Multiple functions may belong to one domain.

| Entry field | Meaning and validation |
| --- | --- |
| `name` | Unique logical metric name using the same syntax as an agent name. Markdown definitions refer to this key. |
| `full_name` | Exact `catalog.schema.function` identifier with three components, using `workspace.catalog`. No SQL fragments or wildcards. |
| `domain` | Name of an entry in `genie_agents`. Unknown domains are rejected. |
| `description` | Business description of the certified metric. |
| `aliases` | Optional list of nonblank matching phrases; defaults to `[]`. |

These references form the allowlist for later routing. The package does not
create or alter customer functions. Data and required permissions must already
exist; deployment preflight will check access in a later task.

## Optional access

`permissions.app` and `permissions.dashboard` are lists of existing Databricks
principals receiving access to the respective resource during deployment.
Both default to empty lists. Each entry must specify exactly one nonblank
`group_name` or `user_name`, for example:

```yaml
permissions:
  app:
    - group_name: analytics-users
  dashboard:
    - user_name: analyst@example.com
```

The contract records access recipients. Resource-specific access levels are
owned by the package's deployment resources, rather than editable YAML fields.

## Profiles, targets, and generated output

Pass the deployment target and `--profile <name>` explicitly at command time
when using future deployment commands. Profiles, hosts, and credentials are
not configuration fields, and the package never auto-selects a profile.
Do not modify application, setup, or resource code for customer settings.
Generated artifacts belong under the ignored `.generated/` directory.

Compile with `python -m setup.compile_config --config customer_config/customer.yml
--output .generated`. The root `databricks.yml` directly includes
`.generated/customer.resources.yml`. Only the root bundle file may contain a
bundle-file `include` section. Generated and static resource files contain a
`resources` mapping, and the generated fragment also declares top-level `sync`.

The compiler also stages the deployable App at `.generated/app_source/` and
points the generated App resource there. It includes the runtime packages,
root `app.yaml`, `requirements.txt`, and the validated `runtime_config.json`.
Customer YAML, tests, Git metadata, and unrelated source files are excluded.
Configuration and App source are validated and published together in one
atomic directory replacement; a failed compile preserves the previous output.

Demo mode is opt-in with `--demo`. It reads packaged definitions from
`demo/resources/*.yml` and `*.yaml`, validates their resource-only shape, and
merges them directly into the same generated document. Demo files cannot
contain nested includes or duplicate/override customer resource declarations.
Local notebook, Python, SQL-file, library, dbt, app, and dashboard source paths
are rebased from each definition file to the generated document; absolute,
remote, substitution-based, and Git source paths are preserved. Production
compilation does not read demo definitions. Demo mode fails without replacing
existing output if definitions are missing or invalid.

The generated fragment also declares top-level `sync` settings. Production
compilation excludes the entire `demo/` tree from DAB synchronization and
upload. With `--demo`, it includes that tree so the demo notebook, its Python
helper, and SQL definitions are available together. Patterns are relative to
the generated fragment, and the root bundle's App-source inclusion and other
exclusions remain in effect. `sync.include` adds files to the default upload
set; it does not restrict that set. Resource input files remain resource-only,
and the generated fragment has no bundle-file `include` or nested includes.
