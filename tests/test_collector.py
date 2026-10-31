import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "collector"))

import alcf_dashboard as ad  # noqa: E402

QSTAT_JSON = r"""
Some login banner printed before the JSON
{
  "timestamp": 1793390000,
  "pbs_version": "2022.1.7",
  "Jobs": {
    "123456.aurora-pbs-0001.hostmgmt.cm.aurora.alcf.anl.gov": {
      "Job_Name": "train",
      "job_state": "R",
      "queue": "prod",
      "project": "MyProject",
      "Resource_List": {"nodect": 128, "walltime": "06:00:00"},
      "resources_used": {"walltime": "01:30:00"},
      "qtime": "Thu Oct 29 10:00:00 2026",
      "stime": "Thu Oct 29 11:00:00 2026",
      "comment": "Job run at Thu Oct 29 at 11:00 on (x4720c5s4b0n0:ncpus=208)+(x4720c5s5b0n0:ncpus=208)",
      "Variable_List": "PATH=C:\windows"
    },
    "123457.aurora-pbs-0001.hostmgmt.cm.aurora.alcf.anl.gov": {
      "Job_Name": "eval",
      "job_state": "Q",
      "queue": "debug",
      "Account_Name": "OtherProject",
      "Resource_List": {"nodect": "2", "walltime": "1:00:00"},
      "estimated": {"start_time": "Fri Oct 30 09:15:00 2026"},
      "comment": "Not Running: Insufficient amount of resource: ncpus"
    },
    "123400.aurora-pbs-0001": {"Job_Name": "old", "job_state": "F", "Exit_status": 1},
    "123458[1].aurora-pbs-0001": {"Job_Name": "array", "job_state": "X", "Exit_status": 0}
  }
}
"""

MYQUOTA = """Name                           Type  Filesystem        Used     Quota    Grace
=========================================================================================
alice                          User  /home             7.45G    50.00G   none
"""

MYPROJECTQUOTAS = """
Lustre : Current Project Quota information for projects you're a member of:

Name                Type      Filesystem          Used               Quota             Grace
==============================================================================================
MyProject           Project   flare               1.5T               10T               none
Name                Type      Filesystem          Used               Quota             Grace
MyProject           Project   eagle               8k                 1T                none
"""

# Shortened `myquota` output from a Crux login node (names changed).  It lists
# project quotas too, and `*` marks usage over the quota.
CRUX_MYQUOTA = """
Current quota information for yourself and projects you're a member of:

Name                           Type     Filesystem          Used          Quota           Grace
===============================================================================================
alice                       User        /home             38.26G            50G               -
ProjA                       Project     /lus/grand        63.72T           100T               -
ProjA                       Project     /lus/eagle        63.72T           100T               -
ProjE                       Project     /lus/eagle       2.159T*             2T         expired
"""

# Shortened `myquota` output from an Aurora login node (names changed), which
# has an extra Pool column.
AURORA_MYQUOTA = """
Current quota information for yourself and projects you're a member of:

Name                           Type     Filesystem    Pool              Used          Quota           Grace
===========================================================================================================
ProjA                       Project     /lus/gecko    -                   8k             0k               -
ProjB_with_a_long_project_name  Project     /lus/flare    -               453.3G             1T               -
ProjC                       Project     /lus/flare    -              2.327G*             1M               -
ProjD                       Project     /lus/flare    -                104k*             1M         expired
alice                       User        /home         -               126.7G         268.7G               -
alice                       User        /home         ddn_hdd         74.06G         179.1G               -
alice                       User        /home         ddn_ssd          52.9G         89.57G               -
"""


def quiet(*_):
    pass


class HelperTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(ad.parse_size("8k"), 8192)
        self.assertEqual(ad.parse_size("1T"), 1024 ** 4)
        self.assertEqual(ad.parse_size("1.5G"), int(1.5 * 1024 ** 3))
        self.assertEqual(ad.parse_size("10GiB"), 10 * 1024 ** 3)
        self.assertEqual(ad.parse_size("2.327G*"), ad.parse_size("2.327G"))
        self.assertEqual(ad.parse_size("0"), 0)
        self.assertIsNone(ad.parse_size("none"))
        self.assertIsNone(ad.parse_size("Used"))

    def test_parse_duration(self):
        self.assertEqual(ad.parse_duration("06:00:00"), 21600)
        self.assertEqual(ad.parse_duration("30:00"), 1800)
        self.assertEqual(ad.parse_duration("1:00:00:00"), 86400)
        self.assertIsNone(ad.parse_duration("abc"))

    def test_parse_iso(self):
        utc = ad._dt.timezone.utc
        self.assertEqual(ad.parse_iso("2026-10-30T20:04:58.017841Z"),
                         ad._dt.datetime(2026, 10, 30, 20, 4, 58, 17841, tzinfo=utc))
        self.assertEqual(ad.parse_iso("2026-10-30T15:00:00-05:00"),
                         ad._dt.datetime(2026, 10, 30, 20, 0, tzinfo=utc))
        self.assertEqual(ad.parse_iso("2026-10-30T20:00:00"), ad._dt.datetime(2026, 10, 30, 20, 0, tzinfo=utc))
        self.assertIsNone(ad.parse_iso("yesterday"))

    def test_unit_kind(self):
        self.assertEqual(ad.unit_kind("urn:doe-iri:allocation:compute:node-hours"), "node_hours")
        self.assertEqual(ad.unit_kind("node_hours"), "node_hours")
        self.assertEqual(ad.unit_kind("urn:doe-iri:allocation:storage:bytes"), "bytes")
        self.assertEqual(ad.unit_kind("inodes"), "inodes")

    def test_match_machine(self):
        self.assertEqual(ad.match_machine("Aurora", ad.DEFAULT_MACHINES), "aurora")
        self.assertEqual(ad.match_machine("polaris-compute", ad.DEFAULT_MACHINES), "polaris")
        self.assertIsNone(ad.match_machine("sophia", ad.DEFAULT_MACHINES))

    def test_is_compute(self):
        # ALCF reports "supported_endpoints": [] for every resource (v1 and v2).
        for rtype in ("compute", "urn:doe-iri:resource:compute"):
            resource = ad.normalize_resource({"id": "r", "name": "Aurora", "resource_type": rtype,
                                              "supported_endpoints": []})
            self.assertTrue(ad.is_compute(resource))
        storage = ad.normalize_resource({"id": "e", "name": "Eagle", "resource_type": "urn:doe-iri:resource:storage"})
        self.assertFalse(ad.is_compute(storage))


class SSHParsingTests(unittest.TestCase):
    def test_parse_qstat_json(self):
        jobs = {j["id"]: j for j in ad.parse_qstat_json(QSTAT_JSON, "aurora")}
        self.assertEqual(set(jobs), {"123456", "123457", "123400", "123458[1]"})
        running = jobs["123456"]
        self.assertEqual(running["state"], "active")
        self.assertEqual(running["nodes"], 128)
        self.assertEqual(running["walltime_s"], 21600)
        self.assertEqual(running["used_walltime_s"], 5400)
        self.assertEqual(running["project"], "MyProject")
        self.assertEqual(running["started"], "2026-10-29T11:00:00+00:00")
        self.assertEqual(running["message"], "Job run at Thu Oct 29 at 11:00")
        self.assertEqual(running["source"], "ssh:aurora")
        queued = jobs["123457"]
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["project"], "OtherProject")
        self.assertEqual(queued["nodes"], 2)
        self.assertEqual(queued["estimated_start"], "2026-10-30T09:15:00+00:00")
        self.assertEqual(queued["message"], "Not Running: Insufficient amount of resource: ncpus")
        self.assertEqual(jobs["123400"]["state"], "failed")
        self.assertEqual(jobs["123458[1]"]["state"], "completed")

    def test_parse_qstat_empty(self):
        self.assertEqual(ad.parse_qstat_json('{"Jobs":{}}', "crux"), [])
        self.assertEqual(ad.parse_qstat_json("", "crux"), [])

    def test_parse_quota_tables(self):
        user = ad.parse_quota_table(MYQUOTA, "polaris")
        self.assertEqual(len(user), 1)
        self.assertEqual(user[0]["filesystem"], "/home")
        self.assertEqual(user[0]["scope"], "user")
        self.assertEqual(user[0]["quota_bytes"], 50 * 1024 ** 3)
        proj = ad.parse_quota_table(MYPROJECTQUOTAS, "polaris")
        self.assertEqual([r["filesystem"] for r in proj], ["flare", "eagle"])
        self.assertEqual([r["scope"] for r in proj], ["project", "project"])
        self.assertEqual(proj[1]["used_bytes"], 8192)

    def test_parse_quota_mixed_scopes(self):
        rows = ad.parse_quota_table(CRUX_MYQUOTA, "crux")
        self.assertEqual([(r["name"], r["scope"], r["filesystem"]) for r in rows], [
            ("alice", "user", "/home"), ("ProjA", "project", "/lus/grand"),
            ("ProjA", "project", "/lus/eagle"), ("ProjE", "project", "/lus/eagle")])
        self.assertTrue(all(r["pool"] is None for r in rows))
        self.assertEqual(rows[0]["used_bytes"], ad.parse_size("38.26G"))
        self.assertEqual(rows[0]["quota_bytes"], 50 * 1024 ** 3)
        self.assertEqual((rows[3]["used_bytes"], rows[3]["grace"]), (ad.parse_size("2.159T"), "expired"))

    def test_parse_quota_pool_column(self):
        rows = ad.parse_quota_table(AURORA_MYQUOTA, "aurora")
        self.assertEqual(len(rows), 7)
        home = [r for r in rows if r["name"] == "alice"]
        self.assertEqual([r["pool"] for r in home], [None, "ddn_hdd", "ddn_ssd"])
        self.assertEqual({r["scope"] for r in home}, {"user"})
        self.assertEqual((home[0]["used_bytes"], home[0]["quota_bytes"], home[0]["grace"]),
                         (ad.parse_size("126.7G"), ad.parse_size("268.7G"), "-"))
        projects = {r["name"]: r for r in rows if r["scope"] == "project"}
        self.assertEqual(projects["ProjA"]["quota_bytes"], 0)
        self.assertEqual(projects["ProjB_with_a_long_project_name"]["used_bytes"], ad.parse_size("453.3G"))
        self.assertEqual((projects["ProjC"]["used_bytes"], projects["ProjC"]["quota_bytes"]),
                         (ad.parse_size("2.327G"), 1024 ** 2))
        self.assertEqual(projects["ProjD"]["grace"], "expired")

    def test_collect_ssh(self):
        # myprojectquotas repeats the project rows of myquota.
        stdout = "\n".join([
            "motd", "@@SECTION whoami", "alice", "@@SECTION jobs", QSTAT_JSON,
            "@@SECTION myquota", CRUX_MYQUOTA, "@@SECTION myprojectquotas", CRUX_MYQUOTA, "@@END",
        ])
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        out = ad.collect_ssh("aurora", "aurora.alcf.anl.gov", run=fake_run)
        self.assertEqual(out["user"], "alice")
        self.assertEqual(len(out["jobs"]), 4)
        self.assertEqual([r["scope"] for r in out["quotas"]], ["user", "project", "project", "project"])
        self.assertEqual(out["errors"], [])
        cmd, kwargs = calls[0]
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[-4:], ["aurora.alcf.anl.gov", "bash", "-l", "-s"])
        self.assertEqual(kwargs["input"], ad.SSH_SCRIPT)

    def test_collect_ssh_failure(self):
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 255, "", "Permission denied (publickey).")

        with self.assertRaisesRegex(RuntimeError, "Permission denied"):
            ad.collect_ssh("aurora", "aurora.alcf.anl.gov", run=fake_run)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Problem:
    """An error response (RFC 9457 problem) served by :class:`FakeIRI`."""

    def __init__(self, status, detail, headers=None):
        self.status, self.detail, self.headers = status, detail, headers or {}


class Responses:
    """A route that answers with each response in turn, then keeps repeating the last."""

    def __init__(self, *responses):
        self.responses = list(responses)

    def next(self):
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


class FakeIRI:
    """Serves canned IRI responses keyed by (method, path)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, req, timeout=None):
        path = req.full_url.replace("https://iri.test/api/v2", "")
        self.requests.append(req)
        route = self.routes.get((req.get_method(), path.split("?", 1)[0]), Problem(404, "Not Found"))
        if isinstance(route, Responses):
            route = route.next()
        if isinstance(route, Problem):
            body = json.dumps({"status": route.status, "detail": route.detail}).encode()
            raise urllib.error.HTTPError(req.full_url, route.status, route.detail, route.headers, io.BytesIO(body))
        return FakeResponse(json.dumps(route).encode())

    def find(self, method, path):
        return [r for r in self.requests if r.get_method() == method and path in r.full_url]


# Shapes as served by ALCF's API v2.  /account/whoami gives an identity UUID,
# which user allocations refer to; jobs are listed by login name ("alice").
ALICE_ID, BOB_ID = "6f1c3b9e-1a2b-4c3d-8e4f-0000000a11ce", "0b0b0b0b-1a2b-4c3d-8e4f-000000000b0b"
IRI_ROUTES = {
    ("GET", "/account/whoami"): {"username": ALICE_ID},
    ("GET", "/status/resources"): [
        {"id": "r-aurora", "name": "Aurora", "current_status": "up", "supported_endpoints": [],
         "resource_type": "urn:doe-iri:resource:compute", "group": "computes"},
        {"id": "r-polaris", "name": "Polaris", "current_status": "degraded", "supported_endpoints": [],
         "resource_type": "urn:doe-iri:resource:compute", "group": "computes"},
        {"id": "r-sophia", "name": "Sophia", "current_status": "up", "supported_endpoints": [],
         "resource_type": "urn:doe-iri:resource:compute", "group": "computes"},
        {"id": "r-eagle", "name": "Eagle", "current_status": "unknown", "supported_endpoints": [],
         "resource_type": "urn:doe-iri:resource:storage", "group": "storages"},
    ],
    ("GET", "/status/incidents"): [
        {"id": "i1", "name": "Polaris maintenance", "status": "down", "start": "2026-10-30T00:00:00Z",
         "end": None, "type": "planned", "resolution": "pending"},
        {"id": "i2", "name": "old", "status": "down", "start": "2020-01-01T00:00:00Z",
         "end": "2020-01-02T00:00:00Z", "type": "unplanned", "resolution": "completed"},
    ],
    # Without include_spec, which ALCF rejects, only the queue and account are filled in.
    ("POST", "/compute/status/r-aurora"): [
        {"id": "123456.aurora-pbs", "status": {"state": "active", "exit_code": None},
         "job_spec": {"attributes": {"queue_name": "prod", "account": "MyProject"}}},
    ],
    # ALCF answers an empty listing with a 404 problem.
    ("POST", "/compute/status/r-polaris"): Problem(404, "No jobs found"),
    ("GET", "/account/capabilities"): [{"id": "cap-aurora", "name": "Aurora nodes"}],
    ("GET", "/account/projects"): [{"id": "MyProject", "name": "MyProject", "description": "AI"}],
    ("GET", "/account/projects/MyProject/project_allocations"): [
        {"id": "a1", "capability_uri": "https://iri.test/api/v2/account/capabilities/cap-aurora",
         "entries": [{"allocation": 100000, "usage": 25000, "unit": "urn:doe-iri:allocation:compute:node-hours"}]},
    ],
    ("GET", "/account/projects/MyProject/project_allocations/a1/user_allocations"): [
        {"id": "u1", "user_id": BOB_ID, "entries": [{"allocation": 0, "usage": 1, "unit": "node_hours"}]},
        {"id": "u2", "user_id": ALICE_ID, "entries": [{"allocation": 0, "usage": 500, "unit": "node_hours"}]},
    ],
}

EXPIRED = Problem(401, "Facility Specific authentication failed: 401: Globus token not active.")
EXPIRED_ROUTES = dict(IRI_ROUTES)
EXPIRED_ROUTES.update({key: EXPIRED for key in IRI_ROUTES if key[1].startswith(("/account", "/compute"))})


RATE_LIMITED = Problem(429, "Per-user rate limit of 5 reqs/sec exceeded.")


class FakeTime:
    """A clock that only advances while sleeping."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(changes=None, token="tok", fake_time=None):
    routes = dict(IRI_ROUTES)
    routes.update(changes or {})
    fake = FakeIRI(routes)
    timing = {"sleep": fake_time.sleep, "clock": fake_time.clock} if fake_time else {"sleep": lambda _: None}
    return ad.IRIClient("https://iri.test/api/v2/", token, urlopen=fake, **timing), fake


class IRITests(unittest.TestCase):
    def test_collect_iri(self):
        client, fake = make_client()
        parts = ad.collect_iri(client, ad.DEFAULT_MACHINES, username="alice", log=quiet)

        self.assertEqual({name: (ok, errors) for name, (_, errors, ok) in parts.items()},
                         {ad.IRI_STATUS: (True, []), ad.IRI_ACCOUNT: (True, []), ad.IRI_JOBS: (True, [])})
        status, account, jobs = (parts[name][0] for name in (ad.IRI_STATUS, ad.IRI_ACCOUNT, ad.IRI_JOBS))
        self.assertEqual([r["name"] for r in status["resources"]], ["Aurora", "Polaris", "Sophia", "Eagle"])
        self.assertEqual(status["resources"][0]["type"], "compute")
        self.assertEqual([i["id"] for i in status["incidents"]], ["i1"])
        # Polaris has no jobs (404 "No jobs found") and Sophia is not requested.
        self.assertEqual(len(jobs["jobs"]), 1)
        job = jobs["jobs"][0]
        self.assertEqual((job["machine"], job["id"], job["state"], job["queue"], job["project"], job["source"]),
                         ("aurora", "123456", "active", "prod", "MyProject", ad.IRI_JOBS))
        alloc = account["projects"][0]["allocations"][0]
        self.assertEqual(alloc["capability"], "Aurora nodes")
        self.assertEqual(alloc["unit"], "node_hours")
        # Your usage is found through the whoami identity.
        self.assertEqual((alloc["allocation"], alloc["usage"], alloc["user_usage"]), (100000, 25000, 500))

        self.assertEqual(fake.requests[0].get_header("Authorization"), "Bearer " + "tok")
        self.assertIn("time=", fake.find("GET", "/status/incidents")[0].full_url)
        posts = fake.find("POST", "/compute/status/")
        self.assertEqual(sorted(p.full_url.split("?")[0].rsplit("/", 1)[1] for p in posts),
                         ["r-aurora", "r-polaris"])
        self.assertNotIn("include_spec", posts[0].full_url)  # ALCF answers it with HTTP 501
        # By login name: without the filter ALCF lists everyone's jobs.
        self.assertEqual(json.loads(posts[0].data), {"owner": "alice"})

    def test_without_token_only_public_status(self):
        client, fake = make_client(token=None)
        parts = ad.collect_iri(client, ad.DEFAULT_MACHINES, username="alice", log=quiet)
        self.assertEqual(list(parts), [ad.IRI_STATUS])
        self.assertTrue(parts[ad.IRI_STATUS][2])
        self.assertFalse(any(r.has_header("Authorization") for r in fake.requests))
        self.assertEqual({r.full_url.split("?")[0] for r in fake.requests},
                         {"https://iri.test/api/v2/status/resources", "https://iri.test/api/v2/status/incidents"})

    def test_expired_token_fails_account_and_jobs(self):
        client, _ = make_client(EXPIRED_ROUTES)
        parts = ad.collect_iri(client, ad.DEFAULT_MACHINES, username="alice", log=quiet)
        self.assertTrue(parts[ad.IRI_STATUS][2])
        for name in (ad.IRI_ACCOUNT, ad.IRI_JOBS):
            records, errors, ok = parts[name]
            self.assertFalse(ok)
            self.assertIn("HTTP 401 Facility Specific authentication failed", errors[-1])

    def test_whoami_is_optional(self):
        client, fake = make_client({("GET", "/account/whoami"): Problem(404, "Not Found")})
        parts = ad.collect_iri(client, ad.DEFAULT_MACHINES, username="alice", log=quiet)
        records, errors, ok = parts[ad.IRI_ACCOUNT]
        self.assertTrue(ok)
        self.assertIn("whoami", errors[0])
        # Without the identity, your share of a multi-user allocation is unknown.
        self.assertIsNone(records["projects"][0]["allocations"][0]["user_usage"])
        self.assertEqual(json.loads(fake.find("POST", "/compute/status/r-aurora")[0].data), {"owner": "alice"})

    def test_jobs_need_the_login_name(self):
        client, fake = make_client()  # whoami gives an identity, not the login name
        parts = ad.collect_iri(client, ad.DEFAULT_MACHINES, log=quiet)
        self.assertEqual(parts[ad.IRI_JOBS][1:], (["skipped: username unknown (use --user)"], False))
        self.assertEqual(fake.find("POST", "/compute/status/"), [])

    def test_failed_allocation_call_fails_account(self):
        client, _ = make_client({("GET", "/account/projects/MyProject/project_allocations"): Problem(500, "boom")})
        parts = ad.collect_iri(client, ad.DEFAULT_MACHINES, username="alice", log=quiet)
        self.assertFalse(parts[ad.IRI_ACCOUNT][2])
        self.assertTrue(parts[ad.IRI_JOBS][2])

    def test_unexpected_response_shape_is_isolated(self):
        client, _ = make_client({("GET", "/status/resources"): {"items": []}})
        parts = ad.collect_iri(client, ad.DEFAULT_MACHINES, username="alice", log=quiet)
        self.assertFalse(parts[ad.IRI_STATUS][2])
        self.assertIn("expected a list of objects", parts[ad.IRI_STATUS][1][0])
        self.assertTrue(parts[ad.IRI_ACCOUNT][2])
        self.assertFalse(parts[ad.IRI_JOBS][2])  # needs the resource list

    def test_empty_collection_is_not_an_error(self):
        client, _ = make_client({("GET", "/status/incidents"): Problem(404, "No incidents found")})
        self.assertEqual(client.get_list("/status/incidents"), [])
        with self.assertRaisesRegex(ad.IRIError, "HTTP 404 Not Found"):
            client.get("/no/such/path")

    def test_requests_are_spaced_out(self):
        # ALCF allows 5 requests per second per user.
        fake_time = FakeTime()
        client, _ = make_client(fake_time=fake_time)
        for _ in range(3):
            client.get_list("/status/resources")
        self.assertEqual(fake_time.sleeps, [ad.IRIClient.MIN_INTERVAL] * 2)

    def test_rate_limited_request_is_retried(self):
        projects = IRI_ROUTES[("GET", "/account/projects")]
        fake_time = FakeTime()
        client, fake = make_client({("GET", "/account/projects"): Responses(
            Problem(429, RATE_LIMITED.detail, {"Retry-After": "3"}), RATE_LIMITED, projects)}, fake_time=fake_time)
        self.assertEqual(client.get_list("/account/projects"), projects)
        self.assertEqual(fake_time.sleeps, [3.0, 2])  # Retry-After, then backoff
        self.assertEqual((len(fake.requests), client.rate_limited), (3, 2))

    def test_persistent_rate_limit_is_an_error(self):
        fake_time = FakeTime()
        client, _ = make_client({("GET", "/account/projects"): RATE_LIMITED}, fake_time=fake_time)
        with self.assertRaisesRegex(ad.IRIError, "HTTP 429 Per-user rate limit"):
            client.get_list("/account/projects")
        self.assertEqual(fake_time.sleeps, [1, 2, 4, 8])


class MergeTests(unittest.TestCase):
    def test_merge_keeps_unrefreshed_sources(self):
        old = ad.empty_snapshot()
        old["user"] = "alice"
        old["sources"] = {ad.IRI_JOBS: {"ok": True, "last_success": "t0"},
                          "ssh:aurora": {"ok": True, "last_success": "t0"}}
        old["jobs"] = [{"id": "1", "source": ad.IRI_JOBS}, {"id": "2", "source": "ssh:aurora"}]
        old["quotas"] = [{"name": "alice", "source": "ssh:aurora"}]

        new = ad.empty_snapshot()
        new["sources"] = {ad.IRI_JOBS: {"ok": True, "last_success": "t1"}}
        new["jobs"] = [{"id": "3", "source": ad.IRI_JOBS}]

        merged = ad.merge_snapshots(new, old)
        self.assertEqual([j["id"] for j in merged["jobs"]], ["3", "2"])
        self.assertEqual(len(merged["quotas"]), 1)
        self.assertEqual(set(merged["sources"]), {ad.IRI_JOBS, "ssh:aurora"})
        self.assertEqual(merged["user"], "alice")

    def test_merge_failed_source_keeps_old_data(self):
        old = ad.empty_snapshot()
        old["sources"] = {ad.IRI_ACCOUNT: {"ok": True, "last_success": "t0"}}
        old["projects"] = [{"id": "p", "source": ad.IRI_ACCOUNT}]
        new = ad.empty_snapshot()
        new["sources"] = {ad.IRI_ACCOUNT: {"ok": False, "errors": ["HTTP 401"]}}
        merged = ad.merge_snapshots(new, old)
        self.assertEqual(merged["projects"], [{"id": "p", "source": ad.IRI_ACCOUNT}])
        self.assertEqual(merged["sources"][ad.IRI_ACCOUNT]["last_success"], "t0")
        self.assertFalse(merged["sources"][ad.IRI_ACCOUNT]["ok"])


class MainTests(unittest.TestCase):
    def test_main_without_sources_writes_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out", "dashboard.json")
            with unittest.mock.patch("sys.stdout", io.StringIO()):
                rc = ad.main(["--no-iri", "--no-ssh", "-o", path])
            self.assertEqual(rc, 0)
            with open(path) as fh:
                snap = json.load(fh)
            self.assertEqual(snap["schema_version"], ad.SCHEMA_VERSION)
            for key in ad.LIST_KEYS:
                self.assertEqual(snap[key], [])

    def refresh(self, old, routes, env, *args):
        """Update the snapshot ``old`` from the IRI API only, as the workflow does."""
        fake = FakeIRI(routes)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dashboard.json")
            with open(path, "w") as fh:
                json.dump(old, fh)
            with unittest.mock.patch("urllib.request.urlopen", fake), unittest.mock.patch("time.sleep"), \
                    unittest.mock.patch.dict(os.environ, env), unittest.mock.patch("sys.stdout", io.StringIO()):
                for key in ("ALCF_IRI_TOKEN", "ALCF_USER"):
                    if key not in env:
                        os.environ.pop(key, None)
                ad.main(["--no-ssh", "--iri-url", "https://iri.test/api/v2", "--merge", path, "-o", path, *args])
            with open(path) as fh:
                return json.load(fh), fake

    def test_expired_token_keeps_previous_data(self):
        # What the scheduled workflow does once the ALCF_IRI_TOKEN secret expires.
        old = ad.empty_snapshot()
        old["sources"] = {ad.IRI_ACCOUNT: {"ok": True, "last_success": "t0"},
                          ad.IRI_JOBS: {"ok": True, "last_success": "t0"}}
        old["projects"] = [{"id": "p", "source": ad.IRI_ACCOUNT}]
        old["jobs"] = [{"id": "1", "machine": "aurora", "source": ad.IRI_JOBS}]
        snap, _ = self.refresh(old, EXPIRED_ROUTES, {"ALCF_IRI_TOKEN": "expired"})
        self.assertEqual(snap["projects"], old["projects"])
        self.assertEqual(snap["jobs"], old["jobs"])
        for name in (ad.IRI_ACCOUNT, ad.IRI_JOBS):
            self.assertFalse(snap["sources"][name]["ok"])
            self.assertEqual(snap["sources"][name]["last_success"], "t0")
        # The public status is still refreshed.
        self.assertTrue(snap["sources"][ad.IRI_STATUS]["ok"])
        self.assertEqual(len(snap["resources"]), 4)

    def test_jobs_are_listed_for_the_snapshot_user(self):
        # Without SSH the login name comes from the previous snapshot, and the
        # whoami identity does not replace it.
        old = ad.empty_snapshot()
        old["user"] = "alice"
        snap, fake = self.refresh(old, IRI_ROUTES, {"ALCF_IRI_TOKEN": "tok"})
        self.assertEqual(json.loads(fake.find("POST", "/compute/status/r-aurora")[0].data), {"owner": "alice"})
        self.assertEqual(snap["user"], "alice")
        self.assertEqual([j["id"] for j in snap["jobs"]], ["123456"])

    def test_failing_token_command_is_reported(self):
        old = ad.empty_snapshot()
        old["sources"] = {ad.IRI_ACCOUNT: {"ok": True, "last_success": "t0"}}
        old["projects"] = [{"id": "p", "source": ad.IRI_ACCOUNT}]
        command = shlex.quote(sys.executable) + " -c \"raise SystemExit('RuntimeError: boom')\""
        snap, _ = self.refresh(old, IRI_ROUTES, {}, "--token-command", command)
        account = snap["sources"][ad.IRI_ACCOUNT]
        self.assertEqual((account["ok"], account["errors"]), (False, ["token command failed: RuntimeError: boom"]))
        self.assertEqual(snap["projects"], old["projects"])
        self.assertTrue(snap["sources"][ad.IRI_STATUS]["ok"])


if __name__ == "__main__":
    unittest.main()
