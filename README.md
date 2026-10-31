# ALCF-Dashboard
ALCF dashboard to monitor usage, projects, and quota

A static [GitHub Pages](https://pages.github.com/) dashboard showing your jobs,
projects/allocations and storage quotas on the ALCF machines **Aurora**,
**Polaris** and **Crux**.

```
collector/alcf_dashboard.py  ──►  docs/data/dashboard.json  ──►  docs/ (GitHub Pages)
   ├─ ALCF IRI API (preferred)
   └─ SSH to aurora/polaris/crux
```

GitHub Pages only serves static files and cannot reach ALCF itself, so a small
collector (Python 3.6+, standard library only, so it also runs with the login
nodes' `/usr/bin/python3`) writes a JSON snapshot that the page renders. If
`docs/data/dashboard.json` does not exist, the page shows
`docs/data/sample.json` with a "sample data" banner.

## What is collected

| Data | IRI API ([docs](https://docs.alcf.anl.gov/services/iri-api/)) | SSH |
|---|---|---|
| System status & incidents | `GET /status/resources`, `GET /status/incidents` (public, no token needed) | – |
| Jobs | `POST /compute/status/{resource}`, filtered to your jobs | `qselect -u $USER` + `qstat -f -F json` |
| Projects & allocations (node-hours) | `GET /account/projects`, `.../project_allocations`, `.../user_allocations` | – |
| Storage quotas | storage (bytes/inodes) project allocations | `myquota`, `myprojectquotas` |

Every source is optional and failures are isolated: when a source fails (an
expired token, an SSH host without an open connection, ...) the dashboard keeps
its last good data and says so in a banner and in the *Data sources* table.
Jobs reported by both the IRI API and `qstat` are merged into a single row.

## Usage

### 1. IRI API token

Get a token with [`alcf-tokens`](https://github.com/argonne-lcf/alcf-tokens)
(needs Python 3.10+). Install it in its own environment: a `pip install --user`
into the system Python can pick up incompatible dependencies.

```bash
uv tool install alcf-tokens    # or: pipx install alcf-tokens
alcf-tokens login iri
export ALCF_IRI_TOKEN=$(alcf-tokens get-token iri)
```

If `ALCF_IRI_TOKEN` is not set but `alcf-tokens` is on your `PATH`, the
collector calls `alcf-tokens get-token iri` itself, which also renews the token
(run `alcf-tokens login iri` again after 7 days); `--token-command` overrides
this, e.g. `--token-command "uvx alcf-tokens get-token iri"`. If the command
fails, the dashboard says so and keeps the previous project and job data.
Without a token only the public system status is collected. The API base URL
defaults to `https://api.alcf.anl.gov/api/v2`; change it with `--iri-url` or
`ALCF_IRI_URL`.

The API allows 5 requests per second per user; the collector stays below that
and retries when it is rate limited, so with many projects a run takes about
half a minute. Jobs are listed for your ALCF login name, which comes from SSH,
`--user` (or `ALCF_USER`) or the previous snapshot: the API's `whoami` returns
an identity UUID instead.

### 2. SSH (optional)

ALCF logins require MFA, so the collector uses `ssh -o BatchMode=yes` and never
prompts. Open a shared connection first, e.g. in `~/.ssh/config`:

```
Host aurora.alcf.anl.gov polaris.alcf.anl.gov crux.alcf.anl.gov
    User <your-alcf-username>
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 12h
```

then authenticate once per machine with `ssh -fN aurora.alcf.anl.gov` (etc.).
`ControlPersist` has to outlast the time between updates. If your SSH config
uses short aliases (`Host aurora`), pass `--ssh-host aurora=aurora` (etc.) so
that their connections are reused. Use `--ssh-user` to set the user,
`--machines aurora,polaris` to limit machines and `--no-ssh` to disable SSH
entirely.

### 3. Collect and publish

```bash
scripts/update_dashboard.sh                # writes docs/data/dashboard.json
python3 -m http.server -d docs 8000        # preview at http://localhost:8000
scripts/update_dashboard.sh --push         # collect and publish
```

Collector options go after `--`, e.g.
`scripts/update_dashboard.sh --push -- --ssh-host aurora=aurora --machines aurora,crux`.
Each run is merged with the previous snapshot, so a source that fails keeps its
last good data. `--push` force-pushes the snapshot as the only commit of the
`dashboard-data` branch, so history does not grow and your checkout is never
touched. If `gh` is installed and logged in, it then starts the deploy
workflow; otherwise the next scheduled run deploys the snapshot.

To update periodically, run it from cron, e.g. every 30 minutes:

```
PATH=/home/<you>/.local/bin:/usr/local/bin:/usr/bin:/bin
*/30 * * * * /path/to/ALCF-Dashboard/scripts/update_dashboard.sh --push >> /home/<you>/alcf-dashboard.log 2>&1
```

or keep it running in a terminal or tmux with
`scripts/update_dashboard.sh --push --every 1800`. Cron has a minimal `PATH`
and no SSH agent: make sure `python3`, `git`, `gh` and `alcf-tokens` are found
and that `git push` to GitHub works without a prompt. A run is skipped while
the previous one is still going.

### 4. GitHub Pages

In the repository settings, set **Pages → Build and deployment → Source** to
**GitHub Actions**. The [`Dashboard`](.github/workflows/pages.yml) workflow runs
the tests and deploys `docs/` with the latest snapshot from the
`dashboard-data` branch on every push to `main`, when
`scripts/update_dashboard.sh --push` asks for it, and every 30 minutes.

Before deploying, the workflow refreshes the public system status. If an
`ALCF_IRI_TOKEN` repository secret is set, it also refreshes projects and IRI
jobs, keeping the snapshot's SSH data; optional `ALCF_IRI_URL` and `ALCF_USER`
variables override the API URL and the login name. IRI access tokens expire after 48 hours; once the secret has expired, the
dashboard keeps the last good data and shows a warning. SSH cannot run in
Actions because of MFA.

> **Privacy:** A GitHub Pages site of a personal account is public (access
> control needs GitHub Enterprise Cloud), and so is everything pushed to a
> public repository, including the `dashboard-data` branch. The snapshot
> contains your username, project names, job names and quota usage. If that is
> a concern, keep the data local (`python3 -m http.server -d docs`) and do not
> use `--push`. Tokens are never written to the snapshot.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The web page (`docs/index.html`, `docs/app.js`, `docs/style.css`) is plain
HTML/CSS/JavaScript with no build step.
