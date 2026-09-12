import json
import logging
import os
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

load_dotenv()

logger = logging.getLogger("vast_cli")

api_url = "https://cloud.vast.ai/api/v0"

_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
    ),
)


def wrap_url(url, query_args=None):
    params = {**(query_args or {}), "api_key": os.environ["VAST_API_KEY"]}
    encoded = {k: v if isinstance(v, str) else json.dumps(v) for k, v in params.items()}
    return url + "?" + urlencode(encoded)


def _request(method, url, **kwargs):
    r = _session.request(method, url, timeout=30, **kwargs)
    try:
        return r.json()
    except ValueError:
        body = " ".join(r.text.split())[:300] or "<empty>"
        raise RuntimeError(
            f"{method} {r.status_code} non-json response: {body}"
        ) from None


def _api(method, url, what, **kwargs):
    body = _request(method, url, **kwargs)
    if not body.get("success"):
        raise RuntimeError(f"{what} failed: {body}")
    return body


@dataclass
class AvailableInstancesFilter:
    min_gpu: int = 8
    min_disk_space_gb: int = 40
    max_dollar_price_hour: float = 10
    # internet up and down
    mbps_up: float = 10
    mbps_down: float = 10
    gpu_name: str = None
    verified: bool = True


OFFER_LIMIT = 64


def _offer(i):
    return {
        "id": i["id"],
        "machine_id": i["machine_id"],
        "gpu": i["gpu_name"],
        "num_gpus": i["num_gpus"],
        "price": i["dph_total"],
        "score": i["score"],
        "disk": i["disk_space"],
        "gpu_ram": i["gpu_ram"],
    }


def gpu_names():
    body = _request("GET", f"{api_url}/gpu_names/unique/")
    if not body.get("success"):
        raise RuntimeError(f"gpu_names failed: {body}")
    return body["gpu_names"]


def get_available_instances(
    options: AvailableInstancesFilter, order=None, limit=OFFER_LIMIT
):
    search = {
        "disk_space": {"gte": options.min_disk_space_gb},
        "rentable": {"eq": True},
        "num_gpus": {"gte": options.min_gpu},
        "dph_total": {"lte": options.max_dollar_price_hour},
        "inet_up": {"gte": options.mbps_up},
        "inet_down": {"gte": options.mbps_down},
        "order": order or [["score", "desc"]],
        "limit": min(limit, OFFER_LIMIT),
        "type": "ask",  # bid or ask, ask = on - demand
    }
    if options.verified:
        search["verified"] = {"eq": True}
    if options.gpu_name:
        search["gpu_name"] = {"eq": options.gpu_name}
    results = _request("GET", wrap_url(f"{api_url}/bundles/", {"q": search}))
    if "offers" not in results:
        raise RuntimeError(f"bundles search returned no 'offers': {results}")
    for i in results["offers"]:
        yield _offer(i)


def pick_offer(options: AvailableInstancesFilter):
    offer = next(get_available_instances(options), None)
    if offer is None:
        raise RuntimeError("no available offer matched the filter")
    return offer


@dataclass
class InstanceOptions:
    # images can be found here https://cloud.vast.ai/api/v0/users/undefined/templates/null/
    docker_image = "pytorch/pytorch:2.7.1-cuda11.8-cudnn9-runtime"
    # options to docker, i.e if you want to open a port
    # ["-p 8081:8081", "-p 8082:8082"]
    docker_options: list[str] = field(default_factory=list)
    disk_space = 10  # gb


def create_instance(offer_id, options: InstanceOptions = None, label=None):
    options = options or InstanceOptions()
    payload = {
        "client_id": "me",
        "image": options.docker_image,
        "env": {opt: "1" for opt in options.docker_options},
        "args_str": "",
        "onstart": "",
        "runtype": "ssh ssh_direc ssh_proxy",
        "image_login": None,
        "use_jupyter_lab": False,
        "jupyter_dir": None,
        "python_utf8": False,
        "lang_utf8": False,
        "disk": options.disk_space,
        "label": label,
    }
    url = wrap_url(f"{api_url}/asks/{offer_id}/")
    return _api("PUT", url, "create_instance", json=payload)["new_contract"]


def list_ssh_keys():
    return _request("GET", wrap_url(f"{api_url}/ssh/"))


def attach_ssh_key(instance_id, public_key):
    url = wrap_url(f"{api_url}/instances/{instance_id}/ssh/")
    return _api("POST", url, "attach_ssh_key", json={"ssh_key": public_key})


def _is_ready(inst):
    return bool(
        inst and inst["status"] == "running" and inst["ssh_host"] and inst["ssh_port"]
    )


class InstanceError(RuntimeError):
    pass


def _failure(inst):
    if not inst:
        return None
    if inst["status"] in ("exited", "offline"):
        return inst["status"]
    msg = inst.get("status_msg") or ""
    return msg if "error" in msg.lower() else None


def wait_until_ready(
    instance_id, timeout=1800, poll=10, log=None, error_grace=6, stall_s=1200
):
    deadline = time.time() + timeout
    errors = 0
    mark = None
    while time.time() < deadline:
        try:
            inst = next(
                (i for i in get_running_instances() if i["id"] == instance_id), None
            )
            listed = True
        except Exception as e:
            inst, listed = None, False
            if log:
                log(f"instance list failed, retrying: {e}")
        if _is_ready(inst):
            return inst
        if inst is not None:
            state = (inst["status"], inst["status_msg"])
            if mark is None or mark[0] != state:
                mark = (state, time.time())
        fail = "no longer listed" if listed and inst is None else _failure(inst)
        if fail:
            errors += 1
            if log:
                log(f"error ({errors}/{error_grace}): {fail}")
            if errors >= error_grace:
                raise InstanceError(f"instance {instance_id} failed: {fail}")
        elif inst is not None:
            if time.time() - mark[1] >= stall_s:
                raise InstanceError(
                    f"instance {instance_id} stuck in {mark[0][0]} for {stall_s}s: "
                    f"{mark[0][1]!r}"
                )
            if log:
                log(f"status={inst['status']} msg={inst['status_msg']!r}")
        time.sleep(poll)
    raise TimeoutError(f"instance {instance_id} not ready within {timeout}s")


def _fmt_ports(public_ip, ports):
    out = []
    for ref, p in ports.items():
        try:
            out.append(f"{public_ip}:{p[0]['HostPort']} -> {ref}")
        except (KeyError, IndexError, TypeError):
            continue
    return out


def _direct_ssh(public_ip, ports):
    if not public_ip:
        return None
    try:
        return {"host": public_ip, "port": int(ports["22/tcp"][0]["HostPort"])}
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _proxy_port(instance):
    try:
        port = int(instance["ssh_port"])
    except (KeyError, TypeError, ValueError):
        return None
    return port + 1 if "jupyter" in (instance.get("image_runtype") or "") else port


def _instance_row(instance):
    host = instance.get("ssh_host")
    port = _proxy_port(instance)
    public_ip = instance.get("public_ipaddr")
    ports = instance.get("ports") or {}
    return {
        "id": instance["id"],
        "ssh_host": host,
        "ssh_port": port,
        "ssh_direct": _direct_ssh(public_ip, ports),
        "ssh": f"ssh root@{host} -p {port}",
        "open_ports": _fmt_ports(public_ip, ports),
        "public_ip": public_ip,
        "status": instance.get("actual_status"),
        "status_msg": (instance.get("status_msg") or "").strip(),
        "label": instance.get("label"),
        "start_date": instance.get("start_date"),
    }


def get_running_instances():
    url = wrap_url(f"{api_url}/instances/", {"owner": "me"})
    body = _request("GET", url)
    if body.get("instances") is None:
        raise RuntimeError(f"list instances failed: {body}")
    for instance in body["instances"]:
        try:
            row = _instance_row(instance)
        except Exception as e:
            ref = instance.get("id") if isinstance(instance, dict) else instance
            logger.warning("[instances] skipping malformed entry %r: %s", ref, e)
            continue
        yield row


def delete_instance(instance_id):
    url = wrap_url(f"{api_url}/instances/{instance_id}/")
    return _api("DELETE", url, "delete_instance", json={})
