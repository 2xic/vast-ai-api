import requests
import json
from dotenv import load_dotenv
import os
from urllib.parse import quote_plus
from dataclasses import dataclass, field
from typing import List

load_dotenv()

api_url = "https://cloud.vast.ai/api/v0/"

def wrap_url(url, query_args={}):
    query_args["api_key"] = os.environ["API_KEY"]
    return url + "?" + "&".join([
        "{x}={y}".format(x=x, y=quote_plus(y if isinstance(y, str) else json.dumps(y))) for x, y in
        query_args.items()
    ])

def get_available_instances(min_gpu=8, min_disk_space_gb=40):
    search = {
        "disk_space": {"gte": min_disk_space_gb}, 
        "verified": {"eq": True}, 
        "rentable": {"eq": True},
        "num_gpus": {
            # We want big machine
            "gte": min_gpu,
            "lte": 16
        },
        "order": [
            ["score", "desc"]
        ],
        "allocated_storage": min_disk_space_gb,
        "cuda_max_good": {},
        "extra_ids": [],
        "type": "ask"  # bid or ask, ask = on - demand
    }
    q = json.dumps(search)
    results = requests.get(f"{api_url}/bundles/?q={q}").json()
    print(results)
    for i in results["offers"]:
        yield {
            "id": i["id"],
            "num_gpus": i["num_gpus"],
            "price": i["dph_total"],
            "disk": min_disk_space_gb
        }

@dataclass
class InstanceOptions:
    # images can be found here https://cloud.vast.ai/api/v0/users/undefined/templates/null/
    docker_image = "pytorch/pytorch"
    # options to docker, i.e if you want to open a port
    # ["-p 8081:8081", "-p 8082:8082"]
    docker_options: List[str] = field(default_factory=list)
    disk_space = 10 # gb

def create_instance(id, options: InstanceOptions=InstanceOptions()):
    url = wrap_url(f"{api_url}/asks/{id}/", {})
    docker_env = {}
    for i in options.docker_options:
        docker_env[i] = "1"
    payload = {
        "client_id": "me",
        "image": options.docker_image,
        "env": docker_env,
        "args_str": "",
        "onstart": "",
        "runtype": "ssh ssh_direc ssh_proxy",
        "image_login": None,
        "use_jupyter_lab": False,
        "jupyter_dir": None,
        "python_utf8": False,
        "lang_utf8": False,
        # size of local disk partition in GB
        "disk": options.disk_space,
    }
    response = requests.put(url, json=payload)
    print(response)
    print(response.json())

def get_running_instances():
    url = wrap_url(f"{api_url}/instances/", {
        "owner": "me"
    })
    for instance in requests.get(url).json()["instances"]:
        port = instance["ssh_port"]
        host = instance["ssh_host"]
        open_ports = instance.get("ports", [])
        public_ip = instance["public_ipaddr"]
        formatted_open_ports = []
        for port_ref in open_ports:
            # ipv4
            entry = open_ports[port_ref][0]
            formatted_open_ports.append(public_ip + ":" + entry["HostPort"] + " -> " + port_ref)
        status = instance["actual_status"]

        yield {
            "id": instance["id"],
            "ssh_host": host,
            "ssh_port": port,
            "status": instance["status_msg"],
            "ssh": f"ssh root@{host} -p {port}",
            "open_ports": formatted_open_ports,
            "public_ip": public_ip,
            "status": status
        }

def stop_all_running_instances():
    for i in get_running_instances():
        delete_instance(i["id"])

def delete_instance(id):
    url = wrap_url(f"{api_url}/instances/{id}/")
    r = requests.delete(url, json={})
    print(r)
    print(r.json())
