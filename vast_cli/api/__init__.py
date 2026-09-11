import importlib

_EXPORTS = {
    "AvailableInstancesFilter": "vast_cli.api.client",
    "InstanceOptions": "vast_cli.api.client",
    "clean": "vast_cli.api.run",
    "destroy": "vast_cli.api.run",
    "exec_on": "vast_cli.api.run",
    "launch": "vast_cli.api.run",
    "list_gpus": "vast_cli.api.run",
    "list_managed": "vast_cli.api.run",
    "ps": "vast_cli.api.run",
    "reap": "vast_cli.api.run",
    "rerun": "vast_cli.api.run",
    "set_max_age": "vast_cli.api.run",
    "ssh_argv": "vast_cli.api.run",
    "client": "vast_cli.api.client",
    "remote": "vast_cli.api.remote",
    "run": "vast_cli.api.run",
}

__all__ = [
    "AvailableInstancesFilter",
    "InstanceOptions",
    "clean",
    "client",
    "destroy",
    "exec_on",
    "launch",
    "list_gpus",
    "list_managed",
    "ps",
    "reap",
    "remote",
    "rerun",
    "run",
    "set_max_age",
    "ssh_argv",
]


def __getattr__(name):
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(target)
    return module if target.endswith(f".{name}") else getattr(module, name)
