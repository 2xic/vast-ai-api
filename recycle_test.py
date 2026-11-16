# /// script
# requires-python = ">=3.10"
# ///
import sys
import tempfile
import types


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m


class _Sess:
    def mount(self, *a, **k):
        pass


_stub("requests", Session=_Sess)
_stub("requests.adapters", HTTPAdapter=lambda *a, **k: None)
_stub("urllib3.util", Retry=lambda *a, **k: None)
_stub("dotenv", load_dotenv=lambda *a, **k: None)

from vast_cli.api import run

REAL = {
    k: getattr(run, k)
    for k in (
        "_provision_reachable",
        "_push_and_start",
        "_probe",
        "_api_status",
        "_wait_gone",
        "remote",
        "sys",
        "time",
    )
}
NOW = 1_000_000


def _probe(hold=False, exit_code="-", mtime=str(NOW - 10), supervised=False):
    return {
        "job": {
            "label": "t",
            "cmd": "sh go.sh",
            "launched_at": NOW - 100,
            "grace_s": 900,
            "max_age_s": 86400,
        },
        "exit_code": exit_code,
        "mtime": mtime,
        "alive": True,
        "has_pgid": True,
        "hold": hold,
        "gpu": "0",
        "supervised": supervised,
    }


class _Clock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _harness(tmp, probes, logs=(), status="running", gone=True):
    for k, v in REAL.items():
        setattr(run, k, v)
    calls = {"black": [], "destroyed": [], "nodes": [], "out": []}
    run.time = _Clock()
    run.bad_machine_cache = lambda add=None, ttl=None: calls["black"].append(add)
    run.destroy_with_retries = calls["destroyed"].append
    run._wait_gone = lambda i, **k: gone
    run._find_existing = lambda label: []
    run.local_pubkey = lambda: "ssh-ed25519 AAA"
    run.key_on_account = lambda k: True
    run._api_status = lambda i: status
    seq = list(probes)
    run._probe = lambda i, claim=False: seq.pop(0) if seq else _probe(exit_code="0")
    out = list(logs)
    run.remote = types.SimpleNamespace(
        run=lambda i, c, timeout=None, text=True: out.pop(0) if out else (0, b"", "")
    )
    run.sys = types.SimpleNamespace(
        stdout=types.SimpleNamespace(
            buffer=types.SimpleNamespace(write=calls["out"].append),
            flush=lambda: None,
        )
    )
    run._push_and_start = lambda *a, **k: None

    def provision(filters, options, label, log, node_key=None, attempts=3, then=None):
        n = len(calls["nodes"]) + 1
        inst = {"id": 100 + n, "machine_id": 200 + n, "ssh": "ssh://x"}
        calls["nodes"].append(inst["id"])
        then(inst)
        return inst["id"], inst

    run._provision_reachable = provision
    return calls


def _launch():
    return run.launch(
        "./",
        "sh go.sh",
        run.AvailableInstancesFilter(min_gpu=1),
        run.InstanceOptions(),
        "t",
    )


def test_a_recycle_request_blacklists_the_machine_and_runs_on_a_fresh_node():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [_probe(exit_code="75"), _probe(exit_code="0")])
        assert _launch() == 0
        assert calls["black"] == [201]
        assert calls["destroyed"] == [101, 102]
        assert calls["nodes"] == [101, 102]


def test_each_fresh_node_gets_its_own_launch_time():
    with tempfile.TemporaryDirectory() as tmp:
        _harness(tmp, [_probe(exit_code="75"), _probe(exit_code="0")])
        starts = []
        run._push_and_start = lambda inst, src, cmd, job, *a: starts.append(
            job["launched_at"]
        )
        assert _launch() == 0
        assert len(starts) == 2 and starts[1] > starts[0]


def test_a_recycle_waits_for_the_job_to_exit():
    with tempfile.TemporaryDirectory() as tmp:
        seen = []
        calls = _harness(tmp, [])
        seq = [_probe(), _probe(exit_code="75")]
        run._probe = lambda i, claim=False: (
            seen.append(list(calls["destroyed"])) or seq.pop(0)
            if seq
            else _probe(exit_code="0")
        )
        assert _launch() == 0
        assert seen[:2] == [[], []] and calls["nodes"] == [101, 102]


def test_hold_blocks_the_recycle_and_reports_the_failure():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [_probe(hold=True, exit_code="75")])
        assert _launch() == 1
        assert calls["nodes"] == [101] and calls["destroyed"] == []


def test_a_failed_job_is_left_for_the_reaper():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [_probe(exit_code="1")])
        assert _launch() == 1
        assert calls["destroyed"] == [] and calls["nodes"] == [101]


def test_the_retry_loop_gives_up_with_75_at_the_recycle_budget():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [_probe(exit_code="75")] * (run.MAX_RECYCLES + 1))
        assert _launch() == 75
        assert len(calls["nodes"]) == run.MAX_RECYCLES + 1
        assert len(calls["destroyed"]) == run.MAX_RECYCLES + 1


def test_a_node_without_ssh_for_too_long_is_blacklisted_and_replaced():
    with tempfile.TemporaryDirectory() as tmp:
        ticks = run.SSH_LOST_S // run.SUPERVISE_POLL_S + 2
        calls = _harness(tmp, [None] * ticks + [_probe(exit_code="0")])
        assert _launch() == 0
        assert calls["black"] == [201] and calls["destroyed"] == [101, 102]


def test_a_short_ssh_outage_keeps_the_node():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [None, None, _probe(exit_code="0")])
        assert _launch() == 0
        assert calls["nodes"] == [101] and calls["destroyed"] == [101]


def test_api_errors_never_count_toward_a_lost_node():
    with tempfile.TemporaryDirectory() as tmp:
        ticks = run.SSH_LOST_S // run.SUPERVISE_POLL_S + 2
        calls = _harness(tmp, [None] * ticks + [_probe(exit_code="0")], status=None)
        assert _launch() == 0
        assert calls["nodes"] == [101] and calls["destroyed"] == [101]


def test_a_node_destroyed_by_someone_else_is_not_replaced():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [None], status="gone")
        assert _launch() == 1
        assert calls["nodes"] == [101] and calls["black"] == []


def test_an_exited_node_is_destroyed_and_not_replaced():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [None], status="exited")
        assert _launch() == 1
        assert calls["nodes"] == [101] and calls["destroyed"] == [101]


def test_a_node_that_lingers_after_destroy_stops_the_launch():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [_probe(exit_code="75")], gone=False)
        try:
            _launch()
            raise AssertionError("expected a lingering node to stop the launch")
        except RuntimeError as e:
            assert "still listed" in str(e)
        assert calls["nodes"] == [101]


def _pulled(tmp, logs):
    calls = _harness(tmp, [_probe(), _probe(), _probe(exit_code="0")], logs)
    offs = []
    real = run._pull_log
    run._pull_log = lambda i, off: (offs.append(off), real(i, off))[1]
    try:
        _launch()
    finally:
        run._pull_log = real
    return calls["out"], offs


def test_the_log_is_streamed_forward_from_the_last_byte():
    bar = "\u2588".encode()
    with tempfile.TemporaryDirectory() as tmp:
        out, offs = _pulled(
            tmp,
            [(0, b"ab" + bar[:1], ""), (0, bar[1:], ""), (0, b"", "")],
        )
        assert b"".join(out) == b"ab" + bar and offs == [1, 4, 6]


def test_a_rerun_that_asks_for_a_new_node_destroys_it_and_exits_75():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [_probe(exit_code="75")])
        node = {"id": 9, "machine_id": 99, "ssh": "ssh://x", "label": "vrun:t"}
        run._find_existing = lambda label: [node]
        run._reset_node = lambda inst, log: None
        run._write_job = lambda inst, job: None
        old = _probe()
        seq = [old, _probe(exit_code="75")]
        run._probe = lambda i, claim=False: seq.pop(0)
        assert run.rerun("t") == 75
        assert calls["black"] == [99] and calls["destroyed"] == [9]


def test_a_rerun_without_a_node_exits_75_so_the_caller_launches():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [])
        assert run.rerun("t") == 75
        assert calls["destroyed"] == []


def test_a_rerun_on_a_broken_node_destroys_it_and_exits_75():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [])
        run._find_existing = lambda label: [{"id": 9, "label": "vrun:t"}]
        run._probe = lambda i, claim=False: None
        assert run.rerun("t") == 75
        assert calls["destroyed"] == [9] and calls["black"] == []


def _reap(tmp, probe, now):
    calls = _harness(tmp, [])
    run.time.t = now
    run._probe = lambda i, claim=False: probe
    run.get_running_instances = lambda: [{"id": 7, "label": "vrun:t"}]
    try:
        run.reap()
    finally:
        run.get_running_instances = REAL_GRI
    return calls["destroyed"]


def test_the_reaper_destroys_a_finished_node_nobody_owns():
    with tempfile.TemporaryDirectory() as tmp:
        assert _reap(tmp, _probe(exit_code="1", mtime=str(NOW - 3600)), NOW) == [7]


def test_the_reaper_leaves_a_node_its_supervisor_owns():
    with tempfile.TemporaryDirectory() as tmp:
        old = _probe(exit_code="1", mtime=str(NOW - 3600), supervised=True)
        assert _reap(tmp, old, NOW) == []


def test_the_backstop_destroys_an_owned_node_long_past_its_max_age():
    with tempfile.TemporaryDirectory() as tmp:
        late = NOW + 86400 + 1800 + run.BACKSTOP_SLACK_S
        assert _reap(tmp, _probe(supervised=True), late) == [7]


def test_a_job_that_died_without_exiting_gets_a_fresh_node():
    with tempfile.TemporaryDirectory() as tmp:
        died = {**_probe(), "alive": False}
        calls = _harness(tmp, [died, _probe(exit_code="0")])
        assert _launch() == 0
        assert calls["black"] == [201] and calls["destroyed"] == [101, 102]


def test_a_setup_failure_destroys_the_node_without_a_blacklist():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [])
        run._provision_reachable = REAL["_provision_reachable"]
        run.bad_machine_cache = lambda add=None, ttl=None: (
            calls["black"].append(add) if add else set()
        )
        run._rent_offer = lambda *a, **k: (101, 201)
        run.wait_until_ready = lambda i, log=None: {"id": i, "ssh": "ssh://x"}
        run.remote = types.SimpleNamespace(wait=lambda inst, log=None: None)

        def fail(*a, **k):
            raise run.SetupFailed("setup failed (rc=1)")

        run._push_and_start = fail
        try:
            _launch()
            raise AssertionError("expected SetupFailed")
        except run.SetupFailed:
            pass
        assert calls["destroyed"] == [101] and calls["black"] == []


def test_the_reaper_destroys_a_node_that_asked_for_a_new_one_but_never_rents():
    with tempfile.TemporaryDirectory() as tmp:
        calls = _harness(tmp, [])
        run.launch = lambda *a, **k: calls["nodes"].append("relaunch")
        try:
            inst = {"id": 7, "machine_id": 77, "label": "vrun:t"}
            run._reap_one(inst, _probe(exit_code="75"), NOW, 86400)
        finally:
            run.launch = REAL_LAUNCH
        assert calls["nodes"] == [] and calls["destroyed"] == [7]


def _real_probe(sup_age, claim):
    sent = []
    out = "job=\nexit_code=-\nmtime=-\nhold=0\nhas_pgid=0\nhb_age=-\n"
    out += f"gpu=\nsup_age={sup_age}\n"
    run.remote = types.SimpleNamespace(
        run_any=lambda i, c, timeout=None: (sent.append(c), (0, out, ""))[1]
    )
    try:
        p = REAL["_probe"]({"id": 1}, claim=claim)
    finally:
        run.remote = REAL["remote"]
    return p["supervised"], sent[0].startswith("touch ")


def test_only_the_supervisor_probe_marks_the_node_supervised():
    assert _real_probe("5", claim=True) == (True, True)
    assert _real_probe("5", claim=False) == (True, False)
    assert _real_probe(str(run.SUPERVISED_FRESH_S), claim=False) == (False, False)
    assert _real_probe("-", claim=False) == (False, False)


REAL_LAUNCH = run.launch
REAL_GRI = run.get_running_instances

for name, fn in [(k, v) for k, v in list(globals().items()) if k.startswith("test_")]:
    fn()
    print("ok", name)
