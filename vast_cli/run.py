import base64
import json
import os
import time

from vast_cli import remote
from vast_cli.api import (
    AvailableInstancesFilter,
    InstanceOptions,
    attach_ssh_key,
    create_instance,
    delete_instance,
    get_available_instances,
    get_running_instances,
    list_ssh_keys,
    wait_until_ready,
)

REMOTE_DIR = "/root/proj"
LABEL_PREFIX = "vrun:"
LAUNCH_GRACE_S = 1800
HEARTBEAT_STALE_S = 90

JOB_SH = (
    "#!/bin/sh\n"
    'cd "$(dirname "$0")"\n'
    "touch HEARTBEAT\n"
    "echo $$ > PGID\n"
    "( trap '' TERM; while :; do touch HEARTBEAT; sleep 20; done ) &\n"
    "HB=$!\n"
    "trap true TERM\n"
    "[ -f .env ] && . ./.env\n"
    "sh run.sh > run.log 2>&1\n"
    "rc=$?\n"
    'kill "$HB" 2>/dev/null\n'
    "echo $rc > DONE\n"
)


def _put_file(inst, name, content):
    b = base64.b64encode(content.encode()).decode()
    remote.run(
        inst,
        f"mkdir -p {REMOTE_DIR} && echo {b} | base64 -d > {REMOTE_DIR}/{name}",
        timeout=60,
    )


def _killgroup(sig):
    return (
        f"P=$(cat {REMOTE_DIR}/PGID 2>/dev/null); "
        f'[ -n "$P" ] && for d in /proc/[0-9]*; do '
        f'g=$(sed "s/.*) //" "$d/stat" 2>/dev/null | cut -d" " -f3); '
        f'[ "$g" = "$P" ] && kill -{sig} "${{d#/proc/}}" 2>/dev/null; '
        "done"
    )


def _local_pubkey():
    for name in ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub"):
        p = os.path.expanduser(f"~/.ssh/{name}")
        if os.path.exists(p):
            return open(p).read().strip()
    raise RuntimeError(
        "no ssh public key in ~/.ssh (id_ed25519/id_rsa/id_ecdsa); run ssh-keygen"
    )


def _key_id(k):
    return " ".join(k.split()[:2])


def _account_pubkeys():
    body = list_ssh_keys()
    items = (
        body
        if isinstance(body, list)
        else (body.get("ssh_keys") or body.get("results") or [])
    )
    return [
        (it.get("public_key") or it.get("key") or "")
        for it in items
        if isinstance(it, dict)
    ]


def _key_on_account(pubkey):
    return any(_key_id(k) == _key_id(pubkey) for k in _account_pubkeys())


def push_dir(inst, local_dir):
    remote.put(inst, local_dir, REMOTE_DIR, files=remote.git_files(local_dir))


def push_path(inst, spec):
    local, _, dest = spec.partition(":")
    local = os.path.expanduser(local)
    if not os.path.exists(local):
        raise RuntimeError(f"--path source not found: {local}")
    if os.path.isdir(local):
        dest = dest or os.path.basename(os.path.normpath(local))
    else:
        dest = dest or "."
    if not dest.startswith("/"):
        dest = f"{REMOTE_DIR}/{dest}"
    remote.put(inst, local, dest)


def destroy_with_retries(inst_id, attempts=5, backoff=3):
    for attempt in range(1, attempts + 1):
        try:
            delete_instance(inst_id)
            print(f"[teardown] destroyed instance {inst_id}")
            return True
        except Exception as e:
            if "no_such_instance" in str(e):
                print(f"[teardown] instance {inst_id} already gone")
                return True
            print(f"[teardown] destroy attempt {attempt}/{attempts} failed: {e}")
            time.sleep(backoff * attempt)
    print("=" * 70)
    print(f"!!! FAILED TO DESTROY INSTANCE {inst_id} - IT MAY STILL BE BILLING !!!")
    print(
        f"  curl -X DELETE "
        f"'https://cloud.vast.ai/api/v0/instances/{inst_id}/?api_key=$VAST_API_KEY'"
    )
    print("=" * 70)
    return False


def _confirm(prompt):
    return input(prompt).strip().lower() in ("y", "yes")


def _rent_offer(filter: AvailableInstancesFilter, options, label, log):
    for offer in get_available_instances(filter):
        log(
            f"offer {offer['id']} ({offer['num_gpus']}x {offer['gpu']}, "
            f"${offer['price']}/h)"
        )
        try:
            return create_instance(offer["id"], options, label=LABEL_PREFIX + label)
        except RuntimeError as e:
            if "no_such_ask" in str(e) or "not available" in str(e):
                log("offer taken between search and create; trying next")
                continue
            raise
    raise RuntimeError("no available offer could be rented (all matches taken)")


def _provision_reachable(filter, options, label, log, pubkey, on_account, attempts=3):
    for attempt in range(1, attempts + 1):
        inst_id = _rent_offer(filter, options, label, log)
        try:
            if not on_account:
                attach_ssh_key(inst_id, pubkey)
                log(f"ssh key attached to {inst_id}")
            log(f"created {inst_id}, waiting for ssh...")
            inst = wait_until_ready(inst_id, log=log)
            remote.wait(inst, log=log)
            return inst_id, inst
        except KeyboardInterrupt:
            log(f"interrupted; destroying {inst_id}")
            destroy_with_retries(inst_id)
            raise
        except Exception as e:
            log(f"node {inst_id} unreachable ({e!r}); destroying")
            destroy_with_retries(inst_id)
            if attempt == attempts:
                raise
            log(f"trying a fresh offer ({attempt + 1}/{attempts})")
    raise RuntimeError("no reachable node within attempt budget")


def _push_and_start(inst, local_dir, cmd, job, setup, paths, log):
    log("writing JOB marker...")
    b = base64.b64encode(json.dumps(job).encode()).decode()
    remote.run(
        inst,
        f"mkdir -p {REMOTE_DIR} && echo {b} | base64 -d > {REMOTE_DIR}/JOB",
        timeout=60,
    )
    log("pushing project files...")
    push_dir(inst, local_dir)
    for spec in paths or []:
        log(f"pushing {spec}")
        push_path(inst, spec)
    if setup:
        log("running setup...")
        rc = remote.shell(inst, f"cd {REMOTE_DIR} && {setup}")
        if rc != 0:
            raise RuntimeError(f"setup failed (rc={rc})")
    log("starting detached job...")
    _start_detached(inst, cmd)


def _start_detached(inst, cmd):
    _put_file(inst, "run.sh", cmd)
    _put_file(inst, "job.sh", JOB_SH)
    remote.run(
        inst,
        f"cd {REMOTE_DIR} && setsid sh job.sh </dev/null >/dev/null 2>&1 & exit 0",
        timeout=30,
    )
    for _ in range(10):
        pgid = remote.run(inst, f"cat {REMOTE_DIR}/PGID 2>/dev/null", timeout=30)[1]
        if pgid.strip():
            return
        time.sleep(1)
    raise RuntimeError("failed to start detached job (no PGID marker)")


def launch(
    src,
    cmd,
    filter: AvailableInstancesFilter,
    options: InstanceOptions,
    label,
    setup=None,
    grace=900,
    max_age=86400,
    drain=1800,
    paths=None,
):
    def log(m):
        print(f"[launch:{label}] {m}")

    local_dir = src if os.path.isdir(src) else os.path.dirname(os.path.abspath(src))
    job = {
        "label": label,
        "cmd": cmd,
        "launched_at": int(time.time()),
        "grace_s": grace,
        "max_age_s": max_age,
        "drain_s": drain,
    }
    existing = _find_existing(label)
    if existing:
        ids = ", ".join(str(i["id"]) for i in existing)
        raise RuntimeError(
            f"a node labelled {LABEL_PREFIX}{label} already exists ({ids}); "
            f"destroy it or pick a new --label before relaunching"
        )
    pubkey = _local_pubkey()
    on_account = _key_on_account(pubkey)
    if not on_account:
        log(f"this ssh key is NOT on your vast account:\n  {pubkey}")
        if not _confirm(f"[launch:{label}] attach it so the node is reachable? [y/N] "):
            raise RuntimeError(
                "aborted: no ssh key attached, the node would be unreachable"
            )
    inst_id, inst = _provision_reachable(
        filter, options, label, log, pubkey, on_account
    )
    try:
        log("ssh up")
        _push_and_start(inst, local_dir, cmd, job, setup, paths, log)
    except (Exception, KeyboardInterrupt) as e:
        log(f"launch aborted ({e!r}); destroying node")
        destroy_with_retries(inst_id)
        raise
    log(f"job running on {inst_id} ({inst['ssh']}); the reaper owns teardown now")
    return inst_id


def _reset_node(inst, log):
    log("killing previous job and clearing markers...")
    remote.run(
        inst,
        f"{_killgroup('KILL')}; "
        f"rm -f {REMOTE_DIR}/DONE {REMOTE_DIR}/SHUTDOWN "
        f"{REMOTE_DIR}/DRAINING {REMOTE_DIR}/PGID {REMOTE_DIR}/HEARTBEAT",
        timeout=30,
    )


def rerun(label, src=".", setup=None, paths=None, cmd=None, grace=None,
          max_age=None, drain=None):
    def log(m):
        print(f"[rerun:{label}] {m}")

    existing = _find_existing(label)
    if not existing:
        raise RuntimeError(
            f"no running node labelled {LABEL_PREFIX}{label}; use launch first"
        )
    if len(existing) > 1:
        ids = ", ".join(str(i["id"]) for i in existing)
        raise RuntimeError(
            f"multiple nodes labelled {LABEL_PREFIX}{label}: {ids}; destroy extras"
        )
    inst = existing[0]
    prev = _probe(inst)
    if not prev or not prev["job"]:
        raise RuntimeError("node has no JOB marker; use launch instead")
    job = prev["job"]
    cmd = cmd or job["cmd"]
    local_dir = src if os.path.isdir(src) else os.path.dirname(os.path.abspath(src))
    job = {
        "label": label,
        "cmd": cmd,
        "launched_at": int(time.time()),
        "grace_s": grace if grace is not None else job["grace_s"],
        "max_age_s": max_age if max_age is not None else job["max_age_s"],
        "drain_s": drain if drain is not None else job["drain_s"],
    }
    _reset_node(inst, log)
    _push_and_start(inst, local_dir, cmd, job, setup, paths, log)
    log(f"rerunning on {inst['id']} ({inst['ssh']}); cmd: {cmd}")
    return inst["id"]


PROBE = (
    f"J=$(base64 {REMOTE_DIR}/JOB 2>/dev/null | tr -d '\\n'); "
    f"D=$(cat {REMOTE_DIR}/DONE 2>/dev/null || echo -); "
    f"M=$(stat -c %Y {REMOTE_DIR}/DONE 2>/dev/null || echo -); "
    f"H=$([ -f {REMOTE_DIR}/HOLD ] && echo 1 || echo 0); "
    f"R=$(cat {REMOTE_DIR}/DRAINING 2>/dev/null || echo -); "
    f"P=$(cat {REMOTE_DIR}/PGID 2>/dev/null); "
    f'G=$([ -n "$P" ] && echo 1 || echo 0); '
    f"HB=$(stat -c %Y {REMOTE_DIR}/HEARTBEAT 2>/dev/null || echo -); "
    f'A=$([ "$HB" != "-" ] && echo $(($(date +%s) - HB)) || echo -); '
    f"C=$(cat {REMOTE_DIR}/RESTARTS 2>/dev/null || echo 0); "
    f"printf 'job=%s\\ndone=%s\\nmtime=%s\\nhold=%s\\n"
    f"drain=%s\\nhas_pgid=%s\\nhb_age=%s\\nrestarts=%s\\n' "
    f'"$J" "$D" "$M" "$H" "$R" "$G" "$A" "$C"'
)


def _probe(inst):
    rc, out, _ = remote.run(inst, PROBE, timeout=45)
    if rc != 0:
        return None
    try:
        f = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        job = json.loads(base64.b64decode(f["job"])) if f["job"] else None
        hb = f["hb_age"]
        return {
            "job": job,
            "done": f["done"],
            "mtime": f["mtime"],
            "hold": f["hold"] == "1",
            "drain": f["drain"],
            "has_pgid": f["has_pgid"] == "1",
            "alive": hb != "-" and int(hb) < HEARTBEAT_STALE_S,
            "restarts": int(f["restarts"] or 0),
        }
    except Exception:
        return None


def _signal_shutdown(inst, deadline):
    remote.run(
        inst,
        f"{_killgroup('TERM')}; "
        f"touch {REMOTE_DIR}/SHUTDOWN; echo {deadline} > {REMOTE_DIR}/DRAINING",
    )


def _ours(inst):
    return (inst.get("label") or "").startswith(LABEL_PREFIX)


def _find_existing(label):
    want = LABEL_PREFIX + label
    return [i for i in get_running_instances() if (i.get("label") or "") == want]


def exec_on(label, cmd=None):
    matches = _find_existing(label)
    if not matches:
        raise RuntimeError(f"no running node labelled {label!r}")
    if len(matches) > 1:
        ids = ", ".join(str(i["id"]) for i in matches)
        raise RuntimeError(f"multiple nodes labelled {label!r}: {ids}")
    inst = matches[0]
    if cmd:
        return remote.shell(inst, f"cd {REMOTE_DIR} && {cmd}")
    return remote.shell(inst, f"cd {REMOTE_DIR}; exec bash -l", tty=True)


def _managed():
    for inst in get_running_instances():
        if _ours(inst):
            yield inst, _probe(inst)


def _state(p):
    if p is None:
        return "unreachable"
    if p["job"] is None:
        return "half-launched"
    if p["drain"] != "-":
        return "draining"
    if p["done"] == "-":
        return "running"
    return "exited"


def _decide_running(p, now, max_restarts):
    j = p["job"]
    if not p["has_pgid"]:
        if now - j["launched_at"] > LAUNCH_GRACE_S:
            return "destroy", "no PGID long after launch (launch died)"
        return None, "launching, no PGID yet"
    if not p["alive"]:
        if p["restarts"] < max_restarts:
            return "restart", (
                f"job died with no DONE, relaunching "
                f"({p['restarts'] + 1}/{max_restarts})"
            )
        if p["hold"]:
            return None, f"died, exhausted {max_restarts} restarts, HOLD set"
        return "destroy", f"died, exhausted {max_restarts} restarts"
    if now - j["launched_at"] > j["max_age_s"]:
        return "signal", "exceeded max_age, sending shutdown heads-up"
    return None, None


def _decide(inst, p, now, default_max_age, max_restarts):
    state = _state(p)
    if state == "unreachable":
        age = now - int(inst["start_date"]) if inst.get("start_date") else 0
        return (
            ("destroy", "unreachable > max_age")
            if age > default_max_age
            else (None, "unreachable, skipping")
        )
    if state == "half-launched":
        age = now - int(inst["start_date"]) if inst.get("start_date") else 0
        if age > LAUNCH_GRACE_S:
            return "destroy", "half-launched (no JOB) past grace"
        return None, "half-launched, within launch grace"
    if state == "draining":
        if p["done"] != "-":
            return "destroy", f"exited (code {p['done']}) after heads-up"
        if now >= int(p["drain"]):
            return "destroy", "ignored SIGTERM past deadline"
        return None, f"draining, {int(p['drain']) - now}s left"
    if state == "running":
        return _decide_running(p, now, max_restarts)
    code, grace, ago = int(p["done"]), p["job"]["grace_s"], now - int(p["mtime"])
    if code == 0:
        return "destroy", "succeeded"
    if p["hold"]:
        return None, "failed but HOLD set, leaving"
    if ago > grace:
        return "destroy", f"failed >{grace}s ago"
    return None, f"failed (exit {code}), {grace - ago}s grace left"


def _restart_job(inst, cmd):
    remote.run(
        inst,
        f"{_killgroup('KILL')}; "
        f"cd {REMOTE_DIR} && rm -f DONE SHUTDOWN DRAINING PGID HEARTBEAT && "
        f"C=$(cat RESTARTS 2>/dev/null || echo 0) && echo $((C + 1)) > RESTARTS",
        timeout=30,
    )
    _start_detached(inst, cmd)


def _reap_one(inst, now, default_max_age, max_restarts):
    p = _probe(inst)
    label = p["job"]["label"] if p and p["job"] else inst.get("label")
    action, msg = _decide(inst, p, now, default_max_age, max_restarts)
    if msg:
        print(f"[reap] {label} {msg}{', destroying' if action == 'destroy' else ''}")
    if action == "destroy":
        destroy_with_retries(inst["id"])
    elif action == "signal":
        _signal_shutdown(inst, now + p["job"]["drain_s"])
    elif action == "restart":
        _restart_job(inst, p["job"]["cmd"])


def reap(default_max_age=86400, max_restarts=3):
    now = int(time.time())
    managed = failed = 0
    for inst in get_running_instances():
        if not _ours(inst):
            continue
        managed += 1
        try:
            _reap_one(inst, now, default_max_age, max_restarts)
        except Exception as e:
            failed += 1
            print(f"[reap] {inst['id']} error, skipping this tick: {e!r}")
    if managed and failed == managed:
        raise RuntimeError(
            f"reap failed on all {managed} managed node(s); environment likely "
            f"broken (is ssh/tar on the service PATH?)"
        )


def list_gpus(max_price=1000.0, min_gpu=1):
    cheapest = {}
    filter = AvailableInstancesFilter(min_gpu=min_gpu, max_dollar_price_hour=max_price)
    for o in get_available_instances(filter):
        name = o["gpu"]
        if name not in cheapest or o["price"] < cheapest[name]:
            cheapest[name] = o["price"]
    for name, price in sorted(cheapest.items()):
        print(f"{name:20} from ${price:.3f}/h")


def clean(force=False):
    targets = [i for i in get_running_instances() if _ours(i)]
    if not targets:
        print("[clean] no managed instances")
        return
    for inst in targets:
        label = (inst.get("label") or "")[len(LABEL_PREFIX) :]
        print(f"[clean] {inst['id']}  {label}")
    if not force and not _confirm(f"destroy these {len(targets)} instance(s)? [y/N] "):
        print("[clean] aborted")
        return
    for inst in targets:
        destroy_with_retries(inst["id"])


def _fmt_dur(s):
    if s <= 0:
        return "expired"
    h, rem = divmod(s, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m}m" if m else f"{s}s"


def _lifetime(p, now):
    if not p or not p["job"]:
        return "-"
    j = p["job"]
    return _fmt_dur(j["max_age_s"] - (now - j["launched_at"]))


def _age(inst, p, now):
    if p and p["job"]:
        return _fmt_dur(now - p["job"]["launched_at"])
    if inst.get("start_date"):
        return _fmt_dur(now - int(inst["start_date"]))
    return "-"


def ps():
    now = int(time.time())
    for inst, p in _managed():
        state = _state(p)
        if state == "exited":
            state += f"({p['done']})" + (" HOLD" if p["hold"] else "")
        label = (inst.get("label") or "")[len(LABEL_PREFIX) :]
        age, left = _age(inst, p, now), _lifetime(p, now)
        restarts = f" restarts {p['restarts']}" if p and p.get("restarts") else ""
        print(
            f"{inst['id']:>10}  {label:<20} {state:<18} "
            f"up {age:<7} left {left}{restarts}"
        )
