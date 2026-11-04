# AI-assisted installation from a public Git URL

Give your coding assistant the public repository clone URL in place of
`<PUBLIC_GIT_URL>`. It needs repository/terminal access and these files, with no
private planning briefs or local skills required. The private definitions
repository is a separate dependency. Never request or print token values.

Copy this starter prompt:

```text
Install this repository in my Databricks workspace using the customer-data path:
<PUBLIC_GIT_URL>

Clone or inspect the public repository, then first read README.md and AGENTS.md.
Read docs/INSTALL_CUSTOMER.md, docs/SECURITY.md and docs/OPERATIONS.md.
Never auto-select a Databricks profile. Ask me for my chosen profile and all
required non-secret configuration values one at a time; wait for each answer.
Never request or print token values, read secret values, or capture secret entry.
Ask me to create/store the private GitHub credential outside this conversation;
collect only secret scope/key names and verify metadata.
Preserve customer resources. Compile first, run preflight with my chosen
profile, then validate the bundle with --strict. Stop before bootstrap or
deployment. Show .generated/summary.json, the redacted strict validation result,
the selected profile/target and proposed resources/grants for my approval.
Require separate explicit approval before any destroy or other destructive action.
After approved deployment, run the documented smoke checks and report evidence.
```

For the demo path, replace `customer-data` with `demo` and the customer guide
with `docs/INSTALL_DEMO.md`. The assistant must prepare/import using that guide,
obtain separate approval for demo DDL/agent creation, insert actual returned
Genie IDs, and use `--demo` in both compile and preflight. A public source URL
does not authorize a private GitHub write or any workspace mutation by itself.

## Deterministic interaction sequence

The assistant reads [AGENTS.md](../AGENTS.md) and the selected installation
guide before taking installation actions. It asks for missing values **one at
a time**, accepts already supplied explicit answers, and waits rather than
guessing. It must never auto-select profiles, even if only one is available.

| Order | Ask / establish |
| --- | --- |
| 1 | Installation mode if not already supplied; then the user's explicit profile choice |
| 2 | Workspace URL only if needed for the user's selected profile's OAuth login; keep identity output private |
| 3 | Customer label, unused App name, existing catalog, dedicated router/demo schema, warehouse ID, existing classifier endpoint, unused experiment path; ask each separately |
| 4 | Private definitions repository URL, branch, relative folder and poll interval; for demo the folder is `demo/definitions` and sample polling is 30 seconds |
| 5 | Secret scope name, then secret key name, then human confirmation that the credential was stored outside chat; verify metadata only |
| 6 | Each existing agent's logical name, description, actual space ID and routing guidance; finish one field at a time and ask whether to add another. Demo instead uses approved imports and returned IDs. |
| 7 | Each approved function's logical name, FQN, domain, description and aliases, or an explicit empty list |
| 8 | Routing confidence/overlap thresholds and certified-function preference; propose documented values individually for acceptance rather than silently choosing |
| 9 | Optional App/dashboard readers, each principal separately, or explicit empty lists |

For demo, also collect the workspace parent folder and approval for the exact
seed/import actions before executing them. The human may run the SQL-editor
seed and secret storage steps directly; ask for completion, never secret input.
Do not push Markdown or change repository access without explicit instruction.

Write only ignored customer configuration (or rendered demo config) for
installation settings. Check local types before compiling. Use exactly the
chosen guide's command order and explicit `--profile "<PROFILE>"` substitution
on every workspace command, including package wrappers. Stop on any failure;
explain the failed gate without printing raw configuration or diagnostics.

## Approval and completion contract

Before bootstrap/deploy, display the compiler's `.generated/summary.json`
counts and mode, preflight result, and the strict validation result/exit code
with workspace URLs, resource IDs and identities redacted. Include the exact
locally selected profile/target for the operator's confirmation, resources to
create, dependencies to bind, grants to apply and retained data on cleanup.
Do not print `.generated/runtime_config.json` or secret environment variables.
If validation cannot run, report it as blocked and do not ask for deployment
approval using an unverified plan. Configuration/mode/profile changes require
fresh checks and approval.

Wait for an explicit approval of that concrete plan before bootstrap/deploy.
Then deploy, run `router_bootstrap` and `router_app`, and execute the selected
guide's health/MCP/trace/dashboard/refresh smoke checks. Report which checks
passed, which require human action and which failed. Never claim that offline
tests or preflight prove live installation success. Provide the private install
record and [operations/cleanup instructions](OPERATIONS.md). Destruction and
demo reseeding require their own explicit approvals.
