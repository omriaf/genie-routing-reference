# Import the three sample Genie Agents

Use the exact command sequence and approval gates in the canonical
[demo installation guide](../../docs/INSTALL_DEMO.md). Seed the configured
source tables first, then import each domain once and insert its returned
space ID into the matching entry in `.generated-demo-setup/demo.yml`.
The compiler and DAB do not create Genie Agents or discover IDs.

## Generated import requests

| Request file | Domain | Data source |
| --- | --- | --- |
| `retail.create.json` | `retail` | configured catalog/schema, `orders` |
| `support.create.json` | `support` | configured catalog/schema, `support_tickets` |
| `marketing.create.json` | `marketing` | configured catalog/schema, `campaign_daily` |

Each contains `warehouse_id`, `title`, `description`, `parent_path`, and
`serialized_space`. The last field is a **JSON string**, containing a version-2
definition with fully qualified tables, sample questions, text instructions
and example SQL. It is encoded once inside the outer request. Internal
32-character IDs are deterministic content identifiers, unrelated to workspace
Genie IDs. No live workspace ID is supplied by the package.

Inspect all three decoded definitions before importing. Confirm the selected
warehouse, source tables, workspace folder and permissions in the Genie UI,
then run each included sample question. The execution identity needs warehouse
use, UC traversal and source-table read access. The installer must be able to
delegate `CAN_RUN` to the App through recorded management authority.

## Inspect an existing definition

Substitute a returned ID and the profile explicitly chosen by the human:

```bash
databricks genie get-space "<GENIE_SPACE_ID>" --include-serialized-space --profile "<PROFILE>" --output json > .generated-demo-setup/exported-agent.json
jq '.serialized_space | fromjson' .generated-demo-setup/exported-agent.json
```

Keep exports private. When migrating to another catalog/schema or warehouse,
regenerate the import requests with destination values; this updates both
source-table identifiers and example SQL. Obtain setup approval, import into
the destination using its human-selected profile, and retain destination IDs
in that destination's rendered config. Re-running successful create commands
creates duplicate external agents. See [Operations](../../docs/OPERATIONS.md)
for separate cleanup of agents and sample SQL objects.
