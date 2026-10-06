# icefloor

A terminal UI for reading **Iceberg table metadata** and **Parquet file metadata** —
both in one place, with the link between them walkable: pick a data file out of an
Iceberg snapshot and drop straight into that file's row groups, encodings and column
statistics.

## Install

Needs Python 3.12+. With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install icefloor          # puts `icefloor` on your PATH
uvx icefloor path/to/table        # or run once without installing
```

`pipx install icefloor` works too. To install the latest unreleased code:

```bash
uv tool install git+https://github.com/gkoenig/icefloor
```

Local files need nothing more. To load tables through a PyIceberg catalog (`-c`), add
the extra for your backend:

| Extra | For |
| --- | --- |
| `sql` | SQL catalog backed by SQLite |
| `s3` | tables on S3 (`s3fs`) |
| `glue` | AWS Glue catalog |
| `azure` | tables on Azure Blob / ADLS Gen2 (`adlfs`), e.g. Databricks Unity Catalog |

```bash
uv tool install 'icefloor[s3,glue]'
```

REST catalogs need no extra beyond the storage one. Catalogs are configured the usual
PyIceberg way, in `~/.pyiceberg.yaml` or `PYICEBERG_*` environment variables. See
[Databricks Unity Catalog](#databricks-unity-catalog) for a worked example.

## How to use it

```bash
icefloor path/to/table
icefloor path/to/file.parquet
icefloor path/to/parts/
icefloor db.events -c prod        # table from a configured PyIceberg catalog
```

`icefloor` auto-detects what you passed in:

- a `*.parquet` file
- a directory containing Parquet files
- an Iceberg table directory that contains a `metadata/` folder
- a `*.metadata.json` file
- a catalog table name when used with `-c/--catalog`

Useful options:

```bash
icefloor --list-sections path/to/table
icefloor -s snapshots path/to/table
icefloor path/to/table -c prod
```

`--list-sections` prints the available section names and exits. `-s` jumps directly to a
particular section. Once the UI opens, use the keyboard controls shown below to browse,
filter and drill down into the data.

## Databricks Unity Catalog

icefloor can browse Iceberg tables registered in Databricks Unity Catalog (UC). It talks
to the **Iceberg REST catalog that Databricks hosts inside every workspace**, so there's no
server to deploy:

```
https://<workspace-host>/api/2.1/unity-catalog/iceberg-rest
```

A UC table name has three parts, `catalog.schema.table`. icefloor splits it like this:

| UC name part | Where it goes |
| --- | --- |
| `catalog` (e.g. `it_dev`) | `warehouse` in the catalog config |
| `schema.table` (e.g. `dev_geko.basket_ice`) | the icefloor target |

### How access works

Two separate connections are involved:

1. **icefloor → workspace (catalog).** icefloor authenticates with a Databricks token and
   asks UC for the table. UC answers with the location of the table's `metadata.json`
   and a **short-lived SAS token** scoped to the table's storage path. This is called
   *credential vending*.
2. **icefloor → storage account (data).** Using that SAS token, icefloor reads
   `metadata.json`, the manifest lists, the manifests and Parquet footers directly from
   ADLS. It never reads Parquet data pages.

So you need a Databricks identity with the right UC grants. You do **not** need an Azure
role, account key or SAS of your own on the storage account.

### Prerequisites

Work through these once. The first three usually need a workspace or metastore admin.

**1. External data access is enabled on the metastore.**
Without it, UC refuses to vend credentials to any engine outside Databricks. A metastore
admin turns it on in Catalog Explorer → ⚙ → Metastore → *External data access*.

**2. Your identity has the grants.**
Use the identity whose token icefloor will send: your user, or a service principal.

```sql
GRANT USE CATALOG ON CATALOG it_dev TO `you@company.com`;
GRANT USE SCHEMA, SELECT, EXTERNAL USE SCHEMA ON SCHEMA it_dev.dev_geko TO `you@company.com`;
```

`EXTERNAL USE SCHEMA` is the one that's easy to miss. It is separate from `SELECT`, it is
**not** included in `ALL PRIVILEGES`, and without it the table may be listed but not
loaded (HTTP 403). Check what you have:

```sql
SHOW GRANTS `you@company.com` ON SCHEMA it_dev.dev_geko;
```

**3. The table is readable as Iceberg.**
It must be one of these:

- a **managed Iceberg table** (`CREATE TABLE … USING ICEBERG`), or
- a **Delta table with UniForm**:
  `'delta.universalFormat.enabledFormats' = 'iceberg'`. UniForm writes the Iceberg
  metadata asynchronously after each Delta commit, so it can lag the newest commit
  briefly.

Plain Delta tables are invisible to the Iceberg endpoint. Check with:

```sql
DESCRIBE TABLE EXTENDED it_dev.dev_geko.basket_ice;   -- look at Provider / Table Properties
```

**4. Your machine can reach both endpoints over HTTPS (443).**

- the workspace host, e.g. `adb-1234567890123456.7.azuredatabricks.net`: blocked by
  workspace **IP access lists** if your IP isn't allowed;
- the storage account, `<account>.dfs.core.windows.net`: blocked if the account accepts
  only **private endpoints** or selected networks.

If only the workspace is reachable, the table loads but every section shows an error.
On a VPN, check that private DNS zones also resolve inside WSL2 or containers
(`nslookup <account>.dfs.core.windows.net`).

**5. icefloor is installed with the Azure extra.**

```bash
uv tool install 'icefloor[azure]'      # or, in a clone:  uv sync --extra azure
```

The extra brings in `adlfs`. That's the only Azure filesystem that understands the SAS
tokens UC vends. Without it icefloor stops with `adlfs is not installed`.

### Choose how to authenticate

Every option ends with icefloor sending a bearer token to the workspace. Pick one.

| Option | Good for | Token lifetime |
| --- | --- | --- |
| A. Databricks CLI login (OAuth, your user) | interactive use on your laptop | ~1h, refreshed by the CLI |
| B. Personal access token (PAT) | quick tests, workspaces without OAuth | what you set, up to the admin's limit |
| C. Service principal (OAuth M2M) | automation, shared machines | fetched automatically per run |

All options use the same catalog entry in `~/.pyiceberg.yaml`. Credentials are best
passed as environment variables, where each config key maps to
`PYICEBERG_CATALOG__<NAME>__<KEY>`:

```yaml
catalog:
  uc:
    type: rest
    uri: https://adb-<<your-number>>.azuredatabricks.net/api/2.1/unity-catalog/iceberg-rest
    warehouse: <<your-UC-catalog>>
```

One entry covers one UC catalog. Add another entry (e.g. `uc_prod`) per catalog you browse.

#### A. Databricks CLI login (recommended for people)

`databricks auth login` runs a browser OAuth login. It saves the workspace as a profile in
`~/.databrickscfg` and caches tokens in `~/.databricks/token-cache.json`. PyIceberg can't
read that cache, but `databricks auth token` prints a fresh access token from it,
refreshing it if needed. Hand that token to icefloor:

```bash
# once per machine (and again when the refresh token expires):
databricks auth login --host https://adb-<<your-number>>.azuredatabricks.net --profile dev

# each time:
export PYICEBERG_CATALOG__UC__TOKEN=$(databricks auth token --profile dev | jq -r .access_token)
icefloor dev_geko.basket_ice -c uc
```

To keep nothing workspace-specific in `~/.pyiceberg.yaml` at all, put this function in
your `~/.bashrc` or `~/.zshrc`. It takes the host from the CLI profile and builds the
catalog config from environment variables, for that one process only:

```bash
# usage: icefloor-uc <cli-profile> <uc-catalog> <schema.table> [icefloor options]
icefloor-uc() {
  local profile=$1 uc_catalog=$2; shift 2
  local host token
  host=$(databricks auth describe --profile "$profile" -o json | jq -r '.details.host // empty')
  token=$(databricks auth token --profile "$profile" | jq -r '.access_token // empty')
  if [[ -z $host || -z $token ]]; then
    echo "icefloor-uc: no host/token for profile '$profile' - run: databricks auth login --profile $profile" >&2
    return 1
  fi
  PYICEBERG_CATALOG__UC__TYPE=rest \
  PYICEBERG_CATALOG__UC__URI="${host%/}/api/2.1/unity-catalog/iceberg-rest" \
  PYICEBERG_CATALOG__UC__WAREHOUSE="$uc_catalog" \
  PYICEBERG_CATALOG__UC__TOKEN="$token" \
    icefloor -c uc "$@"
}

icefloor-uc databricks-profilename catalog schema.table
icefloor-uc databricks-profilename catalog schema.table --list-sections
```

The token exists only in that icefloor process's environment. It isn't written to a
file and doesn't appear in shell history. The function needs `jq`. Without it, swap each
`jq -r <path>` for a small `python -c 'import json,sys; …'` one-liner.

#### B. Personal access token

Create one in the workspace under *your avatar → Settings → Developer → Access tokens*.
Then:

```bash
export PYICEBERG_CATALOG__UC__TOKEN=dapi...
icefloor dev_geko.basket_ice -c uc
```

PATs can live for weeks. Prefer A or C if your workspace allows it, and never commit one
in `~/.pyiceberg.yaml` dotfiles.

#### C. Service principal (OAuth machine-to-machine)

Create an OAuth secret for the service principal (account console → Service principals
→ *Secrets*), grant the SP the privileges from prerequisite 2, then:

```bash
export PYICEBERG_CATALOG__UC__CREDENTIAL='<client-id>:<client-secret>'
export PYICEBERG_CATALOG__UC__OAUTH2_SERVER_URI=https://adb-1234567890123456.7.azuredatabricks.net/oidc/v1/token
export PYICEBERG_CATALOG__UC__SCOPE=all-apis
icefloor dev_geko.basket_ice -c uc
```

PyIceberg exchanges the client credentials for a token on every start.

### Verify step by step

If something fails, these `curl` calls show which layer is broken. They reuse the token
from above:

```bash
HOST=https://adb-1234567890123456.7.azuredatabricks.net
AUTH="Authorization: Bearer $PYICEBERG_CATALOG__UC__TOKEN"

# 1. endpoint + token: JSON with "defaults"/"overrides" means OK
curl -s -H "$AUTH" "$HOST/api/2.1/unity-catalog/iceberg-rest/v1/config?warehouse=it_dev"

# 2. grants + table type: basket_ice must be listed
curl -s -H "$AUTH" "$HOST/api/2.1/unity-catalog/iceberg-rest/v1/catalogs/it_dev/namespaces/dev_geko/tables"
```

If call 2 returns 404, take the `prefix` value from call 1's `overrides` and use it in
place of `catalogs/it_dev`. Then, from icefloor:

```bash
icefloor dev_geko.basket_ice -c uc --list-sections   # catalog side OK, no UI
icefloor dev_geko.basket_ice -c uc -s manifests      # storage side OK if this shows rows
```

| Result | Layer | Fix |
| --- | --- | --- |
| curl times out | network to workspace | VPN, proxy, workspace IP access list |
| HTTP 401 / `rejected the credentials` | token | expired or wrong token; run `databricks auth login` again; for C check `oauth2-server-uri` and `scope` |
| HTTP 403 / `denied access` | UC permissions | `EXTERNAL USE SCHEMA` missing, or external data access off (prerequisites 1–2) |
| `not found in catalog` | naming / table type | target is `schema.table`, catalog goes in `warehouse`; table may be Delta without UniForm |
| `adlfs is not installed` | install | `uv tool install 'icefloor[azure]'` |
| `--list-sections` works, every section shows an error | network to storage | storage firewall / private endpoint, DNS for `*.dfs.core.windows.net` |
| sections start failing after ~1h in one session | vended SAS expired | quit and restart icefloor |

### Limitations

- **Read-only.** icefloor never writes to the table or the catalog.
- **Credentials are fetched once per start.** Neither the Databricks token nor the
  vended SAS token is refreshed while icefloor runs. Restart for a fresh pair.
- **Azure only, as tested.** UC on AWS or GCP vends S3 or GCS credentials instead. That
  should work with the `s3` extra (or `gcsfs`) but hasn't been tried.
- **No live CI.** The Unity Catalog path is covered by offline tests of the error
  handling only. Real workspaces may differ in details such as the REST `prefix`.

## What it shows

**Iceberg** — table identity and format version, the current and historical schemas,
partition specs and sort orders, every snapshot with its summary, refs, the manifest
list, data and delete files, partition-level record/file/size statistics, the metadata
log, and table properties.

**Parquet** — file version and writer, the Parquet and Arrow schemas with definition and
repetition levels, per-row-group sizes and sort order, every column chunk with its codec
and encodings, min/max/null/distinct statistics, footer key-value metadata, and a
per-column footprint showing where the bytes actually went.

Numbers that benefit from it get an inline bar, scaled to the largest value on screen,
so filtering re-scales the comparison. Series over snapshots or row groups also get a
sparkline.

## Keys

| Key | Action |
| --- | --- |
| `↑` `↓` | move within the focused pane |
| `tab` | next pane |
| `enter` | open the highlighted Parquet file |
| `/` | filter the visible rows |
| `esc` | clear the filter, or go back a level |
| `s` / `S` | scope the file views to the highlighted snapshot / back to current |
| `r` | re-read the current section from storage |
| `y` | copy the highlighted row's key |
| `?` | help |
| `t` | light / dark |
| `q` | quit |

Sections load on a background thread, so a table with thousands of manifests stays
responsive and a section that fails to read reports the error in place.

## Sample data

From a clone of the repo:

```bash
uv run python tests/fixture.py fixtures
uv run icefloor fixtures/warehouse/sales/events
```

Builds a partitioned Iceberg table (five snapshots, a schema evolution, an overwrite)
plus loose Parquet files to poke at.

## Development

```bash
git clone https://github.com/gkoenig/icefloor && cd icefloor
uv sync
uv run icefloor path/to/table
uv run pytest -q
```

See `CLAUDE.md` for the architecture.

### Releasing

Bump `version` in `pyproject.toml`, commit, then tag and push:

```bash
git tag v0.1.0 && git push origin v0.1.0
```

The `release` workflow builds, tests and publishes the tag to PyPI. It uses trusted
publishing, so no token is stored in the repo.

## License

MIT
