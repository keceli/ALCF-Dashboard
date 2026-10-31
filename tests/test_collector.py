import io
import json
import os
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
    "123400.aurora-pbs-0001": {"Job_Name": "old", "job_state": "F", "Exit_status": 1}
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


class HelperTests(unittest.TestCase):
    def test_parse_size(self):
        self.assertEqual(ad.parse_size("8k"), 8192)
        self.assertEqual(ad.parse_size("1T"), 1024 ** 4)
        self.assertEqual(ad.parse_size("1.5G"), int(1.5 * 1024 ** 3))
        self.assertEqual(ad.parse_size("10GiB"), 10 * 1024 ** 3)
        self.assertEqual(ad.parse_size("0"), 0)
        self.assertIsNone(ad.parse_size("none"))
        self.assertIsNone(ad.parse_size("Used"))

    def test_parse_duration(self):
        self.assertEqual(ad.parse_duration("06:00:00"), 21600)
        self.assertEqual(ad.parse_duration("30:00"), 1800)
        self.assertEqual(ad.parse_duration("1:00:00:00"), 86400)
        self.assertIsNone(ad.parse_duration("abc"))

    def test_unit_kind(self):
        self.assertEqual(ad.unit_kind("urn:doe-iri:allocation:compute:node-hours"), "node_hours")
        self.assertEqual(ad.unit_kind("node_hours"), "node_hours")
        self.assertEqual(ad.unit_kind("urn:doe-iri:allocation:storage:bytes"), "bytes")
        self.assertEqual(ad.unit_kind("inodes"), "inodes")

    def test_match_machine(self):
        self.assertEqual(ad.match_machine("Aurora", ad.DEFAULT_MACHINES), "aurora")
        self.assertEqual(ad.match_machine("polaris-compute", ad.DEFAULT_MACHINES), "polaris")
        self.assertIsNone(ad.match_machine("sophia", ad.DEFAULT_MACHINES))


class SSHParsingTests(unittest.TestCase):
    def test_parse_qstat_json(self):
        jobs = {j["id"]: j for j in ad.parse_qstat_json(QSTAT_JSON, "aurora")}
        self.assertEqual(set(jobs), {"123456", "123457", "123400"})
        running = jobs["123456"]
        self.assertEqual(running["state"], "active")
        self.assertEqual(running["nodes"], 128)
        self.assertEqual(running["walltime_s"], 21600)
        self.assertEqual(running["used_walltime_s"], 5400)
        self.assertEqual(running["project"], "MyProject")
        self.assertEqual(running["started"], "2026-10-29T11:00:00+00:00")
        self.assertEqual(running["source"], "ssh:aurora")
        queued = jobs["123457"]
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["project"], "OtherProject")
        self.assertEqual(queued["nodes"], 2)
        self.assertEqual(queued["estimated_start"], "2026-10-30T09:15:00+00:00")
        self.assertEqual(jobs["123400"]["state"], "failed")

    def test_parse_qstat_empty(self):
        self.assertEqual(ad.parse_qstat_json('{"Jobs":{}}', "crux"), [])
        self.assertEqual(ad.parse_qstat_json("", "crux"), [])

    def test_parse_quota_tables(self):
        user = ad.parse_quota_table(MYQUOTA, "polaris", "user")
        self.assertEqual(len(user), 1)
        self.assertEqual(user[0]["filesystem"], "/home")
        self.assertEqual(user[0]["quota_bytes"], 50 * 1024 ** 3)
        proj = ad.parse_quota_table(MYPROJECTQUOTAS, "polaris", "project")
        self.assertEqual([r["filesystem"] for r in proj], ["flare", "eagle"])
        self.assertEqual(proj[1]["used_bytes"], 8192)

    def test_collect_ssh(self):
        stdout = "\n".join([
            "motd", "@@SECTION whoami", "alice", "@@SECTION jobs", QSTAT_JSON,
            "@@SECTION myquota", MYQUOTA, "@@SECTION myprojectquotas", MYPROJECTQUOTAS, "@@END",
        ])
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        out = ad.collect_ssh("aurora", "aurora.alcf.anl.gov", run=fake_run)
        self.assertEqual(out["user"], "alice")
        self.assertEqual(len(out["jobs"]), 3)
        self.assertEqual(len(out["quotas"]), 3)
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


class FakeIRI:
    """Serves canned IRI responses keyed by (method, path)."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, req, timeout=None):
        path = req.full_url.replace("https://iri.test/api/v1", "")
        self.requests.append(req)
        key = (req.get_method(), path.split("?", 1)[0])
        if key not in self.routes:
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"detail":"nope"}'))
        return FakeResponse(json.dumps(self.routes[key]).encode())


IRI_ROUTES = {
    ("GET", "/account/whoami"): {"username": "alice"},
    ("GET", "/status/resources"): [
        {"id": "r-aurora", "name": "aurora", "current_status": "up", "supported_endpoints": ["compute"],
         "resource_type": "urn:doe-iri:resource:compute"},
        {"id": "r-polaris", "name": "Polaris", "current_status": "degraded", "supported_endpoints": ["compute"]},
        {"id": "r-eagle", "name": "eagle", "current_status": "up", "supported_endpoints": ["filesystem"]},
    ],
    ("GET", "/status/incidents"): [
        {"id": "i1", "name": "Polaris maintenance", "status": "down", "start": "2026-10-30T00:00:00Z",
         "end": None, "type": "planned", "resolution": "pending"},
        {"id": "i2", "name": "old", "status": "down", "start": "2020-01-01T00:00:00Z",
         "end": "2020-01-02T00:00:00Z", "type": "unplanned", "resolution": "completed"},
    ],
    ("POST", "/compute/status/r-aurora"): [
        {"id": "123456.aurora-pbs", "status": {"state": "active", "time": 1793390000},
         "job_spec": {"name": "train", "attributes": {"queue_name": "prod", "account": "MyProject"},
                      "resources": {"node_count": 128}}},
    ],
    ("GET", "/account/capabilities"): [{"id": "cap-aurora", "name": "Aurora nodes"}],
    ("GET", "/account/projects"): [{"id": "MyProject", "name": "MyProject", "description": "AI"}],
    ("GET", "/account/projects/MyProject/project_allocations"): [
        {"id": "a1", "capability_uri": "https://iri.test/api/v1/account/capabilities/cap-aurora",
         "entries": [{"allocation": 100000, "usage": 25000, "unit": "urn:doe-iri:allocation:compute:node-hours"}]},
    ],
    ("GET", "/account/projects/MyProject/project_allocations/a1/user_allocations"): [
        {"id": "u1", "user_id": "bob", "entries": [{"allocation": 0, "usage": 1, "unit": "node_hours"}]},
        {"id": "u2", "user_id": "alice", "entries": [{"allocation": 0, "usage": 500, "unit": "node_hours"}]},
    ],
}


class IRITests(unittest.TestCase):
    def test_collect_iri(self):
        fake = FakeIRI(IRI_ROUTES)
        client = ad.IRIClient("https://iri.test/api/v1/", "tok", urlopen=fake)
        out = ad.collect_iri(client, ad.DEFAULT_MACHINES, log=lambda *_: None)

        self.assertEqual(out["user"], "alice")
        self.assertEqual([r["name"] for r in out["resources"]], ["aurora", "Polaris", "eagle"])
        self.assertEqual(out["resources"][0]["type"], "compute")
        self.assertEqual([i["id"] for i in out["incidents"]], ["i1"])
        self.assertEqual(len(out["jobs"]), 1)
        job = out["jobs"][0]
        self.assertEqual((job["machine"], job["id"], job["state"], job["queue"], job["nodes"]),
                         ("aurora", "123456", "active", "prod", 128))
        alloc = out["projects"][0]["allocations"][0]
        self.assertEqual(alloc["capability"], "Aurora nodes")
        self.assertEqual(alloc["unit"], "node_hours")
        self.assertEqual((alloc["allocation"], alloc["usage"], alloc["user_usage"]), (100000, 25000, 500))
        # Polaris job listing is not served by the fake -> recorded as an error, not fatal.
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("compute/status/polaris", out["errors"][0])

        first = fake.requests[0]
        self.assertEqual(first.get_header("Authorization"), "Bearer " + "tok")
        post = [r for r in fake.requests if r.get_method() == "POST"][0]
        self.assertIn("include_spec=true", post.full_url)
        self.assertEqual(json.loads(post.data), {})


class MergeTests(unittest.TestCase):
    def test_merge_keeps_unrefreshed_sources(self):
        old = ad.empty_snapshot()
        old["user"] = "alice"
        old["sources"] = {"iri": {"ok": True, "last_success": "t0"}, "ssh:aurora": {"ok": True, "last_success": "t0"}}
        old["jobs"] = [{"id": "1", "source": "iri"}, {"id": "2", "source": "ssh:aurora"}]
        old["quotas"] = [{"name": "alice", "source": "ssh:aurora"}]

        new = ad.empty_snapshot()
        new["sources"] = {"iri": {"ok": True, "last_success": "t1"}}
        new["jobs"] = [{"id": "3", "source": "iri"}]

        merged = ad.merge_snapshots(new, old)
        self.assertEqual([j["id"] for j in merged["jobs"]], ["3", "2"])
        self.assertEqual(len(merged["quotas"]), 1)
        self.assertEqual(set(merged["sources"]), {"iri", "ssh:aurora"})
        self.assertEqual(merged["user"], "alice")

    def test_merge_failed_source_keeps_old_data(self):
        old = ad.empty_snapshot()
        old["sources"] = {"iri": {"ok": True, "last_success": "t0"}}
        old["projects"] = [{"id": "p", "source": "iri"}]
        new = ad.empty_snapshot()
        new["sources"] = {"iri": {"ok": False, "error": "HTTP 401"}}
        merged = ad.merge_snapshots(new, old)
        self.assertEqual(merged["projects"], [{"id": "p", "source": "iri"}])
        self.assertEqual(merged["sources"]["iri"]["last_success"], "t0")
        self.assertFalse(merged["sources"]["iri"]["ok"])


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


if __name__ == "__main__":
    unittest.main()
