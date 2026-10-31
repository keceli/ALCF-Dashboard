#!/usr/bin/env python3
"""Collect ALCF jobs, projects/allocations and quotas into a JSON snapshot.

The snapshot is rendered by the static dashboard in ``docs/`` (GitHub Pages).

Data sources, in order of preference:

* The ALCF implementation of the IRI Facility API
  (https://docs.alcf.anl.gov/services/iri-api/) for system status, incidents,
  projects, allocations (compute and storage) and jobs.
* SSH to the login nodes of aurora, polaris and crux for PBS jobs
  (``qselect``/``qstat``) and filesystem quotas (``myquota`` /
  ``myprojectquotas``).

Only the Python standard library (3.6+) is used so the script runs anywhere,
including ALCF login nodes (``/usr/bin/python3`` is 3.6) and GitHub Actions
runners.
"""

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SCHEMA_VERSION = 1
DEFAULT_IRI_URL = "https://api.alcf.anl.gov/api/v2"
DEFAULT_MACHINES = ("aurora", "polaris", "crux")
DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "docs", "data", "dashboard.json"
)
LIST_KEYS = ("resources", "incidents", "jobs", "projects", "quotas")
# IRI sources.  Each is all-or-nothing: its records replace the previous
# snapshot's only when every call it depends on succeeded (see merge_snapshots).
IRI_STATUS, IRI_ACCOUNT, IRI_JOBS = "iri:status", "iri:account", "iri:jobs"


def utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

_SIZE_RE = re.compile(r"^([0-9]*\.?[0-9]+)\s*([KMGTPE]?)(I?B?)$", re.IGNORECASE)
_SIZE_EXP = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}


def parse_size(text):
    """Parse a human readable size (``7.45G``, ``1T``, ``512k``, ``10GiB``) into bytes.

    Sizes reported by ALCF quota tools are binary (powers of 1024).  Returns
    ``None`` when the value cannot be interpreted (e.g. ``none`` or ``-``).
    """
    if text is None:
        return None
    match = _SIZE_RE.match(str(text).strip().rstrip("*"))
    if not match:
        return None
    number, prefix, _ = match.groups()
    return int(round(float(number) * 1024 ** _SIZE_EXP[prefix.upper()]))


def parse_duration(text):
    """Parse ``[[D:]HH:]MM:SS`` (PBS walltime) into seconds."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return int(text)
    parts = str(text).strip().split(":")
    try:
        values = [int(float(p)) for p in parts]
    except ValueError:
        return None
    seconds = 0
    for factor, value in zip((1, 60, 3600, 86400), reversed(values)):
        seconds += factor * value
    return seconds


def parse_pbs_time(text):
    """Convert a PBS timestamp (``Thu Oct 30 12:00:00 2026`` or epoch) to ISO 8601.

    :data:`SSH_SCRIPT` runs ``qstat`` with ``TZ=UTC`` so textual times are UTC.
    """
    if text in (None, ""):
        return None
    if isinstance(text, (int, float)) or str(text).isdigit():
        return _dt.datetime.fromtimestamp(int(text), _dt.timezone.utc).isoformat()
    try:
        parsed = _dt.datetime.strptime(" ".join(str(text).split()), "%a %b %d %H:%M:%S %Y")
        return parsed.replace(tzinfo=_dt.timezone.utc).isoformat()
    except ValueError:
        return str(text)


_ISO_RE = re.compile(r"^(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?$")


def parse_iso(text):
    """Parse an ISO 8601 timestamp; naive times are UTC (``fromisoformat`` needs 3.7+)."""
    match = _ISO_RE.match(str(text or "").strip())
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    parsed = _dt.datetime.strptime(day + "T" + clock, "%Y-%m-%dT%H:%M:%S")
    if fraction:
        parsed = parsed.replace(microsecond=int(fraction[1:7].ljust(6, "0")))
    minutes = 0
    if zone and zone != "Z":
        minutes = (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 60 + int(zone[-2:]))
    return parsed.replace(tzinfo=_dt.timezone(_dt.timedelta(minutes=minutes)))


def epoch_to_iso(value):
    if value in (None, ""):
        return None
    try:
        return _dt.datetime.fromtimestamp(float(value), _dt.timezone.utc).replace(microsecond=0).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


def unit_kind(unit):
    """Map an IRI allocation unit (URN or legacy name) to node_hours/bytes/inodes."""
    text = str(unit or "").lower()
    if "node" in text and "hour" in text:
        return "node_hours"
    if "inode" in text:
        return "inodes"
    if "byte" in text:
        return "bytes"
    return text or "unknown"


def last_path_segment(uri):
    if not uri:
        return None
    return str(uri).rstrip("/").rsplit("/", 1)[-1]


def short_job_id(job_id):
    return str(job_id).split(".", 1)[0]


# ---------------------------------------------------------------------------
# IRI API
# ---------------------------------------------------------------------------


class IRIError(RuntimeError):
    pass


def _problem_detail(text):
    """The ``detail`` of an RFC 9457 problem response, or ``""``."""
    try:
        detail = json.loads(text).get("detail")
    except (ValueError, AttributeError):
        return ""
    return detail if isinstance(detail, str) else ""


def as_records(value):
    """Check that a response is a list of JSON objects, as IRI collections are."""
    if not isinstance(value, list) or not all(isinstance(v, dict) for v in value):
        raise ValueError(f"expected a list of objects, got {type(value).__name__}")
    return value


def _retry_after(headers, default):
    """Seconds asked for by a ``Retry-After`` header (at most a minute), else ``default``."""
    try:
        return max(0.0, min(float(headers.get("Retry-After")), 60.0))
    except (AttributeError, TypeError, ValueError):
        return default


class IRIClient:
    """Minimal client for the IRI Facility API.  Without a token only the
    public endpoints (``/status/...``) can be used.

    ALCF allows 5 requests per second per user, so requests are spaced
    ``MIN_INTERVAL`` apart and retried with backoff after HTTP 429.
    """

    MIN_INTERVAL = 0.25
    RETRIES = 4

    def __init__(self, base_url, token=None, timeout=60, urlopen=None, sleep=None, clock=None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._next_request = 0.0
        self.rate_limited = 0  # HTTP 429 responses retried

    def _wait_turn(self):
        delay = self._next_request - self._clock()
        if delay > 0:
            self._sleep(delay)
        self._next_request = self._clock() + self.MIN_INTERVAL

    def request(self, method, path, params=None, body=None):
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        for attempt in range(self.RETRIES + 1):
            self._wait_turn()
            try:
                with self._urlopen(req, timeout=self.timeout) as resp:
                    payload = resp.read()
                break
            except urllib.error.HTTPError as exc:
                text = exc.read().decode(errors="replace") if exc.fp else ""
                detail = _problem_detail(text)
                if exc.code == 429 and attempt < self.RETRIES:
                    self.rate_limited += 1
                    self._sleep(_retry_after(exc.headers, 2 ** attempt))
                    continue
                # ALCF answers an empty collection with 404 {"detail": "No incidents found"}.
                if exc.code == 404 and re.match(r"No .* found", detail):
                    return []
                raise IRIError(f"{method} {path}: HTTP {exc.code} {(detail or text)[:200]}".strip()) from None
            except (urllib.error.URLError, OSError) as exc:
                raise IRIError(f"{method} {path}: {exc}") from None
        if not payload:
            return None
        return json.loads(payload)

    def get(self, path, **params):
        return self.request("GET", path, params=params or None)

    def post(self, path, body=None, **params):
        return self.request("POST", path, params=params or None, body=body)

    def get_list(self, path, **params):
        return as_records(self.get(path, **params))

    def post_list(self, path, body=None, **params):
        return as_records(self.post(path, body, **params))


def normalize_resource(res):
    return {
        "id": res.get("id"),
        "name": res.get("name") or res.get("id"),
        "description": res.get("description"),
        "status": res.get("current_status"),
        "type": last_path_segment(str(res.get("resource_type")).replace(":", "/")) if res.get("resource_type") else None,
        "group": res.get("group"),
        "endpoints": res.get("supported_endpoints") or [],
        "source": IRI_STATUS,
    }


def is_compute(resource):
    """True for a normalized compute resource.  ALCF leaves ``supported_endpoints``
    empty, so the resource type (``compute`` or ``urn:doe-iri:resource:compute``)
    decides."""
    return resource.get("type") == "compute" or "compute" in (resource.get("endpoints") or [])


def incident_is_current(incident, now=None):
    now = now or _dt.datetime.now(_dt.timezone.utc)
    end = incident.get("end")
    if not end:
        return True
    end_dt = parse_iso(end)
    return end_dt is None or end_dt >= now


def normalize_incident(inc):
    return {
        "id": inc.get("id"),
        "name": inc.get("name"),
        "description": inc.get("description"),
        "status": inc.get("status"),
        "type": inc.get("type"),
        "resolution": inc.get("resolution"),
        "start": inc.get("start"),
        "end": inc.get("end"),
        "resources": [last_path_segment(u) for u in inc.get("resource_uris") or []],
        "source": IRI_STATUS,
    }


def normalize_iri_job(job, machine):
    status = job.get("status") or {}
    spec = job.get("job_spec") or {}
    attrs = spec.get("attributes") or {}
    resources = spec.get("resources") or {}
    return {
        "machine": machine,
        "id": short_job_id(job.get("id")),
        "name": spec.get("name"),
        "state": (status.get("state") or "unknown").lower(),
        "message": status.get("message"),
        "queue": attrs.get("queue_name"),
        "project": attrs.get("account"),
        "nodes": resources.get("node_count"),
        "walltime_s": attrs.get("duration"),
        "used_walltime_s": None,
        "submitted": None,
        "started": None,
        "estimated_start": None,
        "updated": epoch_to_iso(status.get("time")),
        "exit_code": status.get("exit_code"),
        "source": IRI_JOBS,
    }


def _entries_by_kind(entries):
    return {unit_kind(e.get("unit")): e for e in entries or []}


def normalize_project(project, allocations, capabilities, user_allocations=None):
    """Flatten IRI project allocations into one row per (capability, unit)."""
    rows = []
    for alloc in allocations or []:
        cap_id = alloc.get("capability_id") or last_path_segment(alloc.get("capability_uri"))
        cap = capabilities.get(cap_id, {})
        user_entries = _entries_by_kind((user_allocations or {}).get(alloc.get("id"), {}).get("entries"))
        for entry in alloc.get("entries") or []:
            kind = unit_kind(entry.get("unit"))
            user_entry = user_entries.get(kind) or {}
            rows.append({
                "id": alloc.get("id"),
                "capability": cap.get("name") or cap_id,
                "unit": kind,
                "allocation": entry.get("allocation"),
                "usage": entry.get("usage"),
                "user_allocation": user_entry.get("allocation"),
                "user_usage": user_entry.get("usage"),
            })
    return {
        "id": project.get("id"),
        "name": project.get("name") or project.get("id"),
        "description": project.get("description"),
        "allocations": rows,
        "source": IRI_ACCOUNT,
    }


def _pick_user_allocation(user_allocs, user_ids):
    user_allocs = user_allocs or []
    for ua in user_allocs:
        if ua.get("user_id") in user_ids:
            return ua
    if len(user_allocs) == 1:
        return user_allocs[0]
    return None


def match_machine(name, machines):
    lowered = str(name or "").lower()
    for machine in machines:
        if machine.lower() in lowered:
            return machine.lower()
    return None


def collect_iri(client, machines, username=None, log=print):
    """Query the IRI API.

    Returns a dict that maps each IRI source to ``(records, errors, ok)``.
    ``ok`` is False when a call the source depends on failed (e.g. with an
    expired token), so that :func:`merge_snapshots` keeps the previous data
    instead of an empty list.  Without a token only the public status is
    collected.  Jobs are listed for ``username``, the ALCF login name.
    """
    errors = {IRI_STATUS: [], IRI_ACCOUNT: [], IRI_JOBS: []}
    failed = set()

    def attempt(source, label, func, fatal=True):
        try:
            return func()
        except (IRIError, ValueError, TypeError, AttributeError) as exc:
            errors[source].append(f"{label}: {exc}")
            if fatal:
                failed.add(source)
            log(f"  [iri] {label} failed: {exc}")
            return None

    def part(source, **records):
        return records, errors[source], source not in failed

    resources = attempt(IRI_STATUS, "status/resources", lambda: [
        normalize_resource(r) for r in client.get_list("/status/resources")])
    # Unfiltered, incidents come oldest first, 100 per page: ask for current ones.
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
    incidents = attempt(IRI_STATUS, "status/incidents", lambda: [
        normalize_incident(i)
        for i in client.get_list("/status/incidents", time=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        if incident_is_current(i, now)], fatal=False)
    status = part(IRI_STATUS, resources=resources or [], incidents=incidents or [])
    if not client.token:
        return {IRI_STATUS: status}

    # At ALCF this is an identity UUID, which user allocations refer to, not the login name.
    who = attempt(IRI_ACCOUNT, "whoami", lambda: client.get("/account/whoami"), fatal=False)
    user_ids = {who.get("username") if isinstance(who, dict) else None, username} - {None}

    projects = []
    caps = attempt(IRI_ACCOUNT, "account/capabilities", lambda: {
        c.get("id"): c for c in client.get_list("/account/capabilities")}, fatal=False) or {}
    for project in attempt(IRI_ACCOUNT, "account/projects", lambda: client.get_list("/account/projects")) or []:
        pid = urllib.parse.quote(str(project.get("id")), safe="")
        allocs = attempt(
            IRI_ACCOUNT, f"projects/{project.get('id')}/project_allocations",
            lambda pid=pid: client.get_list(f"/account/projects/{pid}/project_allocations"),
        ) or []
        user_allocs = {}
        for alloc in allocs:
            aid = urllib.parse.quote(str(alloc.get("id")), safe="")
            uas = attempt(
                IRI_ACCOUNT, f"projects/{project.get('id')}/user_allocations",
                lambda pid=pid, aid=aid: client.get_list(
                    f"/account/projects/{pid}/project_allocations/{aid}/user_allocations"
                ),
                fatal=False,
            )
            picked = _pick_user_allocation(uas, user_ids)
            if picked:
                user_allocs[alloc.get("id")] = picked
        row = attempt(IRI_ACCOUNT, f"projects/{project.get('id')}",
                      lambda: normalize_project(project, allocs, caps, user_allocs))
        if row:
            projects.append(row)

    # Jobs on every compute resource that matches one of the requested machines.
    # Without the owner filter ALCF lists everyone's jobs.  It rejects
    # include_spec (HTTP 501) but returns the queue and account anyway.
    jobs = []
    if resources is None or not username:
        reason = "resource list unavailable" if resources is None else "username unknown (use --user)"
        errors[IRI_JOBS].append("skipped: " + reason)
        failed.add(IRI_JOBS)
    else:
        for res in resources:
            machine = match_machine(res["name"], machines) if is_compute(res) else None
            if not machine:
                continue
            rid = urllib.parse.quote(str(res["id"]), safe="")
            found = attempt(IRI_JOBS, f"compute/status/{machine}", lambda rid=rid, machine=machine: [
                normalize_iri_job(j, machine)
                for j in client.post_list(f"/compute/status/{rid}", body={"owner": username}, limit=1000)])
            jobs.extend(found or [])

    return {
        IRI_STATUS: status,
        IRI_ACCOUNT: part(IRI_ACCOUNT, projects=projects),
        IRI_JOBS: part(IRI_JOBS, jobs=jobs),
    }


# ---------------------------------------------------------------------------
# SSH (PBS + quota tools on the login nodes)
# ---------------------------------------------------------------------------

SSH_SCRIPT = r"""
echo "@@SECTION whoami"
id -un
echo "@@SECTION jobs"
ids=$(qselect -u "$(id -un)" 2>/dev/null | tr '\n' ' ')
if [ -n "$ids" ]; then TZ=UTC qstat -f -F json $ids 2>/dev/null; else echo '{"Jobs":{}}'; fi
echo "@@SECTION myquota"
command -v myquota >/dev/null 2>&1 && myquota 2>&1
echo "@@SECTION myprojectquotas"
command -v myprojectquotas >/dev/null 2>&1 && myprojectquotas 2>&1
echo "@@END"
"""

PBS_STATES = {
    "Q": "queued", "W": "queued", "M": "queued", "T": "queued",
    "H": "held", "S": "held", "U": "held",
    "R": "active", "E": "active", "B": "active",
    "F": "completed", "X": "completed",
}


def split_sections(text):
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("@@SECTION "):
            current = line.split(" ", 1)[1].strip()
            sections[current] = []
        elif line.startswith("@@END"):
            current = None
        elif current is not None:
            sections[current].append(line)
    return {k: "\n".join(v) for k, v in sections.items()}


def _loads_lenient(text):
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in qstat output")
    text = text[start:]
    try:
        return json.loads(text, strict=False)
    except json.JSONDecodeError:
        # PBS does not always escape backslashes in Variable_List.
        return json.loads(re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", text), strict=False)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pbs_comment(comment):
    """Drop the exec host list that PBS appends to "Job run at ..." comments."""
    if not comment:
        return None
    return re.sub(r"^(Job run at .*?) on \(.*$", r"\1", str(comment), flags=re.S)


def parse_qstat_json(text, machine):
    data = _loads_lenient(text) if text.strip() else {}
    jobs = []
    for job_id, job in (data.get("Jobs") or {}).items():
        rlist = job.get("Resource_List") or {}
        used = job.get("resources_used") or {}
        state = PBS_STATES.get(str(job.get("job_state", "")).upper(), "unknown")
        exit_code = _to_int(job.get("Exit_status"))
        if state == "completed" and exit_code not in (None, 0):
            state = "failed"
        estimated = job.get("estimated") or {}
        jobs.append({
            "machine": machine,
            "id": short_job_id(job_id),
            "name": job.get("Job_Name"),
            "state": state,
            "message": _pbs_comment(job.get("comment")),
            "queue": job.get("queue"),
            "project": job.get("project") or job.get("Account_Name"),
            "nodes": _to_int(rlist.get("nodect")),
            "walltime_s": parse_duration(rlist.get("walltime")),
            "used_walltime_s": parse_duration(used.get("walltime")),
            "submitted": parse_pbs_time(job.get("qtime") or job.get("ctime")),
            "started": parse_pbs_time(job.get("stime")),
            "estimated_start": parse_pbs_time(estimated.get("start_time")),
            "updated": parse_pbs_time(job.get("mtime")),
            "exit_code": exit_code,
            "source": f"ssh:{machine}",
        })
    return jobs


def parse_quota_table(text, machine):
    """Parse the tables printed by ``myquota`` / ``myprojectquotas``.

    Columns come from the header line above the ``====`` separator, e.g.
    ``Name Type Filesystem Used Quota Grace``; Aurora adds a ``Pool`` column.
    ``myquota`` lists project quotas too, so the scope comes from ``Type``.
    """
    rows, header, previous = [], None, ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if set(stripped) <= set("=-"):
            header = [c.lower() for c in previous.split()]
            continue
        previous = stripped
        if not header or not {"name", "used", "quota"} <= set(header):
            continue
        cols = dict(zip(header, stripped.split(None, len(header) - 1)))
        used, quota = cols.get("used"), cols.get("quota")
        used_b, quota_b = parse_size(used), parse_size(quota)
        if used_b is None and quota_b is None:
            continue  # repeated header or free text
        kind, pool = cols.get("type"), cols.get("pool")
        rows.append({
            "machine": machine,
            "scope": (kind or "").lower() or None,
            "name": cols["name"],
            "type": kind,
            "filesystem": cols.get("filesystem"),
            "pool": None if pool in (None, "-") else pool,
            "used": used,
            "quota": quota,
            "used_bytes": used_b,
            "quota_bytes": quota_b,
            "grace": cols.get("grace"),
            "source": f"ssh:{machine}",
        })
    return rows


def dedupe_quotas(rows):
    seen, out = set(), []
    for row in rows:
        key = (row["name"], row["type"], row["filesystem"], row["pool"])
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


def collect_ssh(machine, host, ssh_command="ssh", timeout=120, run=subprocess.run):
    """Run :data:`SSH_SCRIPT` on ``host`` and parse jobs and quotas."""
    cmd = shlex.split(ssh_command) + [
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", host, "bash", "-l", "-s",
    ]
    try:
        proc = run(cmd, input=SSH_SCRIPT, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   universal_newlines=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"ssh {host}: {exc}") from None
    if "@@END" not in proc.stdout:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        raise RuntimeError(f"ssh {host} exited with {proc.returncode}: {msg[-1] if msg else ''}")
    sections = split_sections(proc.stdout)
    out = {"user": (sections.get("whoami") or "").strip() or None, "jobs": [], "quotas": [], "errors": []}
    try:
        out["jobs"] = parse_qstat_json(sections.get("jobs", ""), machine)
    except ValueError as exc:
        out["errors"].append(f"qstat: {exc}")
    quotas = parse_quota_table(sections.get("myquota", ""), machine)
    quotas += parse_quota_table(sections.get("myprojectquotas", ""), machine)
    out["quotas"] = dedupe_quotas(quotas)
    return out


# ---------------------------------------------------------------------------
# Snapshot assembly
# ---------------------------------------------------------------------------


def empty_snapshot():
    snap = {"schema_version": SCHEMA_VERSION, "generated_at": utcnow(), "user": None, "sources": {}}
    snap.update({key: [] for key in LIST_KEYS})
    return snap


def merge_snapshots(new, old):
    """Keep records from ``old`` whose source was not successfully refreshed in ``new``.

    This lets e.g. a scheduled GitHub Actions run refresh IRI data while
    preserving the SSH data that was pushed from a workstation.
    """
    if not old:
        return new
    refreshed = {name for name, info in new["sources"].items() if info.get("ok")}
    merged = dict(new)
    for key in LIST_KEYS:
        kept = [r for r in old.get(key) or [] if r.get("source") not in refreshed]
        merged[key] = list(new.get(key) or []) + kept
    sources = dict(old.get("sources") or {})
    for name, info in new["sources"].items():
        if not info.get("ok") and name in sources:
            info = dict(info, last_success=sources[name].get("last_success"))
        sources[name] = info
    merged["sources"] = sources
    merged["user"] = new.get("user") or old.get("user")
    return merged


def get_token(args):
    """Return ``(token, error)``; ``error`` says why the token command failed."""
    token = os.environ.get(args.token_env, "").strip()
    if token:
        return token, None
    command = args.token_command
    if command is None and shutil.which("alcf-tokens"):
        command = "alcf-tokens get-token iri"
    if not command:
        return None, None
    try:
        proc = subprocess.run(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"token command failed: {exc}"
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip().splitlines()[-1].strip(), None
    # The last line of an error message or traceback is the telling one.
    lines = (proc.stderr or proc.stdout).strip().splitlines()
    return None, "token command failed: " + (lines[-1].strip() if lines else f"exit status {proc.returncode}")


def read_snapshot(path):
    """The snapshot at ``path``, or None if there is none or it is unreadable."""
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        try:
            return json.load(fh)
        except json.JSONDecodeError as exc:
            print(f"[merge] ignoring unreadable {path}: {exc}")
            return None


def parse_host_overrides(values):
    hosts = {}
    for value in values or []:
        machine, sep, host = value.partition("=")
        if not sep or not machine.strip() or not host.strip():
            raise SystemExit(f"--ssh-host expects MACHINE=HOST, got {value!r}")
        hosts[machine.strip().lower()] = host.strip()
    return hosts


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("-o", "--output", default=os.path.normpath(DEFAULT_OUTPUT),
                   help="where to write the JSON snapshot (default: docs/data/dashboard.json)")
    p.add_argument("--merge", metavar="PATH",
                   help="previous snapshot whose data is kept for sources not refreshed by this run")
    p.add_argument("--machines", default=",".join(DEFAULT_MACHINES),
                   help="comma separated machines (default: %(default)s)")
    p.add_argument("--user", default=os.environ.get("ALCF_USER"),
                   help="your ALCF login name, for listing your jobs through the IRI API "
                        "(default: $ALCF_USER, else from SSH or the --merge snapshot)")
    g = p.add_argument_group("IRI API")
    g.add_argument("--no-iri", action="store_true", help="skip the IRI API")
    g.add_argument("--iri-url", default=os.environ.get("ALCF_IRI_URL", DEFAULT_IRI_URL),
                   help="IRI API base URL (default: %(default)s)")
    g.add_argument("--token-env", default="ALCF_IRI_TOKEN",
                   help="environment variable holding the access token (default: %(default)s)")
    g.add_argument("--token-command",
                   help="command printing an access token (default: 'alcf-tokens get-token iri' if installed)")
    s = p.add_argument_group("SSH")
    s.add_argument("--no-ssh", action="store_true", help="skip SSH collection")
    s.add_argument("--ssh-command", default="ssh", help="ssh executable and options (default: %(default)s)")
    s.add_argument("--ssh-user", help="username for SSH (default: from ~/.ssh/config)")
    s.add_argument("--ssh-host", action="append", metavar="MACHINE=HOST",
                   help="override the SSH host for a machine (default: MACHINE.alcf.anl.gov)")
    s.add_argument("--ssh-timeout", type=int, default=120, help="seconds per machine (default: %(default)s)")
    return p


def add_source(snap, name, records, errors, ok, **extra):
    """Record a source.  Its records are only added when it succeeded; otherwise
    :func:`merge_snapshots` keeps those of the previous snapshot."""
    now = utcnow()
    snap["sources"][name] = dict(extra, ok=ok, errors=errors, collected_at=now,
                                 last_success=now if ok else None)
    if ok:
        for key, rows in records.items():
            snap[key].extend(rows)


def main(argv=None):
    args = build_parser().parse_args(argv)
    machines = [m.strip().lower() for m in args.machines.split(",") if m.strip()]
    hosts = parse_host_overrides(args.ssh_host)
    previous = read_snapshot(args.merge)
    snap = empty_snapshot()
    snap["user"] = args.user

    if not args.no_ssh:
        for machine in machines:
            host = hosts.get(machine, f"{machine}.alcf.anl.gov")
            if args.ssh_user and "@" not in host:
                host = f"{args.ssh_user}@{host}"
            name = f"ssh:{machine}"
            print(f"[ssh] {machine} ({host})")
            try:
                data = collect_ssh(machine, host, args.ssh_command, args.ssh_timeout)
            except RuntimeError as exc:
                print(f"  [ssh] {exc}")
                add_source(snap, name, {}, [str(exc)], False)
                continue
            for error in data["errors"]:
                print(f"  [ssh] {error}")
            add_source(snap, name, {"jobs": data["jobs"], "quotas": data["quotas"]},
                       data["errors"], not data["errors"])
            snap["user"] = snap["user"] or data["user"]

    # The IRI API lists jobs by login name; without SSH (e.g. in CI) it comes
    # from the previous snapshot.
    snap["user"] = snap["user"] or (previous or {}).get("user")
    if not args.no_iri:
        print(f"[iri] querying {args.iri_url}")
        token, token_error = get_token(args)
        if token_error:
            print(f"  [iri] {token_error}")
        if not token:
            print(f"  [iri] no access token (set {args.token_env} or install alcf-tokens): "
                  "collecting the public system status only")
        client = IRIClient(args.iri_url, token)
        parts = collect_iri(client, machines, username=snap["user"])
        if client.rate_limited:
            print(f"  [iri] rate limited {client.rate_limited} times and retried; "
                  "is something else using your account at the same time?")
        if token_error:
            # Show the failure, and keep the previous account and job data.
            parts.update({name: ({}, [token_error], False) for name in (IRI_ACCOUNT, IRI_JOBS)})
        for name, (records, errors, ok) in parts.items():
            add_source(snap, name, records, errors, ok, url=args.iri_url)

    snap = merge_snapshots(snap, previous)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    tmp = args.output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, args.output)
    print(f"wrote {args.output}: " + ", ".join(f"{len(snap[k])} {k}" for k in LIST_KEYS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
