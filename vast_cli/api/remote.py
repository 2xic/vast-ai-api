import hashlib
import logging
import os
import shlex
import subprocess
import tempfile
import time

logger = logging.getLogger("vast_cli")

SSH_OPTS = [
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "ConnectTimeout=15",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=4",
]


def git_files(local_dir):
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                local_dir,
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            capture_output=True,
            check=True,
        ).stdout
    except Exception:
        return None
    return [f for f in out.decode().split("\0") if f]


def addr(inst, direct=False):
    d = inst.get("ssh_direct") if direct else None
    if d:
        return d["host"], d["port"]
    return inst.get("ssh_host"), inst.get("ssh_port")


def _addr_candidates(inst):
    out = []
    for direct in (True, False):
        cand = addr(inst, direct)
        if cand[0] and cand[1] and cand not in out:
            out.append(cand)
    return out


def _mux_dir():
    path = os.path.join(tempfile.gettempdir(), f"vast-ssh-{os.getuid()}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def _mux_opts(inst, host, port):
    key = hashlib.sha256(f"{inst['id']}@{host}:{port}".encode()).hexdigest()[:8]
    sock = os.path.join(_mux_dir(), f"{inst['id']}-{key}")
    return [
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={sock}",
        "-o",
        "ControlPersist=120",
    ]


def _argv(inst, host, port, ssh_args=()):
    if not host or not port:
        raise RuntimeError(f"instance {inst['id']} has no ssh address yet")
    return [
        "ssh",
        *SSH_OPTS,
        *_mux_opts(inst, host, port),
        "-p",
        str(port),
        f"root@{host}",
        *ssh_args,
    ]


def base(inst):
    host, port = inst.get("ssh_addr") or addr(inst)
    return _argv(inst, host, port)


def ssh_argv(inst, ssh_args=(), direct=False):
    return _argv(inst, *addr(inst, direct), ssh_args)


def shell(inst, cmd, tty=False):
    cmd_ssh, *rest = base(inst)
    opts = ["-t"] if tty else []
    return subprocess.call([cmd_ssh, *opts, *rest, cmd])


def run(inst, cmd, timeout=None):
    try:
        p = subprocess.run(
            [*base(inst), cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    return p.returncode, p.stdout, p.stderr


def _probe(inst, host, port, timeout=30):
    try:
        p = subprocess.run(
            [*_argv(inst, host, port), "true"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    err = p.stderr.strip()
    return p.returncode, err.splitlines()[-1] if err else "no stderr"


def wait(inst, timeout=600, poll=5, log=None):
    candidates = _addr_candidates(inst)
    if not candidates:
        raise RuntimeError(f"instance {inst['id']} has no ssh address yet")
    end = time.time() + timeout
    while True:
        tried = []
        for host, port in candidates:
            rc, err = _probe(inst, host, port)
            if rc == 0:
                inst["ssh_addr"] = (host, port)
                return
            tried.append(f"{host}:{port} rc={rc} ({err})")
        report = "; ".join(tried)
        if time.time() > end:
            raise RuntimeError(f"ssh not ready within {timeout}s: {report}")
        if log:
            log(f"ssh not up: {report}")
        time.sleep(poll)


def _stream_once(inst, tar_src, dest):
    unpack = f"mkdir -p {shlex.quote(dest)} && tar xzf - -C {shlex.quote(dest)}"
    tar = subprocess.Popen(["tar", "czf", "-", *tar_src], stdout=subprocess.PIPE)
    try:
        ssh = subprocess.Popen([*base(inst), unpack], stdin=tar.stdout)
    finally:
        tar.stdout.close()
    rc = ssh.wait()
    tar.wait()
    return tar.returncode, rc


def _stream(inst, tar_src, dest, attempts=5, backoff=5):
    for attempt in range(1, attempts + 1):
        tar_rc, rc = _stream_once(inst, tar_src, dest)
        if not (tar_rc or rc):
            return
        if rc == 0:
            raise RuntimeError(f"upload failed locally (tar={tar_rc})")
        if attempt == attempts:
            raise RuntimeError(
                f"upload failed after {attempts} attempts (tar={tar_rc} ssh={rc})"
            )
        delay = backoff * 2 ** (attempt - 1)
        logger.warning(
            "[push] attempt %s/%s failed (tar=%s ssh=%s); retrying in %ss",
            attempt,
            attempts,
            tar_rc,
            rc,
            delay,
        )
        time.sleep(delay)


def put(inst, local, dest, files=None):
    local = os.path.expanduser(local)
    if not os.path.exists(local):
        raise RuntimeError(f"put: {local} not found")
    if files is not None:
        if not files:
            return
        src = ["-C", local, "--", *files]
    elif os.path.isdir(local):
        src = ["-C", local, "."]
    else:
        src = ["-C", os.path.dirname(local) or ".", os.path.basename(local)]
    _stream(inst, src, dest)
