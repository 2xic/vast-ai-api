import json
import os
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

load_dotenv()

api_url = "https://cloud.vast.ai/api/v0/"

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
    return _session.request(method, url, timeout=30, **kwargs).json()


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


def get_available_instances(options: AvailableInstancesFilter):
    search = {
        "disk_space": {"gte": options.min_disk_space_gb},
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "num_gpus": {"gte": options.min_gpu},
        "dph_total": {"lte": options.max_dollar_price_hour},
        "inet_up": {"gte": options.mbps_up},
        "inet_down": {"gte": options.mbps_down},
        "order": [["score", "desc"]],
        "type": "ask",  # bid or ask, ask = on - demand
    }
    if options.gpu_name:
        search["gpu_name"] = {"eq": options.gpu_name}
    results = _request("GET", wrap_url(f"{api_url}/bundles/", {"q": search}))
    if "offers" not in results:
        raise RuntimeError(f"bundles search returned no 'offers': {results}")
    for i in results["offers"]:
        yield {
            "id": i["id"],
            "gpu": i["gpu_name"],
            "num_gpus": i["num_gpus"],
            "price": i["dph_total"],
            "score": i["score"],
            "disk": i["disk_space"],
            "gpu_ram": i["gpu_ram"],
        }


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


def create_instance(id, options: InstanceOptions = None, label=None):
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
    url = wrap_url(f"{api_url}/asks/{id}/")
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


def wait_until_ready(instance_id, timeout=600, poll=10, log=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            inst = next(
                (i for i in get_running_instances() if i["id"] == instance_id), None
            )
        except Exception as e:
            inst = None
            if log:
                log(f"instance list failed, retrying: {e}")
        if _is_ready(inst):
            return inst
        if log and inst is not None:
            log(f"status={inst['status']}, ssh addr pending")
        time.sleep(poll)
    raise TimeoutError(f"instance {instance_id} not ready within {timeout}s")


def get_running_instances():
    url = wrap_url(f"{api_url}/instances/", {"owner": "me"})
    body = _request("GET", url)
    if body.get("instances") is None:
        raise RuntimeError(f"list instances failed: {body}")
    for instance in body["instances"]:
        host, port = instance["ssh_host"], instance["ssh_port"]
        public_ip = instance["public_ipaddr"]
        ports = instance.get("ports") or {}
        yield {
            "id": instance["id"],
            "ssh_host": host,
            "ssh_port": port,
            "ssh": f"ssh root@{host} -p {port}",
            "open_ports": [
                f"{public_ip}:{p[0]['HostPort']} -> {ref}" for ref, p in ports.items()
            ],
            "public_ip": public_ip,
            "status": instance["actual_status"],
            "label": instance.get("label"),
            "start_date": instance.get("start_date"),
        }


def delete_instance(id):
    url = wrap_url(f"{api_url}/instances/{id}/")
    return _api("DELETE", url, "delete_instance", json={})
