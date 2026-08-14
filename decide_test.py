# /// script
# requires-python = ">=3.10"
# ///
import sys
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

from vast_cli.api.run import LAUNCH_GRACE_S, _decide

NOW = 1_000_000
JOB = {
    "label": "vrun:t",
    "cmd": "x",
    "launched_at": NOW,
    "grace_s": 900,
    "max_age_s": 10_000,
    "drain_s": 1800,
}
INST = {"id": 1, "label": "vrun:t", "start_date": 0}


def P(**kw):
    d = {
        "job": dict(JOB),
        "done": "-",
        "mtime": "0",
        "hold": False,
        "drain": "-",
        "has_pgid": True,
        "alive": True,
        "restarts": 0,
    }
    d.update(kw)
    return d


def inst(start_date):
    return {**INST, "start_date": start_date}


NO_SD = {"id": 9, "label": "vrun:t"}


PAST_GRACE = NOW + LAUNCH_GRACE_S + 1
OLD_DONE = P(done="1", mtime=str(NOW - 1000))

CASES = [
    ("unreachable_old", inst(1), None, NOW, "destroy"),
    ("unreachable_young", inst(NOW - 50), None, NOW, None),
    ("unreachable_no_startdate", NO_SD, None, NOW, "warn"),
    ("half_launched_old", inst(1), P(job=None), NOW, "destroy"),
    ("half_launched_young", inst(NOW - 50), P(job=None), NOW, None),
    ("half_launched_no_startdate", NO_SD, P(job=None), NOW, "warn"),
    ("draining_exited", INST, P(drain=str(NOW + 500), done="0"), NOW, "destroy"),
    ("draining_deadline", INST, P(drain=str(NOW - 1), done="-"), NOW, "destroy"),
    ("draining_time_left", INST, P(drain=str(NOW + 500), done="-"), NOW, None),
    ("run_nopgid_past_grace", INST, P(has_pgid=False), PAST_GRACE, "destroy"),
    ("run_nopgid_in_grace", INST, P(has_pgid=False), NOW + 10, None),
    ("crash_restart", INST, P(alive=False, restarts=0), NOW + 100, "restart"),
    ("crash_cap_hold", INST, P(alive=False, restarts=3, hold=True), NOW + 100, None),
    ("crash_cap_destroy", INST, P(alive=False, restarts=3), NOW + 100, "destroy"),
    ("alive_past_maxage", INST, P(alive=True), NOW + 10_001, "signal"),
    ("alive_in_maxage", INST, P(alive=True), NOW + 50, None),
    ("exit_zero", INST, P(done="0"), NOW, "destroy"),
    ("exit_nonzero_hold", INST, P(done="1", hold=True), NOW, None),
    ("exit_nonzero_past_grace", INST, OLD_DONE, NOW, "destroy"),
    ("exit_nonzero_in_grace", INST, P(done="1", mtime=str(NOW - 100)), NOW, None),
]


def main():
    failed = 0
    for name, i, p, now, want in CASES:
        action, _ = _decide(i, p, now, default_max_age=86_400, max_restarts=3)
        if action == want:
            print(f"PASS {name}")
        else:
            failed += 1
            print(f"FAIL {name}: got {action!r}, want {want!r}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
