"""Песочница для выполнения shell-команд.

По умолчанию используется Docker (изолированный контейнер с подмонтированной
рабочей директорией пользователя). Если Docker недоступен — fallback на
SubprocessSandbox с урезанным набором.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False

    def format(self, *, max_chars: int = 8000) -> str:
        out = []
        if self.stdout:
            out.append(f"STDOUT:\n{self.stdout}")
        if self.stderr:
            out.append(f"STDERR:\n{self.stderr}")
        out.append(f"EXIT CODE: {self.exit_code}")
        text = "\n\n".join(out) if out else f"EXIT CODE: {self.exit_code}"
        if self.truncated:
            text += "\n\n[вывод обрезан]"
        if len(text) > max_chars:
            return text[: max_chars - 30] + "\n\n[вывод обрезан]"
        return text


class Sandbox(Protocol):
    workdir: Path

    async def run(self, command: str, *, timeout: int) -> CommandResult: ...


class DockerSandbox:
    """Запускает команды в одноразовом Docker-контейнере с монтированной workdir.

    Контейнер создаётся для каждой команды (`docker run --rm`), что даёт чистую
    изоляцию. Сетевой доступ оставлен включённым, чтобы агент мог `pip install`,
    `git clone` и т.п. — отключайте через DOCKER_SANDBOX_NETWORK=none для
    параноидальной изоляции.
    """

    def __init__(
        self,
        workdir: Path,
        *,
        image: str = "python:3.12-slim",
        network: str = "bridge",
        memory: str = "1g",
        cpus: str = "1.0",
        max_output: int = 8000,
        host_workdir: Path | None = None,
    ) -> None:
        self.workdir = workdir.resolve()
        # Если бот сам запущен в Docker, путь, который видит docker daemon,
        # отличается от пути внутри контейнера. host_workdir — это путь на ХОСТЕ.
        self.host_workdir = (host_workdir or self.workdir).resolve()
        self.image = image
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.max_output = max_output
        self.workdir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def is_available() -> bool:
        return shutil.which("docker") is not None

    async def run(self, command: str, *, timeout: int) -> CommandResult:
        # /sandbox — рабочая директория внутри контейнера.
        args = [
            "docker",
            "run",
            "--rm",
            "-i",
            f"--network={self.network}",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            "--workdir=/sandbox",
            "-v",
            f"{self.host_workdir}:/sandbox",
            "-e",
            "HOME=/sandbox",
            "-e",
            "PYTHONUNBUFFERED=1",
            self.image,
            "bash",
            "-lc",
            command,
        ]
        return await _run_subprocess(args, timeout=timeout, max_output=self.max_output)


class SubprocessSandbox:
    """Fallback без Docker: запускает bash напрямую в workdir.

    ВАЖНО: изоляция отсутствует. Используйте только если Docker недоступен и
    бот развёрнут в выделенной VM/контейнере, которой не жалко.
    """

    def __init__(self, workdir: Path, *, max_output: int = 8000) -> None:
        self.workdir = workdir.resolve()
        self.max_output = max_output
        self.workdir.mkdir(parents=True, exist_ok=True)

    async def run(self, command: str, *, timeout: int) -> CommandResult:
        env = dict(os.environ)
        env["HOME"] = str(self.workdir)
        return await _run_subprocess(
            ["bash", "-lc", command],
            timeout=timeout,
            max_output=self.max_output,
            cwd=self.workdir,
            env=env,
        )


async def _run_subprocess(
    args: list[str],
    *,
    timeout: int,
    max_output: int,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
    except FileNotFoundError as exc:
        return CommandResult(exit_code=127, stdout="", stderr=str(exc))

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return CommandResult(
            exit_code=124,
            stdout="",
            stderr=f"Команда прервана по таймауту ({timeout}с)",
            truncated=True,
        )

    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    truncated = False
    if len(stdout) > max_output:
        stdout = stdout[:max_output] + "\n[обрезано]"
        truncated = True
    if len(stderr) > max_output:
        stderr = stderr[:max_output] + "\n[обрезано]"
        truncated = True

    return CommandResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        truncated=truncated,
    )


def build_sandbox(workdir: Path, *, max_output: int) -> Sandbox:
    if DockerSandbox.is_available() and os.getenv("DISABLE_DOCKER_SANDBOX") != "1":
        # Если бот сам запущен в контейнере, нужен HOST_WORKSPACES_DIR — путь
        # рабочей папки на ХОСТЕ (тот же том, что подмонтирован в /app/data/workspaces).
        host_workspaces = os.getenv("HOST_WORKSPACES_DIR")
        host_workdir = None
        if host_workspaces:
            host_workdir = Path(host_workspaces) / workdir.name
        logger.info("Используется DockerSandbox для %s (host=%s)", workdir, host_workdir)
        return DockerSandbox(
            workdir,
            image=os.getenv("SANDBOX_IMAGE", "python:3.12-slim"),
            network=os.getenv("SANDBOX_NETWORK", "bridge"),
            memory=os.getenv("SANDBOX_MEMORY", "1g"),
            cpus=os.getenv("SANDBOX_CPUS", "1.0"),
            max_output=max_output,
            host_workdir=host_workdir,
        )
    logger.warning(
        "Docker не найден — используется SubprocessSandbox без изоляции (workdir=%s)", workdir
    )
    return SubprocessSandbox(workdir, max_output=max_output)
