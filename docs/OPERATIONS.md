# Update, operation, troubleshooting and cleanup

Keep a private installation record: source revision, mode, selected profile,
target `customer`, config location, generated summary, bundle resource links,
definitions revision, approvals and smoke results. Never record token values.
Use [Security](SECURITY.md) for permission and retention boundaries.

## Routine operation

```bash
databricks bundle summary --target customer --profile "<PROFILE>"
databricks apps get "<APP_NAME>" --profile "<PROFILE>"
databricks apps logs "<APP_NAME>" --profile "<PROFILE>"
```

Read logs privately; redact sensitive metadata before sharing. Open `/health`
using authenticated workspace access. `ready: false`/HTTP 503 means startup
has not completed. With `ready: true`, inspect `status`, `definitions` and both
`tracing` sinks: degraded is actionable even when requests still answer.
Delta tracing is `not_ready` until the first request writes a trace. After a
test request require both sinks to be ready. Record the configuration
fingerprint and definitions revision with smoke results.

Use the dashboard for route/domain volumes, latency and failure patterns.
Inspect the configured MLflow experiment and
`<CATALOG>.<ROUTER_SCHEMA>.routing_traces` for a selected `trace_id`. Delta and
MLflow are independent sinks; verify both after a request. Follow approved
data-retention procedures; the package has no automatic retention schedule.

## Update code or configuration

Back up ignored customer/demo YAML to approved private storage. Keep the bundle
state and checkout associated with the same deployment; changing App name,
experiment path, profile or state location is a migration requiring separate
review. Do not deploy another customer's config over an existing target.

Fetch and inspect an explicitly chosen source revision; these commands do not
merge it. Use the branch/tag/commit approved by your organization:

```bash
git status --short
git fetch origin
git diff HEAD "<APPROVED_REVISION>"
```

After reviewing the full change and preserving local edits, check out the
approved revision using your normal Git workflow. Refresh the environment and
run offline verification:

```bash
uv pip install -e '.[test]'
python -m pytest -q
```

Repeat the chosen installation guide from compile through smoke checks:
[customer](INSTALL_CUSTOMER.md) or [demo](INSTALL_DEMO.md). Demo compile **and**
preflight must keep `--demo`; the target is always `customer`. Show the new
redacted summary and strict validation result before renewed deployment
approval. Run initial bootstrap when needed, deploy, rerun `router_bootstrap`
for grants, then run `router_app` to publish/start the refreshed source.
Generated outputs are rebuilt together; never hand-edit them.

A Markdown-only update needs a reviewed commit/push to the configured private
branch and a wait of at least `poll_seconds`. Verify a new `defs_commit_sha`
and expected routing. New functions or domains require YAML changes and
redeployment; Markdown cannot expand executable capabilities. Token rotation
requires an App restart after the human updates the bound secret.

## Rollback

For a definitions regression, revert the offending definitions commit through
the repository's review process and verify the next successful poll. A failed
refresh preserves the last valid snapshot; a restart cannot recover that
in-memory snapshot if the remote branch is still invalid.

For an application/configuration regression, restore the last approved source
revision and matching private config, reinstall dependencies if needed,
recompile, preflight, strictly validate, obtain approval and redeploy. Do not
assume code rollback reverses SQL data or schema changes. An incompatible
trace table requires an explicitly reviewed migration or a separately named
router schema; never drop or rewrite it to bypass preflight.

## Troubleshooting

| Symptom | Check / action |
| --- | --- |
| Compile rejects configuration | Replace every placeholder; quote IDs; check strict field types, duplicate keys, supported identifiers and domain/function references. Never log raw input. |
| Demo compile rejects space IDs | Import agents after seeding; insert returned IDs in the rendered config. Do not compile the checked-in template or invent IDs. |
| Generated resource include is missing | Run the mode's compile first from the repository root; regenerate `.generated/` rather than editing bundle includes. |
| Preflight authentication or CLI failure | Confirm CLI 1.0+ and the explicitly chosen profile through browser OAuth. Do not switch profiles automatically. |
| Delegation authority cannot be verified | Have the resource owner grant the selected installer/group the required management/ownership rights and readable ACL metadata. Runtime usage permissions alone are insufficient. |
| Warehouse/bootstrap error | Use Pro or Serverless; confirm DDL/grant privileges and warehouse availability. Inspect the bootstrap run privately. |
| Existing trace table incompatible | Compare with `sql/bootstrap.sql`; plan an explicit migration. `CREATE TABLE IF NOT EXISTS` does not migrate columns. |
| Strict validation warns or fails | Resolve each warning/error and rerun with `--strict`. Capture a redacted result; never bypass strictness. |
| App exists but is not running | Run `router_bootstrap`, then `router_app`, after approved deployment. Bundle deployment alone does not publish/start App code. |
| Startup 503 / definitions not ready | Check private repository URL, branch, folder, approved token access/expiry, bound secret names, egress and all Markdown cross-references. Never fetch the token for diagnosis. |
| Definitions degraded after refresh | Fix or revert the invalid commit; verify the next poll. The App keeps the last valid generation until a valid replacement. |
| Genie/function fails | Check App bindings, UC traversal, function signature/arguments and Genie execution identity's source-data access; test the dependency in its native UI. |
| Wrong route or refusal | Review configured descriptions, aliases, thresholds and governed definitions; inspect trace evidence. Live classifier results vary. Review policy changes before deployment. |
| MLflow or Delta degraded | Check App experiment `CAN_EDIT`, trace `MODIFY`, traversal, warehouse availability and schema compatibility. Verify both sinks with another approved test request. |
| Dashboard cannot query | Readers need dashboard access, trace `SELECT`, UC traversal and warehouse use. Credentials are not embedded; rerun bootstrap for configured readers. |
| Atomic output replacement fails | Use a local macOS/Linux filesystem supporting atomic directory exchange; failed compilation preserves prior output. |

## Cleanup

Inventory the bundle-managed App, job(s), dashboard and experiment, preserve
authorized trace evidence, then show the selected profile/target and exact
removal scope for **explicit destruction approval**. Do not infer approval
from an earlier deployment. Keep `.databricks/` state until destruction is
complete. After approval, run the CLI directly because the package wrapper
supports only validate/deploy/run:

```bash
databricks bundle summary --target customer --profile "<PROFILE>"
databricks bundle destroy --target customer --profile "<PROFILE>"
```

Review the CLI's destruction plan and confirmation. Never add automatic
approval flags. Bundle destruction does not remove SQL-created
`routing_traces`, its schema, explicit SQL grants, or customer resources:
existing Genie Agents, functions, warehouse, endpoint, secret and repository.
Have owners review residual grants, retained traces, token revocation and
retention before approving any separate removal. Never drop a shared catalog
or schema, or revoke access still used by other applications.

Demo cleanup additionally inventories the separately imported Genie IDs,
`orders`, `support_tickets`, `campaign_daily` and the three sample functions.
The owner removes only those approved demo objects through the workspace UI
or reviewed SQL after confirming no shared dependencies. Even a dedicated
schema may contain the retained trace table. Recompiling without `--demo`
removes the overlay from output; it does not delete seeded data or agents.

After successful teardown and any approved private backup, remove only the
known local generated folders if desired:

```bash
rm -r .generated
rm -r .generated-demo-setup
```

Run each removal only if that folder exists and its contents may be discarded.
The demo folder contains the recorded external IDs. Keep private configuration
or delete it under your organization's retention policy; do not commit it.
