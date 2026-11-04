# Assistant installation contract

This public repository is installable using only `<PUBLIC_GIT_URL>` and its
checked-in documentation. For installation requests, first read `README.md`,
`docs/AI_ASSISTED_INSTALL.md`, `docs/SECURITY.md`, `docs/OPERATIONS.md`, the chosen
`docs/INSTALL_CUSTOMER.md` or `docs/INSTALL_DEMO.md`, and
`customer_config/README.md`. Do not depend on private briefs or machine-local
skills. The gates below govern workspace installation/teardown; routine code
or documentation development can run offline tests without deployment approval.

- Ask for missing non-secret configuration values **one at a time** and wait
  for each answer. Reuse explicit answers already given in this session.
- **Never auto-select a Databricks profile.** Ask the user to choose it. Do not
  infer it from the environment, defaults or the number of profiles. Supply
  the chosen value explicitly as `--profile "<PROFILE>"` for every Databricks
  CLI command and `setup.preflight`, `setup.bootstrap`, `setup.bundle` command.
  Bundle commands also use `--target customer`. Do not commit a host/profile.
- **Never request or print token values** or other secrets. Never retrieve a
  secret value, dump credentials/environment variables, capture interactive
  secret entry or put secrets in arguments, YAML, generated output or logs.
  Have the human create/store the private read-only GitHub token outside chat;
  collect scope/key names only and inspect metadata only. Do not execute the
  human-only interactive secret-storage command on the user's behalf.
- Preserve **customer resources**: existing Genie Agents, UC functions/data,
  warehouse, serving endpoint, secret and private repository. Do not modify
  source/resource code to insert customer settings or invent missing IDs.
  Use ignored `customer_config/customer.yml` or `.generated-demo-setup/demo.yml`.
  No private GitHub push or access change is authorized by cloning the public URL.
- Compile first, then run preflight with the selected profile, then bundle
  validation with `--strict`. Demo compile and preflight both require `--demo`;
  the selected bundle target remains `customer`. Stop on failure. Generated
  output is rebuilt, not hand-edited. Preflight checks installer delegation
  authority and secret metadata, not token contents or complete App behavior.
- Before any bootstrap/deploy write, show `.generated/summary.json`, mode,
  proposed created resources/bindings/grants, selected profile/target, preflight
  result and strict validation result/exit code with sensitive metadata
  redacted. Never print raw runtime config. Require explicit deployment
  **approval** of that concrete validated plan and wait. Changed configuration,
  source revision, mode or profile requires revalidation and renewed approval.
- Demo preparation is offline. Obtain separate approval for the exact
  synthetic DDL replacements and external Genie creations; seed before import,
  preserve responses and insert actual returned IDs in the rendered config.
  Never retry successful imports or reseed without explicit approval.
- After approved bootstrap/deploy, run `router_bootstrap`, then `router_app`.
  Verify authenticated `/health`, MCP `ask_analytics`, every configured route,
  refusals, Delta/MLflow traces, dashboard access and definitions refresh using
  the selected guide. Report actual evidence and unrun checks separately.
- Require separate explicit approval for `destroy`, deletion, revocation or
  other destructive actions. Show the exact target and deletion scope. Keep
  bundle state until teardown completes. Explain retained SQL-created traces,
  grants, customer resources and separate demo objects. Never use automatic
  approval flags to bypass a human gate.

For repository changes, add focused tests when required and run
`python -m pytest tests/test_documentation.py -v` and `python -m pytest -q` in
the documented environment. Do not claim live workspace verification from
offline tests. Keep real hosts, IDs, emails, profile names, customer names and
secrets out of checked-in installation examples.
