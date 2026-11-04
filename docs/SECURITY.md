# Security and resource ownership

Read this before either installation path. The public repository contains
source and synthetic examples. Customer configuration, generated output,
definitions, resource identities and trace data belong in approved private
locations. Git ignore rules reduce accidental commits; they are not an access
control boundary. Review staged changes before committing.

## Identities and delegation

| Identity | Required authority / boundary |
| --- | --- |
| Selected installer | Create/manage bundle objects and execute bootstrap SQL; delegate use of the selected dependencies through readable ACLs |
| App principal | Warehouse `CAN_USE`, endpoint `CAN_QUERY`, configured Genie `CAN_RUN`, approved function `EXECUTE`, trace table `MODIFY`, definitions secret `READ`, experiment `CAN_EDIT` |
| Genie execution identity | Warehouse use and catalog/schema traversal plus `SELECT` on that agent's source tables; verify in the workspace |
| App reader | Optional configured `CAN_USE`; App readers can submit questions executed through the App's granted resources |
| Dashboard reader | Optional configured `CAN_READ`, bootstrap-granted trace-table `SELECT` and UC traversal; warehouse use must also be available |

The bootstrap job explicitly grants the App principal `USE CATALOG` and
`USE SCHEMA` on router/function schemas. Leaf privileges come from App bindings.
Treat App access as access to the configured analytics capabilities; the
router does not add per-question row filtering for each end user. Apply
underlying UC policies and Genie access rules for your deployment model.

Installer preflight fails closed when it cannot prove delegation authority.
It checks warehouse management/ownership, endpoint and Genie management,
catalog/schema usage plus management/ownership, function execution plus
management/ownership, trace-table modification plus management/ownership, and
explicit secret-scope `MANAGE`. Missing schema/table creation rights are
checked when bootstrap is needed. Readable direct identity/group metadata is
required; undocumented or nested-group authority is not assumed.
See [the exact installer inventory](INSTALL_CUSTOMER.md) for details.

Preflight checks installer metadata, not the future App's complete behavior.
It does not fetch a secret, validate GitHub token contents, test every
workspace creation privilege, or prove live model/Genie/trace behavior. Strict
bundle validation and live smoke checks are separate gates.

## Credentials and private GitHub

Use a fine-grained read-only GitHub token restricted to the single private
definitions repository, with `Contents: Read-only`, required metadata read
access, expiry and organization approval where applicable. Do not use a
broad write-enabled credential. Keep token values out of chat, YAML, command
arguments, shell history, logs, generated artifacts and commits. Never request
or print token values through an assistant. The human enters the token through
an approved secret flow; an assistant sees only scope/key names and metadata.

The deployed App manifest maps `GITHUB_DEFINITIONS_TOKEN` to the bound secret.
The runtime uses App-provided Databricks credentials; local profiles are not
uploaded. Never dump environment variables, enable shell tracing for secret
operations or attach raw debug logs. Validation errors are sanitized but
free-text customer inputs can still contain sensitive content: review locally.
The compiler's `summary.json` exposes fixed filenames, counts and mode only.
Other generated files contain resource metadata and are not redacted summaries.

Rotate the GitHub token before expiry: the human replaces the secret under
the same scope/key, restarts the App to refresh its environment, verifies a
successful definitions fetch, then revokes the old token. Changing a scope/key
name requires config compilation, preflight, strict validation, approval and
redeployment. Secret metadata existence alone does not prove a valid token.

## Routing and definition boundaries

Configured UC function keys form an allowlist; scalar arguments are bound
parameters. Unknown function keys are rejected before execution. Governed
Markdown must use valid domains, unique names/aliases, compatible allowlisted
functions or `null`, and a nonempty body. It provides business context rather
than executable SQL. Treat Markdown and model inputs as untrusted content;
review definitions with the same care as routing policy. Strict parsing and
an allowlist do not guarantee correct model interpretation of every question.

The App pins each request to one valid definitions generation. A malformed
refresh retains the last valid snapshot and marks definitions degraded. An
invalid initial fetch prevents readiness. Do not treat degraded health as a
successful installation or silently substitute demo definitions.

## Trace content and retention

Delta and MLflow record question and answer text, reasoning, route/domain,
candidate confidence, latency, errors, Genie/function references, configuration
fingerprint and definitions revision. These may contain customer business
data. Do not submit secrets as questions. Restrict App, dashboard, experiment,
warehouse and trace-table access to approved principals. Dashboard readers
can see operational/business information in traces.

The package does not implement automatic retention or deletion. Agree a
retention period and owner for Delta, MLflow, backups and diagnostic exports
before live use. Apply organization-approved retention jobs and deletion
procedures to both trace sinks. Export only authorized, redacted evidence.
An MLflow/Delta failure marks health degraded even if analytics still answers.

## Cleanup boundaries

Destroying bundle-managed objects removes the router App, job, dashboard and
experiment according to bundle state; archive authorized evidence first.
The SQL-created schema/table and explicit SQL grants can persist. Existing
Genie Agents, functions, warehouse, endpoint, secret and definitions repository
remain customer resources. Demo agents and SQL sample objects were created
separately and also require owner-managed cleanup. Review residual grants,
tokens and retention with their owners. Follow [Operations](OPERATIONS.md)
and obtain explicit approval for destructive actions.
