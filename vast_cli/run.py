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
    dest = dest or os.path.basename(os.path.normpath(local))
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
            print(f"[teardown] destroy attempt {attempt}/{attempts} failed: {e}")
            time.sleep(backoff * attempt)
    key = os.environ.get("VAST_API_KEY", "$VAST_API_KEY")
    print("=" * 70)
    print(f"!!! FAILED TO DESTROY INSTANCE {inst_id} - IT MAY STILL BE BILLING !!!")
    print(
        f"  curl -X DELETE 'https://cloud.vast.ai/api/v0/instances/{inst_id}/?api_key={key}'"
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
        log("ssh up; pushing project files...")
        push_dir(inst, local_dir)
        for spec in paths or []:
            log(f"pushing {spec}")
            push_path(inst, spec)
        if setup:
            log("running setup...")
            rc, out, err = remote.run(inst, f"cd {REMOTE_DIR} && {setup}", timeout=1800)
            if rc != 0:
                raise RuntimeError(f"setup failed (rc={rc}): {err or out}")
        log("writing JOB marker and starting detached job...")
        b = base64.b64encode(json.dumps(job).encode()).decode()
        remote.run(inst, f"echo {b} | base64 -d > {REMOTE_DIR}/JOB", timeout=60)
        start = (
            f"cd {REMOTE_DIR} && "
            f'setsid sh -c \'echo $$ > PGID; trap "" TERM; '
            f"[ -f .env ] && . ./.env; "
            f"{cmd} > run.log 2>&1; echo $? > DONE' "
            f"</dev/null >/dev/null 2>&1 & exit 0"
        )
        remote.run(inst, start, timeout=30)
        pgid = ""
        for _ in range(10):
            pgid = remote.run(inst, f"cat {REMOTE_DIR}/PGID 2>/dev/null", timeout=30)[1]
            if pgid.strip():
                break
            time.sleep(1)
        if not pgid.strip():
            raise RuntimeError("failed to start detached job (no PGID marker)")
    except (Exception, KeyboardInterrupt) as e:
        log(f"launch aborted ({e!r}); destroying node")
        destroy_with_retries(inst_id)
        raise
    log(f"job running on {inst_id} ({inst['ssh']}); the reaper owns teardown now")
    return inst_id


PROBE = (
    f"J=$(base64 {REMOTE_DIR}/JOB 2>/dev/null | tr -d '\\n'); "
    f"D=$(cat {REMOTE_DIR}/DONE 2>/dev/null || echo -); "
    f"M=$(stat -c %Y {REMOTE_DIR}/DONE 2>/dev/null || echo -); "
    f"H=$([ -f {REMOTE_DIR}/HOLD ] && echo 1 || echo 0); "
    f"R=$(cat {REMOTE_DIR}/DRAINING 2>/dev/null || echo -); "
    f'printf \'%s|%s|%s|%s|%s\' "$J" "$D" "$M" "$H" "$R"'
)


def _probe(inst):
    rc, out, _ = remote.run(inst, PROBE)
    if rc != 0:
        return None
    j, done, mtime, hold, drain = [*out.split("|"), "", "-", "-", "0", "-"][:5]
    job = json.loads(base64.b64decode(j)) if j else None
    return {
        "job": job,
        "done": done,
        "mtime": mtime,
        "hold": hold == "1",
        "drain": drain,
    }


def _signal_shutdown(inst, deadline):
    remote.run(
        inst,
        f"kill -TERM -$(cat {REMOTE_DIR}/PGID) 2>/dev/null; "
        f"touch {REMOTE_DIR}/SHUTDOWN; echo {deadline} > {REMOTE_DIR}/DRAINING",
    )


def _ours(inst):
    return (inst.get("label") or "").startswith(LABEL_PREFIX)


def _find_existing(label):
    want = LABEL_PREFIX + label
    return [i for i in get_running_instances() if (i.get("label") or "") == want]


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


def _decide(inst, p, now, default_max_age):
    state = _state(p)
    if state == "unreachable":
        age = now - int(inst["start_date"]) if inst.get("start_date") else 0
        return (
            ("destroy", "unreachable > max_age")
            if age > default_max_age
            else (None, "unreachable, skipping")
        )
    if state == "half-launched":
        return "destroy", "half-launched (no JOB)"
    if state == "draining":
        if p["done"] != "-":
            return "destroy", f"exited (code {p['done']}) after heads-up"
        if now >= int(p["drain"]):
            return "destroy", "ignored SIGTERM past deadline"
        return None, f"draining, {int(p['drain']) - now}s left"
    if state == "running":
        j = p["job"]
        if now - j["launched_at"] > j["max_age_s"]:
            return "signal", "exceeded max_age, sending shutdown heads-up"
        return None, None
    code, grace, ago = int(p["done"]), p["job"]["grace_s"], now - int(p["mtime"])
    if code == 0:
        return "destroy", "succeeded"
    if p["hold"]:
        return None, "failed but HOLD set, leaving"
    if ago > grace:
        return "destroy", f"failed >{grace}s ago"
    return None, f"failed (exit {code}), {grace - ago}s grace left"


def _reap_one(inst, now, default_max_age):
    p = _probe(inst)
    label = p["job"]["label"] if p and p["job"] else inst.get("label")
    action, msg = _decide(inst, p, now, default_max_age)
    if msg:
        print(f"[reap] {label} {msg}{', destroying' if action == 'destroy' else ''}")
    if action == "destroy":
        destroy_with_retries(inst["id"])
    elif action == "signal":
        _signal_shutdown(inst, now + p["job"]["drain_s"])


def reap(default_max_age=86400):
    now = int(time.time())
    for inst in get_running_instances():
        if not _ours(inst):
            continue
        try:
            _reap_one(inst, now, default_max_age)
        except Exception as e:
            print(f"[reap] {inst['id']} error, skipping this tick: {e!r}")


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


def ps():
    for inst, p in _managed():
        state = _state(p)
        if state == "exited":
            state += f"({p['done']})" + (" HOLD" if p["hold"] else "")
        label = (inst.get("label") or "")[len(LABEL_PREFIX) :]
        print(f"{inst['id']:>10}  {label:<20} {state}")
