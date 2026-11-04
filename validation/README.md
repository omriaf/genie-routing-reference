# Verification and publication gates

Local checks need the Python 3.11+ environment from the
[customer guide](../docs/INSTALL_CUSTOMER.md) or
[demo guide](../docs/INSTALL_DEMO.md), including `.[test]`, and Git. They do not
need a Databricks profile, credentials, network, package download or deployment.
Ruff rules are pinned in `pyproject.toml` to avoid machine-specific defaults.

## Offline checks and synthetic compilation

From the repository root, in that existing environment:

```bash
python -m pytest tests/test_documentation.py -v
python -m pytest -v
ruff check .
python scripts/scan_public_repo.py
mkdir -p validation/local
cp customer_config/customer.example.yml validation/local/customer.fixture.yml
python -m setup.compile_config --config validation/local/customer.fixture.yml --output .generated-production
python -m demo.generators.prepare \
  --catalog acme_catalog --schema sample_analytics --warehouse-id abc123 \
  --classifier-endpoint sample-classifier --experiment-path /Shared/sample-router \
  --repository-url https://github.com/acme/business-definitions.git \
  --secret-scope sample-scope --secret-key definitions-token \
  --parent-path /Workspace/Shared/sample-agents --output .generated-demo-setup
python - <<'PY'
from pathlib import Path
import yaml

path = Path('.generated-demo-setup/demo.yml')
document = yaml.safe_load(path.read_text())
# Deliberately synthetic offline IDs. Never preflight or deploy these fixtures.
for index, agent in enumerate(document['genie_agents'], 1):
    agent['space_id'] = f'{index:032x}'
path.write_text(yaml.safe_dump(document))
PY
python -m setup.compile_config --config .generated-demo-setup/demo.yml --output .generated --demo
python scripts/scan_public_repo.py
```

Stop on any nonzero exit. Production output must omit `demo_seed`; demo output
must include it, stage complete App sources and contain a redacted summary.
These fixtures prove compilation only. Real installation still requires actual
returned Genie IDs, user-selected profile, preflight and strict validation.
Both installation modes use bundle target `customer`.

Keep private evidence in ignored `validation/internal-test-results.md` and
private configurations, fixtures and logs in ignored `validation/local/`.
Generated directories are also ignored. Never force-add these paths. A forced
add fails the scanner even if the file is empty. Before committing new public
files, stage them so the scanner includes them; ordinary untracked files are
outside its scope. Scanning before staging alone is insufficient.

## Scanner coverage and documented exceptions

`scripts/scan_public_repo.py` uses only Python's standard library and Git. It
scans tracked working files, staged blobs, every reachable commit/tree/blob on
all local refs, commit authors/committers/messages, ref names and annotated tag
metadata. Deleted historical files still fail. It does not fetch anything or
read credential files. Shallow history, replacement history, symlinks, submodules,
unreadable files and unreviewed binary content fail closed. Findings print only
object IDs or numbered tracked entries, line numbers and rule names; they never
print matched values, filenames or Git error output. Exit codes are 0 for a clean
scan, 1 for findings, and 2 when the scan cannot complete.

Rules detect personal email addresses, local home paths, workspace/App hosts,
internal host/source/transcript markers, literal workspace/resource IDs and
profiles, common Databricks/GitHub/AWS/Slack/JWT credentials, private-key headers,
quoted and unquoted credential/profile assignments in YAML, dotenv, shell,
JSON/TOML-style settings, and private configuration paths. They are heuristic:
new credential formats, arbitrary business prose, encodings, encrypted archives,
unreachable/reflog-only objects, external repos and local Git configuration are
outside the gate. A human still reviews the public diff and reachable history.

For additional **known** non-secret environment values, manually prepare
`validation/local/deny-values.txt` with one literal per line (at least four
characters; no comments). Add workspace IDs, internal profile names or customer
names as needed; do not discover profiles or read credential files to populate
it. The optional command below checks those literals case-insensitively,
including bare occurrences and even otherwise allowed synthetic values:

```bash
python scripts/scan_public_repo.py --deny-file validation/local/deny-values.txt
```

The complete built-in exceptions are deliberately narrow. They never exempt a
file, directory or entire rule, and never waive a token/private-key match:

| Rule | Allowed synthetic values / reason |
| --- | --- |
| Email | Any mailbox on reserved `example.com`, `example.org`, `example.net`; exact invalid-URL fixtures `placeholder@github.com`, `password@github.com`, `git@github.com`, `secret@api.github.com`, `github.com@evil.test` |
| Public attribution | Exact GitHub privacy address `omriaf@users.noreply.github.com`, already used for this public source's commits; this is public author metadata, not an internal identity |
| Profile | `chosen`, `selected`, `test-profile`, `sync-test` in offline CLI doubles (the last is a temporary loopback mock); `NAME`, `NAME.`, `<PROFILE>` and `<your-selected-profile>` in current/historical usage examples. These angle-bracket values are unconfigured examples, not profiles |
| Secret assignment | `TOKEN`, `PLACEHOLDER`, `REJECTED_TEST_PLACEHOLDER`, `synthetic-secret-in-backend-error`, `ENVIRONMENT_CONTENT_MUST_STAY_PRIVATE`, `synthetic-bound-token-DO-NOT-LOG` are inert regression-test sentinels; exact `<PASSWORD>`, `<CLIENT_SECRET>`, `<API_KEY>`, `<TOKEN>` are unconfigured examples |
| Resource ID | `abc123`, `987abc`, `warehouse123`, `offlinewarehouse`, `space-123`; intentionally invalid input fragments `not`, `has`, `01abc`, `g`, `0`, and the repeated-hex source fragment `f` |
| Genie ID | Exact synthetic `01abc000000000000000000000000001`, `01abc000000000000000000000000002`; unconfigured demo markers `REPLACE_RETAIL_SPACE_ID`, `REPLACE_SUPPORT_SPACE_ID`, `REPLACE_MARKETING_SPACE_ID` |

Negative scanner fixtures construct rejected values at runtime instead of
checking credential-shaped strings into source. Extending an exception requires
an exact inert value, a documented reason here, and a regression test proving a
nearby real-looking value still fails.
Unquoted `null`, `None` and `~` are explicit no-value markers. Their quoted forms
remain ordinary literals and fail. There is no wildcard placeholder exemption:
adding private text to an allowed placeholder also fails.

## Live smoke tool: separately gated

Do not run the live command until a human has selected a profile and approved
the installation under [AGENTS.md](../AGENTS.md). The tool requires explicit
`--profile` even if an ambient/default profile exists. Missing/blank/placeholder
profiles fail before any configuration read or client construction. Import and
`--help` do not authenticate. The tool never lists profiles or deploys.

Prepare private `validation/local/smoke.json`. Its `runtime_config` path is
relative to this JSON file, usually `../../.generated/runtime_config.json`.
Use the actual App origin and the reviewed **definitions repository** SHA, not
the routing package SHA. Replace all placeholders. This example shows one Genie
domain; add one `genie` case for every configured domain:

```json
{
  "app_url": "https://<APP_HOST>",
  "runtime_config": "../../.generated/runtime_config.json",
  "definitions_revision": "<40_CHARACTER_DEFINITIONS_SHA>",
  "cases": [
    {"kind": "genie", "question": "<QUESTION_WITHOUT_FUNCTION_MATCH>", "domain": "<DOMAIN>"},
    {"kind": "function", "question": "<CERTIFIED_ALIAS_QUESTION>", "domain": "<DOMAIN>", "function": "<FUNCTION_KEY>", "expected_rows": [["<KNOWN_RESULT>"]]},
    {"kind": "fail_loud", "question": "<OUT_OF_DOMAIN_QUESTION>", "refusal_status": "no_route"},
    {"kind": "git_enriched", "question": "<EXACT_GOVERNED_ALIAS_QUESTION>", "domain": "<DOMAIN>", "definition_term": "<DEFINITION_NAME>"}
  ]
}
```

The enriched example assumes a definition with `certified_function: null` and
no classifier-selected function. If its governed alias selects a function,
also set `function` and `expected_rows`. Function keys must be allowlisted in
the specified domain. With `prefer_certified_functions: true`, require the
function FQN and exact known rows; copy the expected rows' JSON scalar types
from an independently verified result (SQL commonly returns strings). With
`false`, require a Genie answer and no function execution for the same matched
case. An empty function allowlist cannot satisfy this four-scenario gate;
record that limitation separately instead of silently skipping a check.

```bash
python scripts/smoke_test.py --config validation/local/smoke.json --profile "<PROFILE>"
```

The tool uses that profile for the workspace SDK/App authentication and the
MLflow tracking URI. The selected identity must be an intended App reader and
have trace SELECT, warehouse use and experiment read access. The HTTPS App
origin must end in the Databricks Apps domain; redirects and ambient HTTP proxy
settings are disabled. The HTTP/MCP path uses the selected profile's auth
headers; profile authentication must support App invocation. Authentication
failures stop the tool rather than switching profiles or prompting for tokens.
Competing authentication selectors stop before either SDK/MLflow client is
constructed. The central guard follows the documented
[unified-auth environment fields](https://docs.databricks.com/dev-tools/auth/env-vars.html)
and SDK credential suppliers. It rejects **all `DATABRICKS_*` variables**, including
host, profile/config-file selection, workspace/account/cloud IDs, PAT/OAuth/OIDC,
runtime discovery and future fields. It also rejects the `ARM_*`, `AZURE_*`,
`GOOGLE_*`, `GCLOUD_*`, `CLOUDSDK_*`, `AWS_*`, `ACTIONS_ID_TOKEN_*`, `IDENTITY_*`,
`MSI_*` and `DB_MODEL_SERVING_*` namespaces; the SDK's Azure DevOps `SYSTEM_*`
OIDC inputs; model-serving credential-directory/context flags; and legacy
`_OAUTH_TOKEN`/`_OAUTH_TOKEN_EXPIRATION` cache selectors. This intentionally fails
closed across credential namespaces, even if a particular key appears harmless.
Unset those inputs locally before using the selected profile. Only environment
**names** are inspected; values are neither read nor printed, and the guard does
not mutate the environment. Unrelated settings such as `PATH`, `LANG`, `TMPDIR`
and application-specific settings survive unchanged. Tests independently inject
known omitted selectors, test future namespace fields and check every installed
SDK Config environment field/alias without constructing Config or authenticating.

Assertions cover MCP discovery, every configured Genie domain, certified routing
policy and known results, explicit refusal status, exact alias enrichment,
unchanged question, configuration fingerprint, pinned Git revision and unique
trace IDs. Each request must have exactly one Delta row and MLflow analytics
span with matching provenance and latency. Delta's MLflow persistence flag must
be true. Health must stay ready with a valid definitions generation: only the
initial Delta `not_ready` state is allowed; both trace sinks must be ready after
each request. SQL evidence queries are read-only and parameterized, with bounded
polling. Analytics invocations themselves write the normal application traces.
Vendor logging and error text are suppressed; only fixed check names are printed.

Delta readback verifies the dashboard's data source, but **dashboard reader/UI
access and Git polling refresh remain manual live checks**. Open the dashboard
as its intended reader, then follow the reviewed definition-change/refresh check
in the chosen installation guide. Record actual links, redacted evidence,
approvals and unrun checks privately. An automated smoke pass alone is not a
deployment, publication or complete installation approval.

## Local clean-clone rehearsal

Clone the candidate commit from a local filesystem path into a temporary folder
with `git clone --no-hardlinks "<LOCAL_SOURCE_REPOSITORY>" "<TEMPORARY_CLONE>"`.
Do not use a network URL. Verify the expected commit and a clean tracked tree,
and confirm `.git/objects/info/alternates` is absent. Follow README/AGENTS and
the offline commands above from the clone root. Run the scanner again after
fixture generation and confirm generated/config/results paths remain ignored.
Check that `setup`, `app`, `demo.generators.prepare` and the verification scripts
import from the clone, and generated notebook/dashboard paths resolve inside it.

An already provisioned Python environment may be reused without installation;
record its Python and dependency versions and that limitation. Such a rehearsal
proves source portability with those dependencies, not a fresh dependency
download or authenticated installation. No preflight, strict workspace
validation, deployment, public repository creation, push or tag belongs in the
offline rehearsal. Keep evidence in the original checkout's ignored results.
