# Import the three sample Genie Agents

Use the environment variables and generated output from
[the demo setup](../README.md). Complete the `seed.sql` run first: the tables
must exist and be readable when Genie imports the definitions. Use Databricks
CLI 1.0+ with an explicitly selected authenticated profile, and install `jq` to
inspect the responses below. The installer needs permission to create items
under the selected workspace folder and to delegate `CAN_RUN` on the created
agents to the router App principal. Genie also requires an enabled Genie
experience and a supported Pro or Serverless SQL warehouse.

The offline preparation command generates three complete create requests:

| Request file | Domain | Data source |
| --- | --- | --- |
| `retail.create.json` | `retail` | configured catalog/schema, `orders` |
| `support.create.json` | `support` | configured catalog/schema, `support_tickets` |
| `marketing.create.json` | `marketing` | configured catalog/schema, `campaign_daily` |

Each contains `warehouse_id`, `title`, `description`, `parent_path`, and
`serialized_space`. The last field is a **JSON string**, containing a version-2
definition with fully qualified tables, sample questions, text instructions,
and example SQL. It is encoded once inside the outer request. Internal
32-character IDs are deterministic sample question/instruction identifiers;
they are unrelated to workspace Genie IDs. No live workspace ID is supplied by
the package.

Inspect the JSON before importing; for example:

```bash
jq '.serialized_space | fromjson' "$DEMO_OUTPUT/retail.create.json"
databricks workspace mkdirs "$DEMO_PARENT_PATH" --profile "$DEMO_PROFILE"

databricks genie create-space --json @"$DEMO_OUTPUT/retail.create.json" \
  --profile "$DEMO_PROFILE" --output json > "$DEMO_OUTPUT/retail.created.json"
databricks genie create-space --json @"$DEMO_OUTPUT/support.create.json" \
  --profile "$DEMO_PROFILE" --output json > "$DEMO_OUTPUT/support.created.json"
databricks genie create-space --json @"$DEMO_OUTPUT/marketing.create.json" \
  --profile "$DEMO_PROFILE" --output json > "$DEMO_OUTPUT/marketing.created.json"

jq -r '.space_id' "$DEMO_OUTPUT/retail.created.json"
jq -r '.space_id' "$DEMO_OUTPUT/support.created.json"
jq -r '.space_id' "$DEMO_OUTPUT/marketing.created.json"
```

Check every command succeeded and each ID is a 32-character hexadecimal value.
Edit `$DEMO_OUTPUT/demo.yml`: replace `REPLACE_RETAIL_SPACE_ID`,
`REPLACE_SUPPORT_SPACE_ID`, and `REPLACE_MARKETING_SPACE_ID` with the respective
returned IDs, keeping them quoted as YAML strings. This is the installer-edited
copy rendered from `demo/config/demo.yml`. No compiler or bundle operation
creates these agents or discovers their IDs. Re-running `create-space` creates
additional agents, so keep the returned IDs for reuse.

Verify each imported agent in the workspace Genie UI and run its included
sample question. Confirm the selected warehouse, source table, and permissions.
Then continue with `--demo` compilation and preflight in the demo README.

To reuse an existing sample agent, export its definition for inspection with
the selected profile (substitute one of your returned IDs):

```bash
DEMO_SPACE_ID='<returned-agent-id>'
databricks genie get-space "$DEMO_SPACE_ID" --include-serialized-space \
  --profile "$DEMO_PROFILE" --output json \
  | jq '.serialized_space | fromjson' > "$DEMO_OUTPUT/exported-agent.json"
```

When migrating to another catalog/schema or warehouse, rerun offline preparation
with the new settings and import those newly generated `*.create.json` requests
into the destination workspace using its explicitly chosen profile. Preserve
the destination IDs in that destination's rendered config. Catalog/schema
references occur in both table identifiers and SQL; regenerating the requests
updates both. Agents and their permissions remain installer-owned external
dependencies throughout this flow.
