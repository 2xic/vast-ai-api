# /// script
# requires-python = ">=3.10"
# ///
import base64
import json
import subprocess
import sys
import time
import types


def _stub(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class _Sess:
    def mount(self, *a, **k):
        pass


_stub("requests", Session=_Sess)
_stub("requests.adapters", HTTPAdapter=lambda *a, **k: None)
_stub("urllib3.util", Retry=lambda *a, **k: None)
_stub("dotenv", load_dotenv=lambda *a, **k: None)

from vast_cli.api import remote, run
from vast_cli.api.run import (
    REMOTE_DIR,
    _killgroup,
    _probe,
    _reset_node,
    _restart_job,
    _signal_shutdown,
    _start_detached,
)

CONTAINER = "vast_bashtest"
IMAGE = "debian:stable-slim"
INST = {"id": "test", "ssh_host": "test", "ssh_port": 22}
JOB = {
    "label": "vrun:t",
    "cmd": "sleep 300",
    "launched_at": 1000,
    "grace_s": 900,
    "max_age_s": 86400,
    "drain_s": 1800,
}


def dexec(cmd):
    p = subprocess.run(
        ["docker", "exec", CONTAINER, "sh", "-c", cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    return p.returncode, p.stdout, p.stderr


def fake_run(inst, cmd, timeout=None):
    return dexec(cmd)


def reset(job=None):
    dexec(f"rm -rf {REMOTE_DIR}; mkdir -p {REMOTE_DIR}")
    if job is not None:
        b = base64.b64encode(json.dumps(job).encode()).decode()
        dexec(f"printf %s {b} | base64 -d > {REMOTE_DIR}/JOB")


def start_container():
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)
    subprocess.run(
        ["docker", "run", "-d", "--name", CONTAINER, IMAGE, "sleep", "infinity"],
        check=True,
        capture_output=True,
    )


def stop_container():
    subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True, check=False)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def killgroup(sig):
    dexec(_killgroup(sig))


@case
def parse_roundtrip():
    reset(JOB)
    dexec(f"echo 0 > {REMOTE_DIR}/RESTARTS")
    _start_detached(INST, "sleep 300")
    p = _probe(INST)
    assert p is not None, "probe returned None"
    assert p["job"] == JOB, f"job mismatch: {p['job']}"
    assert p["restarts"] == 0, p["restarts"]


@case
def running_is_alive():
    reset(JOB)
    _start_detached(INST, "sleep 300")
    p = _probe(INST)
    assert p["has_pgid"], "no pgid after start"
    assert p["alive"], "not alive after start"
    assert p["done"] == "-", f"unexpected DONE: {p['done']}"


@case
def clean_exit_records_code():
    reset(JOB)
    _start_detached(INST, "sh -c 'exit 7'")
    time.sleep(2)
    p = _probe(INST)
    assert p["done"] == "7", f"exit code not captured: {p['done']}"


@case
def crash_detected_by_stale_heartbeat():
    reset(JOB)
    _start_detached(INST, "sleep 300")
    killgroup("KILL")
    old = run.HEARTBEAT_STALE_S
    run.HEARTBEAT_STALE_S = 1
    time.sleep(2)
    try:
        p = _probe(INST)
    finally:
        run.HEARTBEAT_STALE_S = old
    assert p["has_pgid"], "pgid marker gone"
    assert not p["alive"], "crashed tree still reads alive"
    assert p["done"] == "-", "DONE written on hard kill (should not be)"


@case
def stale_heartbeat_is_dead():
    reset(JOB)
    _start_detached(INST, "sleep 300")
    dexec(f"touch -d '2 minutes ago' {REMOTE_DIR}/HEARTBEAT")
    p = _probe(INST)
    assert not p["alive"], "stale heartbeat not treated as dead"


@case
def signal_terms_group_and_records():
    reset(JOB)
    _start_detached(INST, "sleep 300")
    _signal_shutdown(INST, 12345)
    time.sleep(2)
    p = _probe(INST)
    assert p["drain"] == "12345", f"drain deadline missing: {p['drain']}"
    ex = dexec(f"test -f {REMOTE_DIR}/SHUTDOWN")[0]
    assert ex == 0, "SHUTDOWN marker missing"
    assert p["done"] != "-", "wrapper did not record DONE after child TERM"


@case
def restart_increments_and_revives():
    reset(JOB)
    _start_detached(INST, "sleep 300")
    killgroup("KILL")
    time.sleep(1)
    _restart_job(INST, "sleep 300")
    p = _probe(INST)
    assert p["restarts"] == 1, f"restart count wrong: {p['restarts']}"
    assert p["alive"], "not alive after restart"
    assert p["done"] == "-", "stale DONE survived restart"


@case
def reset_clears_restarts():
    reset(JOB)
    _start_detached(INST, "sleep 300")
    dexec(f"echo 2 > {REMOTE_DIR}/RESTARTS")
    _reset_node(INST, lambda m: None)
    ex = dexec(f"test -f {REMOTE_DIR}/RESTARTS")[0]
    assert ex != 0, "RESTARTS survived reset (rerun would inherit stale count)"


@case
def clean_exit_kills_heartbeat():
    reset(JOB)
    _start_detached(INST, "sh -c 'exit 0'")
    time.sleep(2)
    hb1 = dexec(f"stat -c %Y {REMOTE_DIR}/HEARTBEAT")[1].strip()
    time.sleep(3)
    hb2 = dexec(f"stat -c %Y {REMOTE_DIR}/HEARTBEAT")[1].strip()
    assert hb1 and hb1 == hb2, "heartbeat still ticking after clean exit (toucher leaked)"


@case
def survives_container_reboot():
    reset(JOB)
    _start_detached(INST, "sleep 300")
    p = _probe(INST)
    assert p["alive"] and p["has_pgid"], "job not running before reboot"
    old_pgid = dexec(f"cat {REMOTE_DIR}/PGID")[1].strip()

    subprocess.run(
        ["docker", "restart", "-t", "0", CONTAINER], check=True, capture_output=True
    )

    assert dexec(f"test -f {REMOTE_DIR}/JOB")[0] == 0, "JOB marker lost on reboot"
    assert dexec(f"test -f {REMOTE_DIR}/PGID")[0] == 0, "PGID marker lost on reboot"
    gone = dexec(f"kill -0 {old_pgid} 2>/dev/null; echo $?")[1].strip()
    assert gone != "0", "job process survived container reboot (unexpected)"

    dexec(f"touch -d '2 minutes ago' {REMOTE_DIR}/HEARTBEAT")
    p = _probe(INST)
    assert p["has_pgid"] and not p["alive"], "dead-after-reboot job not detected"
    action, _ = run._decide(INST, p, 2000, default_max_age=86400, max_restarts=3)
    assert action == "restart", f"reaper must restart post-reboot job, got {action!r}"


@case
def write_job_roundtrips_marker():
    reset(JOB)
    run._write_job(INST, dict(JOB, max_age_s=12345))
    p = _probe(INST)
    assert p["job"]["max_age_s"] == 12345, f"JOB marker not updated: {p['job']}"


def main():
    remote.run = fake_run
    run.remote.run = fake_run
    start_container()
    failed = 0
    try:
        for fn in CASES:
            try:
                fn()
                print(f"PASS {fn.__name__}")
            except AssertionError as e:
                failed += 1
                print(f"FAIL {fn.__name__}: {e}")
            except Exception as e:
                failed += 1
                print(f"ERROR {fn.__name__}: {e!r}")
    finally:
        stop_container()
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
