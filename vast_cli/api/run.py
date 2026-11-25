import base64
import dataclasses
import json
import logging
import os
import sys
import tarfile
import time

from vast_cli.api import remote
from vast_cli.api.client import (
    OFFER_LIMIT,
    AvailableInstancesFilter,
    InstanceOptions,
    add_account_ssh_key,
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
DEFAULT_MAX_AGE_S = 86400
BACKSTOP_SLACK_S = 3600
UNREACHABLE_PROBES = 6
UNREACHABLE_S = 1500
SUPERVISED_FRESH_S = 300
HEARTBEAT_STALE_S = 90
RECYCLE_EXIT = "75"
MAX_RECYCLES = 3
SUPERVISE_POLL_S = int(os.environ.get("VAST_SUPERVISE_POLL_S", "20"))
SSH_LOST_S = 600
LOG_CHUNK = 1048576

logger = logging.getLogger("vast_cli")

JOB_SH = (
    "#!/bin/sh\n"
    'cd "$(dirname "$0")"\n'
    "touch HEARTBEAT\n"
    "echo $$ > PGID.tmp && mv -f PGID.tmp PGID\n"
    "( trap '' TERM; while :; do touch HEARTBEAT; sleep 20; done ) &\n"
    "HB=$!\n"
    "trap true TERM\n"
    "[ -f .env ] && . ./.env\n"
    f"export VAST_RECYCLE_EXIT={RECYCLE_EXIT}\n"
    "export VAST_JOB=1\n"
    "sh run.sh > run.log 2>&1\n"
    "rc=$?\n"
    'kill -KILL "$HB" 2>/dev/null\n'
    "echo $rc > EXIT_CODE.tmp && mv -f EXIT_CODE.tmp EXIT_CODE\n"
)


def _put_file(inst, name, content):
    b = base64.b64encode(content.encode()).decode()
    return remote.run(
        inst,
        f"mkdir -p {REMOTE_DIR} && T={REMOTE_DIR}/.{name}.$$ && "
        f"echo {b} | base64 -d > $T && mv -f $T {REMOTE_DIR}/{name}",
        timeout=60,
    )[0]


def _write_job(inst, job):
    if _put_file(inst, "JOB", json.dumps(job)) != 0:
        raise RuntimeError("failed to write JOB marker")


def _killgroup(sig):
    return (
        f"P=$(cat {REMOTE_DIR}/PGID 2>/dev/null); "
        f"for d in /proc/[0-9]*; do "
        f'g=$(sed "s/.*) //" "$d/stat" 2>/dev/null | cut -d" " -f3); '
        f'{{ [ -n "$P" ] && [ "$g" = "$P" ] || '
        f'tr "\\0" "\\n" < "$d/environ" 2>/dev/null | grep -qx VAST_JOB=1; }} '
        f'&& kill -{sig} "${{d#/proc/}}" 2>/dev/null; '
        "done"
    )


def local_pubkey():
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


def key_on_account(pubkey):
    return any(_key_id(k) == _key_id(pubkey) for k in _account_pubkeys())


def push_dir(inst, src):
    local = src if os.path.isdir(src) else os.path.dirname(os.path.abspath(src))
    remote.put(inst, local, REMOTE_DIR, files=remote.git_files(local))


def push_path(inst, src, spec):
    local, dest = _split_path_spec(spec)
    files = None
    if os.path.isdir(local):
        files = []
        for root, _, names in os.walk(local):
            for n in names:
                rel = os.path.relpath(os.path.join(root, n), local)
                if not remote.excluded(_arcname(dest, rel), src):
                    files.append(rel)
    if not dest.startswith("/"):
        dest = f"{REMOTE_DIR}/{dest}"
    remote.put(inst, local, dest, files=files)


def _split_path_spec(spec):
    local, _, dest = spec.partition(":")
    local = os.path.expanduser(local)
    if not os.path.exists(local):
        raise RuntimeError(f"--path source not found: {local}")
    if os.path.isdir(local):
        return local, dest or os.path.basename(os.path.normpath(local))
    return local, dest or "."


def _arcname(dest, name):
    arc = os.path.normpath(os.path.join(dest, name))
    if os.path.isabs(dest) or arc == ".." or arc.startswith(".." + os.sep):
        raise RuntimeError(f"--path destination escapes the project dir: {dest}")
    return arc


def _overlay(members, src, spec):
    local, dest = _split_path_spec(spec)
    if not os.path.isdir(local):
        members[_arcname(dest, os.path.basename(local))] = local
        return
    for root, _, names in os.walk(local):
        for n in names:
            p = os.path.join(root, n)
            arc = _arcname(dest, os.path.relpath(p, local))
            if remote.excluded(arc, src):
                continue
            members[arc] = p


def bundle(src, out, paths=()):
    tracked = remote.git_files(src)
    if tracked is None:
        raise RuntimeError(f"bundle: {src} is not a git repository")
    if not tracked:
        raise RuntimeError(f"bundle: no files to ship from {src}")
    members = {f: os.path.join(src, f) for f in tracked if not remote.excluded(f, src)}
    for spec in paths or []:
        _overlay(members, src, spec)
    if out == "-":
        _write_bundle(sys.stdout.buffer, members)
    else:
        with open(out, "wb") as fh:
            _write_bundle(fh, members)
    return len(members)


def _write_bundle(stream, members):
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for arc, local in sorted(members.items()):
            tar.add(local, arcname=f"./{arc}", recursive=False)


def destroy_with_retries(inst_id, attempts=5, backoff=3):
    for attempt in range(1, attempts + 1):
        try:
            delete_instance(inst_id)
            logger.info("[teardown] destroyed instance %s", inst_id)
            return True
        except Exception as e:
            if "no_such_instance" in str(e):
                logger.info("[teardown] instance %s already gone", inst_id)
                return True
            logger.warning(
                "[teardown] destroy attempt %s/%s failed: %s", attempt, attempts, e
            )
            time.sleep(backoff * attempt)
    logger.error("%s", "=" * 70)
    logger.error(
        "!!! FAILED TO DESTROY INSTANCE %s - IT MAY STILL BE BILLING !!!", inst_id
    )
    logger.error(
        "  curl -X DELETE "
        "'https://cloud.vast.ai/api/v0/instances/%s/?api_key=$VAST_API_KEY'",
        inst_id,
    )
    logger.error("%s", "=" * 70)
    return False


SKIP_CACHE = os.path.expanduser(
    os.environ.get("VAST_SKIP_CACHE", "~/.cache/vast/bad_machines.jsonl")
)
SKIP_TTL = 7 * 86400


def bad_machine_cache(add=None, ttl=SKIP_TTL):
    now = int(time.time())
    rows = []
    try:
        with open(SKIP_CACHE) as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except (OSError, ValueError):
        pass
    rows = [r for r in rows if now - r["ts"] < ttl and r["machine_id"] != add]
    if add is not None:
        rows.append({"machine_id": add, "ts": now})
        os.makedirs(os.path.dirname(SKIP_CACHE), exist_ok=True)
        tmp = f"{SKIP_CACHE}.{os.getpid()}"
        with open(tmp, "w") as fh:
            fh.writelines(json.dumps(r) + "\n" for r in rows)
        os.replace(tmp, SKIP_CACHE)
    return {r["machine_id"] for r in rows}


def _blocking_filters(filters: AvailableInstancesFilter):
    relaxed = {
        "min_cuda": 0,
        "direct_port": False,
        "min_reliability": 0.0,
        "mbps_down": 0,
        "mbps_up": 0,
        "min_disk_space_gb": 0,
        "countries": (),
        "verified": False,
        "max_dollar_price_hour": filters.max_dollar_price_hour * 2,
    }
    counts = {
        f"{name}={off}": len(
            list(get_available_instances(dataclasses.replace(filters, **{name: off})))
        )
        for name, off in relaxed.items()
        if getattr(filters, name) != off
    }
    return {name: n for name, n in counts.items() if n}


def _rent_offer(filters: AvailableInstancesFilter, options, label, log, skip=()):
    tried = skipped = 0
    for offer in get_available_instances(filters):
        if offer["machine_id"] in skip:
            skipped += 1
            continue
        tried += 1
        log(
            f"offer {offer['id']} ({offer['num_gpus']}x {offer['gpu']}, "
            f"${offer['price']}/h)"
        )
        try:
            inst_id = create_instance(offer["id"], options, label=LABEL_PREFIX + label)
            return inst_id, offer["machine_id"]
        except RuntimeError as e:
            if "no_such_ask" in str(e) or "not available" in str(e):
                log("offer taken between search and create; trying next")
                continue
            log(f"create failed: {e}")
            raise
    if tried:
        raise RuntimeError(f"all {tried} matching offers were taken")
    if skipped:
        raise RuntimeError(
            f"all {skipped} matching offers are on machines that failed recently"
        )
    blocking = _blocking_filters(filters)
    if not blocking:
        raise RuntimeError(
            f"no {filters.gpu_name or 'GPU'} offer, even with each filter relaxed"
        )
    found = ", ".join(f"{name} gives {n}" for name, n in blocking.items())
    raise RuntimeError(f"no offer matched the filter; {found}")


REAP_STATE = os.path.expanduser(
    os.environ.get("VAST_REAP_STATE", "~/.cache/vast/reap_state.json")
)


def _load_reap_state():
    try:
        with open(REAP_STATE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _save_reap_state(state):
    os.makedirs(os.path.dirname(REAP_STATE), exist_ok=True)
    tmp = f"{REAP_STATE}.{os.getpid()}"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, REAP_STATE)


def _account(seen, p, now):
    if p is None:
        return {
            **seen,
            "fails": seen.get("fails", 0) + 1,
            "since": seen.get("since", now),
        }
    job = p["job"]
    return {"deadline": job["launched_at"] + job["max_age_s"]} if job else {}


def _wait_gone(inst_id, timeout=300, poll=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(i["id"] == inst_id for i in get_running_instances()):
            return True
        time.sleep(poll)
    return False


def _destroy_gone(inst_id):
    destroy_with_retries(inst_id)
    if not _wait_gone(inst_id):
        raise RuntimeError(f"{inst_id} is still listed after destroy")


class SetupFailed(RuntimeError):
    pass


def _pull_log(inst, off):
    f = f"{REMOTE_DIR}/run.log"
    rc, out, _ = remote.run(
        inst,
        f"tail -c +{off} {f} 2>/dev/null | head -c {LOG_CHUNK}",
        timeout=60,
        text=False,
    )
    if rc != 0:
        return off, b""
    return off + len(out), out


def _stream_log(inst, off, drain=False):
    while True:
        off, chunk = _pull_log(inst, off)
        sys.stdout.buffer.write(chunk)
        sys.stdout.flush()
        if not drain or not chunk:
            return off


def _api_status(inst_id):
    try:
        return next(
            (i["status"] for i in get_running_instances() if i["id"] == inst_id),
            "gone",
        )
    except Exception:
        return None


def _supervise(inst, log):
    off, lost_since = 1, None
    while True:
        time.sleep(SUPERVISE_POLL_S)
        p = _probe(inst, claim=True)
        if p is None:
            status = _api_status(inst["id"])
            if status in ("gone", "exited"):
                return "gone", f"node is {status}"
            if status is not None:
                lost_since = lost_since or time.time()
                if time.time() - lost_since > SSH_LOST_S:
                    return "lost", f"no ssh for {SSH_LOST_S}s while {status}"
            continue
        lost_since = None
        action, why = _decide(inst, p, int(time.time()), DEFAULT_MAX_AGE_S)
        off = _stream_log(inst, off, drain=action is not None or p["exit_code"] != "-")
        if action == "replace":
            return "recycle", why
        if p["exit_code"] != "-":
            return (
                "done" if p["exit_code"] == "0" else "failed"
            ), f"rc={p['exit_code']}"
        if action == "destroy":
            return "dead", why


def _watch(inst, log):
    verdict, why = _supervise(inst, log)
    if verdict == "failed":
        log(f"job failed ({why}); the reaper keeps the node for its grace period")
        return 1
    log(f"destroying {inst['id']}: {why}")
    if verdict in ("recycle", "lost") and inst.get("machine_id"):
        bad_machine_cache(add=inst["machine_id"])
    _destroy_gone(inst["id"])
    return {"done": 0, "gone": 1, "dead": 1}.get(verdict)


def _provision_reachable(
    filters, options, label, log, node_key=None, attempts=3, then=None
):
    bad_machines = bad_machine_cache()
    if bad_machines:
        log(f"skipping {len(bad_machines)} machines that failed in the last 7 days")
    last_failure = None
    for attempt in range(1, attempts + 1):
        try:
            inst_id, machine_id = _rent_offer(
                filters, options, label, log, bad_machines
            )
        except RuntimeError as e:
            if last_failure is None:
                raise
            log(f"no offer left outside the {len(bad_machines)} machines that failed")
            raise last_failure from e
        try:
            if node_key:
                attach_ssh_key(inst_id, node_key)
                log(f"ssh key attached to {inst_id} only")
            log(f"created {inst_id}, waiting for ssh...")
            inst = wait_until_ready(inst_id, log=log)
            remote.wait(inst, log=log)
            log("ssh up")
            if then:
                then(inst)
            return inst_id, inst
        except (KeyboardInterrupt, SetupFailed):
            log(f"stopped; destroying {inst_id}")
            destroy_with_retries(inst_id)
            _wait_gone(inst_id)
            raise
        except Exception as e:
            log(f"node {inst_id} on machine {machine_id} failed ({e!r}); destroying")
            last_failure = e
            bad_machines.add(machine_id)
            bad_machine_cache(add=machine_id)
            destroy_with_retries(inst_id)
            if attempt == attempts:
                raise
            log(f"trying a fresh offer ({attempt + 1}/{attempts})")
    raise RuntimeError("no reachable node within attempt budget")


def _push_and_start(inst, src, cmd, job, setup, paths, log, bundle=None):
    log("writing JOB marker...")
    _write_job(inst, job)
    if bundle:
        log(f"pushing bundle {bundle}...")
        remote.put_bundle(inst, bundle, REMOTE_DIR)
    else:
        log("pushing project files...")
        push_dir(inst, src)
        for spec in paths or []:
            log(f"pushing {spec}")
            push_path(inst, src, spec)
    if setup:
        log("running setup...")
        rc = remote.shell(inst, f"cd {REMOTE_DIR} && {setup}")
        if rc != 0:
            raise SetupFailed(f"setup failed (rc={rc})")
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


def _job(label, cmd, grace, max_age):
    return {
        "label": label,
        "cmd": cmd,
        "launched_at": int(time.time()),
        "grace_s": grace,
        "max_age_s": max_age,
    }


def launch(
    src,
    cmd,
    filters: AvailableInstancesFilter,
    options: InstanceOptions,
    label,
    setup=None,
    grace=900,
    max_age=86400,
    paths=None,
    attach_missing_key=False,
    bundle=None,
    node_key_only=False,
    detach=False,
):
    def log(m):
        logger.info("[launch:%s] %s", label, m)

    existing = _find_existing(label)
    if existing:
        ids = ", ".join(str(i["id"]) for i in existing)
        raise RuntimeError(
            f"a node labelled {LABEL_PREFIX}{label} already exists ({ids}); "
            f"destroy it or pick a new --label before relaunching"
        )
    pubkey = local_pubkey()
    on_account = key_on_account(pubkey)
    node_key = None
    if not on_account:
        if not attach_missing_key:
            raise RuntimeError(
                f"ssh key not on vast account ({pubkey}); pass "
                f"attach_missing_key=True to add it, else the node "
                f"would be unreachable"
            )
        if node_key_only:
            node_key = pubkey
        else:
            add_account_ssh_key(pubkey)
            log("ssh key added to the vast account")
    recycles = 0
    while True:
        inst_id, inst = _provision_reachable(
            filters,
            options,
            label,
            log,
            node_key,
            then=lambda node: _push_and_start(
                node,
                src,
                cmd,
                _job(label, cmd, grace, max_age),
                setup,
                paths,
                log,
                bundle,
            ),
        )
        log(f"job running on {inst_id} ({inst['ssh']})")
        if detach:
            return 0
        rc = _watch(inst, log)
        if rc is not None:
            return rc
        recycles += 1
        if recycles > MAX_RECYCLES:
            log(f"used its {MAX_RECYCLES} recycles, giving up")
            return 75
        log(f"taking a fresh node ({recycles}/{MAX_RECYCLES})")


def _reset_node(inst, log):
    log("killing previous job and clearing markers...")
    remote.run(
        inst,
        f"{_killgroup('KILL')}; "
        f"rm -f {REMOTE_DIR}/EXIT_CODE {REMOTE_DIR}/PGID {REMOTE_DIR}/HEARTBEAT",
        timeout=30,
    )


def _mismatch(inst, want):
    checks = {
        "gpu": lambda have, asked: have == asked,
        "num_gpus": lambda have, asked: have is not None and have >= asked,
        "disk": lambda have, asked: have is not None and have >= asked,
    }
    return ", ".join(
        f"{name} is {inst.get(name)}, asked for {want[name]}"
        for name, ok in checks.items()
        if want.get(name) is not None and not ok(inst.get(name), want[name])
    )


def rerun(
    label,
    src=".",
    setup=None,
    paths=None,
    cmd=None,
    grace=None,
    max_age=None,
    bundle=None,
    want=None,
    check=False,
    detach=False,
):
    def log(m):
        logger.info("[rerun:%s] %s", label, m)

    existing = _find_existing(label)
    if not existing:
        log("no node with this label; it needs a launch")
        return 75
    if len(existing) > 1:
        ids = ", ".join(str(i["id"]) for i in existing)
        raise RuntimeError(
            f"multiple nodes labelled {LABEL_PREFIX}{label}: {ids}; destroy extras"
        )
    inst = existing[0]
    wrong = _mismatch(inst, want or {})
    if wrong:
        raise RuntimeError(
            f"node {inst['id']} does not fit: {wrong}; destroy it and launch, "
            f"or ask for what it has"
        )
    if check:
        return 0
    prev = _probe(inst, claim=True)
    if not prev or not prev["job"]:
        log(f"{inst['id']} is unreachable or has no job; destroying it")
        _destroy_gone(inst["id"])
        return 75
    old = prev["job"]
    cmd = cmd or old["cmd"]
    job = _job(
        label,
        cmd,
        old["grace_s"] if grace is None else grace,
        old["max_age_s"] if max_age is None else max_age,
    )
    _reset_node(inst, log)
    _push_and_start(inst, src, cmd, job, setup, paths, log, bundle)
    log(f"rerunning on {inst['id']} ({inst['ssh']}); cmd: {cmd}")
    if detach:
        return 0
    rc = _watch(inst, log)
    return 75 if rc is None else rc


PROBE = (
    f"J=$(base64 {REMOTE_DIR}/JOB 2>/dev/null | tr -d '\\n'); "
    f"D=$(cat {REMOTE_DIR}/EXIT_CODE 2>/dev/null || echo -); "
    f"M=$(stat -c %Y {REMOTE_DIR}/EXIT_CODE 2>/dev/null || echo -); "
    f"H=$([ -f {REMOTE_DIR}/HOLD ] && echo 1 || echo 0); "
    f"P=$(cat {REMOTE_DIR}/PGID 2>/dev/null); "
    f'G=$([ -n "$P" ] && echo 1 || echo 0); '
    f"HB=$(stat -c %Y {REMOTE_DIR}/HEARTBEAT 2>/dev/null || echo -); "
    f'A=$([ "$HB" != "-" ] && echo $(($(date +%s) - HB)) || echo -); '
    f"SV=$(stat -c %Y {REMOTE_DIR}/SUPERVISED 2>/dev/null || echo -); "
    f'S=$([ "$SV" != "-" ] && echo $(($(date +%s) - SV)) || echo -); '
    f"U=$(timeout 10 nvidia-smi --query-gpu=utilization.gpu "
    f"--format=csv,noheader,nounits 2>/dev/null | tr '\\n' ',' | sed 's/,$//'); "
    f"printf 'job=%s\\nexit_code=%s\\nmtime=%s\\nhold=%s\\n"
    f"has_pgid=%s\\nhb_age=%s\\ngpu=%s\\n"
    f"sup_age=%s\\n' "
    f'"$J" "$D" "$M" "$H" "$G" "$A" "$U" "$S"'
)


def _probe(inst, claim=False):
    touch = f"touch {REMOTE_DIR}/SUPERVISED 2>/dev/null; " if claim else ""
    rc, out, _ = remote.run_any(inst, touch + PROBE, timeout=45)
    if rc != 0:
        return None
    try:
        f = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        job = json.loads(base64.b64decode(f["job"])) if f["job"] else None
        hb, sup = f["hb_age"], f.get("sup_age", "-")
        return {
            "job": job,
            "exit_code": f["exit_code"],
            "mtime": f["mtime"],
            "hold": f["hold"] == "1",
            "has_pgid": f["has_pgid"] == "1",
            "alive": hb != "-" and int(hb) < HEARTBEAT_STALE_S,
            "gpu": f.get("gpu") or "-",
            "supervised": sup != "-" and int(sup) < SUPERVISED_FRESH_S,
        }
    except Exception:
        return None


def _ours(inst):
    return (inst.get("label") or "").startswith(LABEL_PREFIX)


def _find_existing(label):
    want = LABEL_PREFIX + label
    return [i for i in get_running_instances() if (i.get("label") or "") == want]


def _one(label):
    matches = _find_existing(label)
    if not matches:
        raise RuntimeError(f"no running node labelled {label!r}")
    if len(matches) > 1:
        ids = ", ".join(str(i["id"]) for i in matches)
        raise RuntimeError(f"multiple nodes labelled {label!r}: {ids}")
    return matches[0]


def destroy(label):
    inst = _one(label)
    _destroy_gone(inst["id"])
    return inst["id"]


def set_max_age(label, max_age):
    inst = _one(label)
    p = _probe(inst)
    if not p or not p["job"]:
        raise RuntimeError("node has no JOB marker")
    job = p["job"]
    job["max_age_s"] = max_age
    _write_job(inst, job)
    return {
        "id": inst["id"],
        "label": label,
        "max_age_s": max_age,
        "left_s": _left_s(p, int(time.time())),
    }


def _summary(instances):
    return ", ".join(
        f"{i.get('label') or '(unlabelled)'} (id {i['id']}, {i.get('status')})"
        for i in instances
    )


def resolve(target=None):
    instances = list(get_running_instances())
    live = [i for i in instances if i.get("status") == "running"]
    if target is None:
        if len(live) == 1:
            return live[0]
        if not live:
            raise RuntimeError(f"no running instance: {_summary(instances) or 'none'}")
        raise RuntimeError(f"pick an instance: {_summary(live)}")
    wanted = (target, LABEL_PREFIX + target)
    hits = [i for i in instances if (i.get("label") or "") in wanted]
    if not hits:
        raise RuntimeError(
            f"no instance labelled {target!r}: {_summary(instances) or 'none'}"
        )
    running = [i for i in hits if i.get("status") == "running"]
    if not running:
        raise RuntimeError(f"{target!r} is not running: {_summary(hits)}")
    if len(running) > 1:
        raise RuntimeError(
            f"multiple instances labelled {target!r}: {_summary(running)}"
        )
    return running[0]


def ssh_argv(target=None, ssh_args=(), direct=True):
    return remote.ssh_argv(resolve(target), ssh_args, direct)


def exec_on(label, cmd=None):
    inst = _one(label)
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
    if p["exit_code"] == "-":
        return "running"
    return "exited"


def _decide_running(p, now):
    j = p["job"]
    if not p["has_pgid"]:
        if now - j["launched_at"] > LAUNCH_GRACE_S:
            return "destroy", "no PGID long after launch (launch died)"
        return None, "launching, no PGID yet"
    if not p["alive"]:
        if p["hold"]:
            return None, "died without EXIT_CODE, HOLD set"
        return "replace", "job died without EXIT_CODE"
    if now - j["launched_at"] > j["max_age_s"]:
        return "destroy", "exceeded max_age"
    return None, None


def _inst_age(inst, now):
    sd = inst.get("start_date")
    return now - int(sd) if sd else None


def _decide_unreachable(inst, now, default_max_age, seen):
    age = _inst_age(inst, now)
    deadline = seen.get("deadline")
    if deadline is None:
        if age is None:
            return (
                "warn",
                "unreachable with no start_date; cannot age out, destroy by hand",
            )
        deadline = now - age + default_max_age
    fails, down = seen.get("fails", 0), now - seen.get("since", now)
    what = f"unreachable for {down}s, {fails} failed probes"
    if now <= deadline:
        return None, f"{what}, {deadline - now}s before its limit"
    if fails < UNREACHABLE_PROBES or down < UNREACHABLE_S:
        return None, f"{what}, past its limit, trying again"
    return "destroy", f"{what}, past its limit"


def _decide(inst, p, now, default_max_age, seen=None):
    state = _state(p)
    if state == "unreachable":
        return _decide_unreachable(inst, now, default_max_age, seen or {})
    if state == "half-launched":
        age = _inst_age(inst, now)
        if age is None:
            return (
                "warn",
                "half-launched with no start_date; cannot age out, destroy by hand",
            )
        if age > LAUNCH_GRACE_S:
            return "destroy", "half-launched (no JOB) past grace"
        return None, "half-launched, within launch grace"
    if state == "running":
        return _decide_running(p, now)
    code, grace, ago = int(p["exit_code"]), p["job"]["grace_s"], now - int(p["mtime"])
    if code == 0:
        return "destroy", "succeeded"
    if p["hold"]:
        return None, "failed but HOLD set, leaving"
    if p["exit_code"] == RECYCLE_EXIT:
        return "replace", f"asked for a new node (exit {RECYCLE_EXIT})"
    if ago > grace:
        return "destroy", f"failed >{grace}s ago"
    return None, f"failed (exit {code}), {grace - ago}s grace left"


def _backstop(inst, p, now):
    if p["job"]:
        age = now - p["job"]["launched_at"]
        limit = p["job"]["max_age_s"]
    else:
        age = _inst_age(inst, now)
        limit = LAUNCH_GRACE_S
    if age is None or age <= limit + BACKSTOP_SLACK_S:
        return {"id": inst["id"], "action": None, "msg": "owned by a supervisor"}
    logger.warning("[reap] %s is %ss old, past its backstop limit", inst["id"], age)
    destroy_with_retries(inst["id"])
    return {"id": inst["id"], "action": "destroy", "msg": "past backstop limit"}


def _reap_one(inst, p, now, default_max_age, seen=None):
    label = p["job"]["label"] if p and p["job"] else inst.get("label")
    action, msg = _decide(inst, p, now, default_max_age, seen)
    if action == "warn":
        logger.warning("[reap] %s %s", label, msg)
    elif msg:
        logger.info("[reap] %s %s%s", label, msg, ", destroying" if action else "")
    if action in ("destroy", "replace"):
        destroy_with_retries(inst["id"])
    return {"id": inst["id"], "label": label, "action": action, "msg": msg}


def reap(default_max_age=DEFAULT_MAX_AGE_S):
    now = int(time.time())
    results = []
    old, state = _load_reap_state(), {}
    managed = failed = 0
    for inst in get_running_instances():
        if not _ours(inst):
            continue
        managed += 1
        key = str(inst["id"])
        state[key] = old.get(key, {})
        try:
            p = _probe(inst)
            state[key] = _account(state[key], p, now)
            results.append(
                _backstop(inst, p, now)
                if p and p["supervised"]
                else _reap_one(inst, p, now, default_max_age, state[key])
            )
        except Exception as e:
            failed += 1
            logger.warning("[reap] %s error, skipping this tick: %r", inst["id"], e)
    _save_reap_state(state)
    if managed and failed == managed:
        raise RuntimeError(
            f"reap failed on all {managed} managed node(s); environment likely "
            f"broken (is ssh/tar on the service PATH?)"
        )
    return results


def list_gpus(max_price=1000.0, min_gpu=1, verified=True, min_gpu_ram_gb=0):
    base_filter = AvailableInstancesFilter(
        min_gpu=min_gpu,
        max_dollar_price_hour=max_price,
        verified=verified,
        min_gpu_ram_gb=min_gpu_ram_gb,
    )
    found = {}
    while True:
        page = get_available_instances(
            dataclasses.replace(base_filter, skip_gpus=tuple(found)),
            order=[["dph_total", "asc"]],
        )
        offers = list(page)
        if not offers:
            return sorted(found.items())
        new = {}
        for o in offers:
            if o["gpu"] not in found:
                new.setdefault(o["gpu"], o["price"])
        if not new:
            raise RuntimeError("offer search ignored the gpu_name notin filter")
        found.update(new)
        if len(offers) < OFFER_LIMIT:
            return sorted(found.items())


def list_managed():
    return [i for i in get_running_instances() if _ours(i)]


def clean(targets=None):
    if targets is None:
        targets = list_managed()
    return [i["id"] for i in targets if destroy_with_retries(i["id"])]


def _age_s(inst, p, now):
    if p and p["job"]:
        return now - p["job"]["launched_at"]
    if inst.get("start_date"):
        return now - int(inst["start_date"])
    return None


def _left_s(p, now):
    if not p or not p["job"]:
        return None
    j = p["job"]
    return j["max_age_s"] - (now - j["launched_at"])


def _country(geo):
    return (geo or "").rsplit(",", 1)[-1].strip() or "-"


def ps():
    now = int(time.time())
    rows = []
    for inst, p in _managed():
        rows.append(
            {
                "id": inst["id"],
                "machine_id": inst.get("machine_id") or "-",
                "country": _country(inst.get("geolocation")),
                "label": (inst.get("label") or "")[len(LABEL_PREFIX) :],
                "state": _state(p),
                "exit_code": p["exit_code"] if p else "-",
                "hold": bool(p and p.get("hold")),
                "age_s": _age_s(inst, p, now),
                "left_s": _left_s(p, now),
                "gpu": p["gpu"] if p and p.get("gpu") else "-",
            }
        )
    return rows
