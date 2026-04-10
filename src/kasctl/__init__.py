#!/usr/bin/env -S uv run --script
#
# /// script
# dependencies = [
#   "coloredlogs",
#   "docker",
#   "pyyaml",
#   "questionary",
#   "rich",
#   "tomli_w",
# ]
# ///

import argparse
import datetime
import logging
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import tomllib

import coloredlogs
import docker
import questionary
import tomli_w
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
)

logger = logging.getLogger("image-builder")

CONSOLE = Console()
CUSTOM_STYLE = questionary.Style(
    [
        ("answer", "noinherit bold italic fg:#FF9D00"),
        ("pointer", "noinherit bold italic fg:#FF9D00"),
        ("selected", "noinherit bold italic fg:#FF9D00"),
    ]
)
PATH = pathlib.Path(f"{os.environ['HOME']}/.local/share/kas-builder/")
TASK_PROGRESS_RE = re.compile(r"NOTE: Running task (\d+) of (\d+)")

PATH.mkdir(parents=True, exist_ok=True)


def __yaml_dict_representer__(dumper, data):
    if not data:
        return dumper.represent_scalar("tag:yaml.org,2002:", "")
    return dumper.represent_mapping("tag:yaml.org,2002:map", data.items())


def __yaml_str_representer__(dumper, data):
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def get_all_branches(name: str, url: str):
    command = f"git ls-remote -h {url}"
    with CONSOLE.status(
        f"[bold italic]fetching branches from [#FF9D00]{name}",
        spinner="bouncingBar",
        spinner_style="#FF9D00",
    ):
        result = subprocess.run(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        logger.error("git ls-remote failed for %s (%s): %s", name, url, result.stdout.decode())
        raise RuntimeError(f"failed to fetch branches from {name} ({url})")
    branches = [line for line in result.stdout.decode().splitlines()]
    branches = ["/".join(branch.split("/")[2:]) for branch in branches]
    return list(filter(None, branches))


def get_default_branch(name: str, url: str):
    command = f"git ls-remote --symref {url} HEAD"
    with CONSOLE.status(
        f"[bold italic]fetching default branch for [#FF9D00]{name}",
        spinner="bouncingBar",
        spinner_style="#FF9D00",
    ):
        result = subprocess.run(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        logger.error("git ls-remote failed for %s (%s): %s", name, url, result.stdout.decode())
        raise RuntimeError(f"failed to fetch default branch from {name} ({url})")

    match = re.search(r"ref: refs/heads/(.*)HEAD", result.stdout.decode())
    if match:
        default_branch = match.group(1).rstrip()
        logger.debug("fetched default branch %s from %s (%s)", default_branch, name, url)
        return default_branch


def get_distro(distros: list[str]):
    distro = questionary.select(
        "select distro:",
        choices=distros,
    ).ask()
    if distro is not None:
        return distro
    raise KeyboardInterrupt()


def get_kas_config(layer_settings: dict, layer_path: pathlib.Path | None):
    logger.debug("using layer settings:\n%s", str(layer_settings))

    name = layer_settings["layer"]["name"]
    url = layer_settings["layer"]["url"]

    config = dict()
    config["distro"] = get_distro(layer_settings["distros"])
    config["header"] = dict()
    config["header"]["version"] = 18
    config["header"]["includes"] = [
        {
            "repo": name,
            "file": "kas/yocto/include/common.yaml",
        },
    ]

    machines = layer_settings["machines"]
    machine = get_machine(machines)
    config["machine"] = machine
    if machines[machine].get("includes"):
        for include in machines[machine]["includes"]:
            config["header"]["includes"].append(
                {
                    "repo": name,
                    "file": include,
                }
            )

    config["repos"] = dict()

    config["repos"][name] = dict()
    if layer_path is not None:
        config["repos"][name]["path"] = f"/home/kas/{name}"
    else:
        select_branch = questionary.confirm(f"select {name} branch", default=False).ask()
        if select_branch is None:
            raise KeyboardInterrupt()
        branch = get_selected_branch(name, url) if select_branch else get_default_branch(name, url)

        config["repos"][name] = dict()
        config["repos"][name]["url"] = url
        config["repos"][name]["branch"] = branch
        config["repos"][name]["commit"] = get_latest_commit_from_branch(branch, url)

    config["target"] = get_target(layer_settings["targets"])

    repos = layer_settings.get("repos")

    if repos:
        branches_variable = list()
        selected_repos = get_selected_repos(repos)

        for repo in repos.keys():
            name = repo["name"]
            url = repo["url"]
            variable = repo["variable"]
            if repo in selected_repos:
                branch = select_branch(name, url)
            else:
                branch = get_default_branch(name, url)
            branches_variable.append(f'{variable} = "{branch}"')

        config["local_conf_header"] = dict()
        config["local_conf_header"]["branches"] = "\n".join(branches_variable)

    return config


def get_layer_path(name: str) -> pathlib.Path:
    layer_path = questionary.path(
        f"path to {name} repository",
        style=CUSTOM_STYLE,
    ).ask()

    if layer_path:
        return pathlib.Path(layer_path)
    raise KeyboardInterrupt()


def get_latest_commit_from_branch(branch: str, url: str):
    command = f"git ls-remote {url} refs/heads/{branch}"
    with CONSOLE.status(
        f"[bold italic]fetching latest commit from [#FF9D00]{branch}",
        spinner="bouncingBar",
        spinner_style="#FF9D00",
    ):
        result = subprocess.run(shlex.split(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        logger.error("git ls-remote failed for %s (%s): %s", branch, url, result.stdout.decode())
        raise RuntimeError(f"failed to fetch latest commit from {branch} ({url})")

    match = re.search(r"^(\w+)", result.stdout.decode())
    if match:
        commit = match.group(1).rstrip()
        logger.debug("fetched commit %s from %s (%s)", commit, branch, url)
        return commit


def get_machine(machines: dict[str, dict[str, list[str]]]):
    machine = questionary.select(
        "select machine:",
        choices=machines.keys(),
    ).ask()
    if machine is not None:
        return machine
    raise KeyboardInterrupt()


def get_selected_branch(name: str, url: str):
    branches = get_all_branches(name, url)
    default_branch = get_default_branch(name, url)
    selection = questionary.autocomplete(
        f"select {name} branch:",
        style=CUSTOM_STYLE,
        default=default_branch,
        choices=branches,
        validate=lambda text: True if text in branches else "Please select a valid branch",
    ).ask()
    if selection is not None:
        return selection
    raise KeyboardInterrupt()


# TODO: proper type annotations
def get_selected_repos(repos: dict):
    selected_repos = questionary.checkbox(
        "select repos:",
        choices=[questionary.Choice(x, checked=True) for x in repos.keys()],
        style=CUSTOM_STYLE,
    ).ask()

    if selected_repos is not None:
        return selected_repos
    raise KeyboardInterrupt()


def get_target(targets: list[str]):
    target = questionary.select(
        "select target:",
        choices=targets,
    ).ask()
    if target is not None:
        return target
    raise KeyboardInterrupt()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("layer")
    parser.add_argument("--print", action="store_true", help="print stored settings")
    parser.add_argument("--clear", action="store_true", help="clear stored settings")
    parser.add_argument("--local", action="store_true", help="use local repository")
    parser.add_argument("-v", action="store_true", help="info level logging")
    parser.add_argument("-vv", action="store_true", help="debug level logging")
    parser.add_argument("-vvv", action="store_true", help="trace level logging")
    args = parser.parse_args()

    coloredlogs.install(
        level=logging.DEBUG if args.vvv or args.vv else logging.INFO if args.v else logging.WARNING,
        logger=logger,
    )

    yaml.add_representer(str, __yaml_str_representer__)
    yaml.add_representer(dict, __yaml_dict_representer__)

    PATH.mkdir(parents=True, exist_ok=True)

    if args.print:
        try:
            with (PATH / f"{args.layer}.yaml").open() as f:
                kas_config = yaml.safe_load(f)
            print(yaml.safe_dump(kas_config, indent=4))
            sys.exit(0)
        except FileNotFoundError:
            sys.exit(1)

    if args.clear:
        (PATH / f"{args.layer}.yaml").unlink(missing_ok=True)

    try:
        with (PATH / "settings.toml").open("rb") as f:
            kas_builder_settings = tomllib.load(f)

        if args.layer not in kas_builder_settings or kas_builder_settings[args.layer].get("path") is None:
            raise FileNotFoundError()
        else:
            layer_path = pathlib.Path(kas_builder_settings[args.layer]["path"])
    except FileNotFoundError:
        layer_path = get_layer_path(args.layer)
        kas_builder_settings = dict()
        kas_builder_settings[args.layer] = dict()
        kas_builder_settings[args.layer]["path"] = str(layer_path)

    try:
        with (layer_path / "kas" / ".settings.toml").open("rb") as f:
            layer_settings = tomllib.load(f)
    except FileNotFoundError:
        logger.error("TODO: proper error message for missing layer settings.toml")
        sys.exit(1)

    try:
        with (PATH / f"{args.layer}.yaml").open() as f:
            kas_config = yaml.safe_load(f)
            questionary.print(f"stored {args.layer} configuration:\n", style="bold", end="")
            questionary.print(yaml.dump(kas_config, indent=4), style="italic fg:#FF9D00")
        reuse = questionary.confirm("build image using stored configuration").ask()
        if reuse is None:
            raise KeyboardInterrupt()
        if not reuse:
            kas_config = kas_config | get_kas_config(layer_settings, layer_path if args.local else None)
    except FileNotFoundError:
        kas_config = get_kas_config(layer_settings, layer_path if args.local else None)

    with (PATH / "settings.toml").open("wb") as f:
        tomli_w.dump(kas_builder_settings, f)

    (PATH / f"{args.layer}.yaml").write_text(yaml.dump(kas_config, indent=4, default_flow_style=False))

    command = "uv run kas build"
    logger.info("executing command '%s'", command)

    client = docker.from_env()

    user = f"{os.getuid()}:{os.getgid()}"

    ssh_known_hosts = pathlib.Path(os.environ.get("SSH_FOLDER", os.path.expanduser("~/.ssh"))) / "known_hosts"
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")

    original_handler = signal.getsignal(signal.SIGINT)
    container = None
    status = None
    logs = ""

    try:
        container = client.containers.run(
            init=True,
            name=args.layer,
            image=args.layer,
            command=["/bin/bash", "-c", f"exec {command}"],
            environment={"SSH_AUTH_SOCK": "/ssh-agent"},
            volumes=[
                f"{str(ssh_known_hosts)}:/home/kas/.ssh/known_hosts:ro",
                f"{ssh_auth_sock}:/ssh-agent:ro",
                f"{str(layer_path)}:/home/kas/{args.layer}:z",
                f"{str(PATH / f'{args.layer}.yaml')}:/home/kas/{args.layer}/kas/yocto/.config.yaml:ro",
            ],
            user=user,
            detach=True,
            stop_signal="SIGINT",
            working_dir=f"/home/kas/{args.layer}/kas/yocto",
        )

        shutting_down = False

        def graceful_shutdown(signum, frame):
            nonlocal shutting_down
            if shutting_down:
                logger.warning("forced shutdown requested")
                container.kill()
                return
            shutting_down = True
            logger.info("graceful shutdown requested, waiting for bitbake to finish...")
            result = subprocess.run(
                ["bash", "-c", "pgrep -f 'bin/bitbake -c build'"],
                capture_output=True,
                text=True,
            )
            pid = result.stdout.strip()
            if pid:
                logger.info("sending SIGTERM to bitbake (pid %s)", pid)
                os.kill(int(pid), signal.SIGINT)
            else:
                logger.warning("bitbake process not found, killing container")
                container.kill()

        signal.signal(signal.SIGINT, graceful_shutdown)

        log_lines = []
        with Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(complete_style="#FF9D00", finished_style="green", bar_width=None),
            TaskProgressColumn(),
            TimeRemainingColumn(),
            expand=True,
            console=CONSOLE,
        ) as progress:
            task = progress.add_task("Preparing build...", total=None)
            current = 0
            total = None

            for chunk in container.logs(stream=True, follow=True):
                line = chunk.decode("utf-8", errors="replace")
                log_lines.append(line)

                if shutting_down:
                    progress.update(task, description="[red]Shutting down bitbake...")
                    continue

                match = TASK_PROGRESS_RE.search(line)
                if match:
                    current = int(match.group(1))
                    total = int(match.group(2))
                    progress.update(
                        task,
                        completed=current,
                        total=total,
                        description=f"Building ({current}/{total})",
                    )

            status = container.wait()
            if status["StatusCode"] == 0:
                progress.update(
                    task,
                    completed=total or 1,
                    total=total or 1,
                    description="[green]Build complete",
                )
            else:
                progress.update(
                    task,
                    completed=total or 1,
                    total=total or 1,
                    description=f"[red]Build failed (exit {status['StatusCode']})",
                )

    finally:
        signal.signal(signal.SIGINT, original_handler)

        if container is not None:
            try:
                logs = container.logs(stdout=True, stderr=True).decode("utf-8")
                now = datetime.datetime.now()
                logfile = PATH / f"logs/build_{now.strftime('%Y-%m-%d_%H:%M:%S')}.log"
                logfile.parent.mkdir(parents=True, exist_ok=True)
                with logfile.open("w") as f:
                    f.write(logs)
            except docker.errors.NotFound:
                # container already gone, use streamed logs instead
                logs = "".join(log_lines) if log_lines else ""

            try:
                container.stop()
            except docker.errors.NotFound:
                pass

            try:
                container.remove()
            except docker.errors.NotFound:
                pass

    if status is None or status["StatusCode"] != 0:
        print(logs)
        sys.exit(status["StatusCode"] if status else 1)

    if "qemuarm" in kas_config["machine"]:
        serial_port = 4321

        CONSOLE.print(
            f"[bold]to create a serial console run: "
            f"[italic #FF9D00]socat pty,link=/tmp/vserial0,raw,echo=0, TCP:127.0.0.1:{serial_port} &"
        )

        subprocess.run(
            [
                "docker",
                "run",
                "--network=host",
                "--privileged",
                "--rm",
                "--user",
                user,
                "-it",
                "-e",
                "SSH_AUTH_SOCK=/ssh-agent",
                "-v",
                "/dev/bus/usb:/dev/bus/usb",
                "-v",
                f"{str(ssh_known_hosts)}:/home/kas/.ssh/known_hosts:ro",
                "-v",
                f"{ssh_auth_sock}:/ssh-agent:ro",
                "-v",
                f"{str(layer_path)}:/home/kas/{args.layer}:z",
                "-v",
                f"{str(PATH / f'{args.layer}.yaml')}:/home/kas/{args.layer}/kas/yocto/.config.yaml:ro",
                "-v",
                "/tmp:/tmp",
                "-w",
                f"/home/kas/{args.layer}/kas/yocto",
                args.layer,
                "uv",
                "run",
                "kas",
                "shell",
                "-c",
                f'runqemu nonetwork qemuparams="-display none -monitor stdio -serial tcp:127.0.0.1:{serial_port},server,nowait '
                f"-netdev tap,id=net0,ifname=tap0,script=no,downscript=no "
                f"-device virtio-net-device,netdev=net0,mac=52:54:00:12:34:02 "
                f'-device usb-host,vendorid=0x2c7c,productid=0x6002,bus=usb-bus.0,id=modem"',
            ],
        )

    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
