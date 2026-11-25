# artifact-replication-check

A CLI that verifies an artifact repository has fully replicated to a second site before (and after) you move a DNS name to it.

> Lab project: a clean-room reference implementation of a pattern I use at work. It contains no employer code or data.

## The problem

A common way to move clients between artifact repository sites is a GeoDNS change: the hostname everyone pulls from starts resolving to the new site. If replication to that site is incomplete, nothing fails at the moment of the change. It fails later, when a cluster in the affected region tries to pull a version the new site does not have yet. In the case that motivated this tool, the new site had versions up to v5 while the original had up to v10. Pulls hung instead of failing quickly. The rollback then failed too, because not every repository was set up for federation in the reverse direction.

What I took from it:

1. Prove replication is complete before the cut-over, per repository and per tag, not from a dashboard total.
2. Check content, not just names. A tag that exists on both sites can still point at different manifests.
3. Put a timeout on everything, so a hung site is reported as an error and does not stall the check.
4. Run the same check in the opposite direction (swap source and target) to test the rollback path.

## Quick start

```bash
python -m pip install -e .            # Python 3.11+
artifact-replication-check -c examples/demo-config.yaml
```

The demo compares two saved inventories in `examples/`, so it runs offline. For real registries, start from `examples/registry-config.yaml`.

```
$ artifact-replication-check -c examples/demo-config.yaml
Source: site-a       examples/source-inventory.json
Target: site-b       examples/target-inventory.json
Digests compared: yes

REPOSITORY              SOURCE  TARGET  MISSING  EXTRA  DIGEST  STATUS
platform/payments-api       10       5        5      0       0  DRIFT
platform/ledger              2       2        0      0       1  DRIFT
tools/base-image             2       2        0      0       0  OK
platform/notifications       1       0        1      0       0  MISSING

Details:
  platform/payments-api: missing on target: v6, v7, v8, v9, v10
  platform/ledger:1.4.1 digest differs: source=sha256:bb22bb22bb22 target=sha256:000000000000
  platform/notifications: repository missing on target (1 tags)

Result: DRIFT FOUND (exit 1)
```

Here a target site does not answer. It is reported as an error after the timeout and does not hang (Docker Registry mode, run against the fake registry from the test suite, `--timeout 2`):

```
Source: site-a       http://127.0.0.1:55842
Target: site-b       http://127.0.0.1:55843
Digests compared: no

REPOSITORY             SOURCE  TARGET  MISSING  EXTRA  DIGEST  STATUS
platform/payments-api      10       0        0      0       0  ERROR

Details:
  platform/payments-api: ERROR site-b: timed out after 2s: GET http://127.0.0.1:55843/v2/platform/payments-api/tags/list

Result: ERRORS (exit 2)
```

## Usage

```
artifact-replication-check -c CONFIG [--json] [--digests] [--fail-on {missing,digest,any}]
                           [--phase {pre,post}] [--timeout SECONDS]
```

| Option | Meaning |
|---|---|
| `--json` | Machine-readable report (full digests, every tag) instead of the table. |
| `--digests` | Compare manifest digests for tags present on both sites. Same as `compare_digests: true`. |
| `--fail-on` | `missing`: exit 1 only for missing tags or repositories. `digest`: only for digest mismatches. `any` (default): also for tags that exist only on the target. |
| `--phase` | `pre` (default): the DNS result is informational. `post`: exit 1 if the DNS name does not point at the target. |
| `--timeout` | Per-request timeout in seconds. Overrides `timeout:` in the config (default 10). |

Exit codes: `0` in sync, `1` drift found, `2` errors (unreachable site, timeout, auth failure, bad config). Errors win over drift, because a partial answer must not look like a clean one.

### Config

```yaml
timeout: 10
source:
  name: site-a
  url: https://registry-a.example.com
  auth: {username_env: REG_A_USER, password_env: REG_A_PASSWORD}
target:
  name: site-b
  url: https://registry-b.example.com
  auth: {token_env: REG_B_TOKEN}
compare_digests: true
repositories: [platform/payments-api, platform/ledger]
dns:
  name: registry.example.com
  expect_addresses: [203.0.113.20, 203.0.113.21]
  # expect_cname: registry-b.example.com
```

Credentials come only from environment variables. The config holds variable names, never secrets. If a variable is not set, the run stops with exit 2.

Each site has a `type`:

- `registry` (default): the Docker Registry HTTP API v2, which Docker Distribution, Harbor, Artifactory, Nexus and most other OCI registries implement.
- `generic`: a JSON listing, either from an `http(s)://` URL or from a local file (the path is relative to the config file). Use it for non-Docker repositories or for inventories exported by another tool:

  ```json
  {"repositories": {"libs/app": ["1.0", "1.1"],
                    "libs/lib": {"2.0": "sha256:..."}}}
  ```

  Tags can be a list, or a `tag -> digest` mapping if you want digests compared.

## How it works

- **Tags**: `GET /v2/<name>/tags/list`, following `Link: <...>; rel="next"` pagination until there are no more pages (with a guard against pagination loops). A `404` means the repository does not exist on that site.
- **Digests** (optional): `HEAD /v2/<name>/manifests/<tag>`, with an `Accept` header that lists OCI index and manifest plus Docker manifest list and v2 manifest types, then a comparison of `Docker-Content-Digest`. Without the full `Accept` list some registries convert the manifest and report a different digest. If a tag is listed but its manifest returns `404`, that is reported as `<missing>`, which is a typical sign of an interrupted replication.
- **Auth**: Basic auth or a static bearer token from env vars. If the registry answers `401` with a `WWW-Authenticate: Bearer realm=...` challenge, the tool fetches a token from the realm once per repository and retries, the same way `docker pull` does.
- **DNS**: `socket.gethostbyname_ex` runs in a worker thread with a timeout, because the stdlib resolver call has none. The name "points to target" when every address returned is in `expect_addresses` and, if `expect_cname` is set, the canonical name matches.
- **Timeouts**: every HTTP request uses the configured timeout. A site that accepts the connection and never answers is reported as `timed out after Ns`.
- Up to 8 repositories are checked in parallel. Each one gets its own result, so one failure does not hide the others.

To test the rollback, swap `source` and `target` and run the check again. Tags that only exist on the new site (`EXTRA`) are the ones you would lose if you rolled back.

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"      # Windows: .venv\Scripts\python
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

The tests run fully offline against a fake Docker registry built on `http.server` that runs in a background thread (see `tests/conftest.py`). They cover pagination, missing tags and repositories, extra tags, digest mismatches and missing manifests, hung and refused connections, the bearer token challenge, generic mode from both file and HTTP, the DNS check, and config errors.

## Limitations

- You list the repositories to compare. There is no `_catalog` discovery, since many registries disable it or limit it to admins.
- Registry auth supports Basic, a static bearer token, and the standard bearer challenge. Cloud-specific credential helpers (ECR, GCR, ACR) are not built in, but you can export a token into `token_env`.
- Digest checks send one `HEAD` per tag on each site. On repositories with thousands of tags, that is a lot of requests, so leave digests off for a quick first pass.
- The DNS check uses the local resolver. With GeoDNS, the answer depends on where you run the check, so run it from the affected region (or a host that uses its resolvers).
- Reads only. It does not trigger or repair replication.
