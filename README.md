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
collector (Python 3, standard library only) writes a JSON snapshot that the page
renders. If `docs/data/dashboard.json` does not exist, the page shows
`docs/data/sample.json` with a "sample data" banner.

## What is collected

| Data | IRI API ([docs](https://docs.alcf.anl.gov/services/iri-api/)) | SSH |
|---|---|---|
| System status & incidents | `GET /status/resources`, `GET /status/incidents` | – |
| Jobs | `POST /compute/status/{resource}` | `qselect -u $USER` + `qstat -f -F json` |
| Projects & allocations (node-hours) | `GET /account/projects`, `.../project_allocations`, `.../user_allocations` | – |
| Storage quotas | storage (bytes/inodes) project allocations | `myquota`, `myprojectquotas` |

Every source is optional and failures are isolated: the dashboard's *Data
sources* table shows what succeeded, when, and any errors. Jobs reported by both
the IRI API and `qstat` are merged into a single row.

## Usage

### 1. IRI API token

Get a token with [`alcf-tokens`](https://github.com/argonne-lcf/alcf-tokens):

```bash
pip install alcf-tokens        # or: uvx alcf-tokens ...
alcf-tokens login iri
export ALCF_IRI_TOKEN=$(alcf-tokens get-token iri)
```

If `ALCF_IRI_TOKEN` is not set but `alcf-tokens` is on your `PATH`, the
collector calls `alcf-tokens get-token iri` itself (`--token-command` overrides
this). The API base URL defaults to `https://api.alcf.anl.gov/api/v1`; change it
with `--iri-url` or `ALCF_IRI_URL`.

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
Use `--ssh-host aurora=myalias` to point at a different host alias,
`--ssh-user` to set the user, `--machines aurora,polaris` to limit machines and
`--no-ssh` to disable SSH entirely.

### 3. Collect and publish

```bash
python3 collector/alcf_dashboard.py            # writes docs/data/dashboard.json
python3 -m http.server -d docs 8000            # preview at http://localhost:8000
git add docs/data/dashboard.json && git commit -m "Update dashboard" && git push
```

Run it periodically (e.g. from `cron` on your workstation or an ALCF login node)
to keep the dashboard fresh. `--merge docs/data/dashboard.json` keeps data from
sources that were not refreshed by the current run.

### 4. GitHub Pages

In the repository settings, set **Pages → Build and deployment → Source** to
**GitHub Actions**. The [`Dashboard`](.github/workflows/pages.yml) workflow runs
the tests and deploys `docs/` on every push to `main`.

Optionally add an `ALCF_IRI_TOKEN` repository secret (and an `ALCF_IRI_URL`
variable to override the API URL): the workflow then refreshes the IRI data
every 30 minutes and on each deploy, preserving the SSH data committed in
`docs/data/dashboard.json`. Note that IRI tokens expire (about 48 hours), so the
secret must be renewed; SSH cannot run in Actions because of MFA.

> **Privacy:** GitHub Pages sites are public unless your plan supports private
> Pages. The snapshot contains your username, project names, job names and
> quota usage. Use a private repository with private Pages, or keep the data
> local (`python3 -m http.server -d docs`) if that is a concern. Tokens are
> never written to the snapshot.

## Development

```bash
python3 -m unittest discover -s tests -v
```

The web page (`docs/index.html`, `docs/app.js`, `docs/style.css`) is plain
HTML/CSS/JavaScript with no build step.
