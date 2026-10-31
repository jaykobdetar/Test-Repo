"""Real containment check after the launching Python and Podman client are killed.

Only a fixed synthetic program runs. The observer never stops the container until
its observation has passed or failed; cleanup cannot count as evidence of success.
"""

from __future__ import annotations

from contextlib import ExitStack
import ctypes
import multiprocessing
import os
import re
import select
import signal
import subprocess
import time
import uuid

from .sandbox import PodmanSandbox, SandboxLimits


class LifecycleError(RuntimeError):
    pass


def _pidfd_open(pid: int) -> int:
    # The bundled portable Python may omit its Linux wrappers even though the
    # host libc/kernel provide them. Keep PID-reuse-safe observation in that case.
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "pidfd_open"):
        raise LifecycleError("host runtime lacks PID descriptors")
    function = libc.pidfd_open
    function.argtypes, function.restype = [ctypes.c_int, ctypes.c_uint], ctypes.c_int
    result = function(pid, 0)
    if result < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open failed")
    return result


def _kill_pidfd(descriptor: int):
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "pidfd_send_signal"):
        raise LifecycleError("host runtime lacks PID descriptor signals")
    function = libc.pidfd_send_signal
    function.argtypes, function.restype = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint], ctypes.c_int
    if function(descriptor, signal.SIGKILL, None, 0) < 0:
        raise OSError(ctypes.get_errno(), "pidfd_send_signal failed")


def _driver(settings: dict, wall_seconds: int, marker: str, events):
    # Observe the real launcher rather than replacing its process or behavior.
    original_popen = subprocess.Popen

    def observed_popen(command, *args, **kwargs):
        launched_at = time.monotonic()
        process = original_popen(command, *args, **kwargs)
        if isinstance(command, list) and command[1:3] == ["--remote=false", "run"]:
            events.send(
                {"client_pid": process.pid, "name": command[command.index("--name") + 1], "launched_at": launched_at}
            )
        return process

    subprocess.Popen = observed_popen
    try:
        sandbox = PodmanSandbox(**settings)
        code = (
            "import os, pathlib, signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"pathlib.Path('/output/lifecycle-ready').write_text({marker!r})\n"
            "for fd in (0, 1, 2): os.close(fd)\n"
            "while True: time.sleep(1)\n"
        )
        result = sandbox.run(
            code, limits=SandboxLimits(wall_seconds=wall_seconds, memory_bytes=128 * 1024**2, max_broker_requests=0)
        )
        events.send({"early_exit": result.returncode, "reason": result.termination_reason})
    except BaseException as error:
        # Do not send arbitrary subprocess/exception text across this channel.
        events.send({"driver_error": type(error).__name__})
    finally:
        subprocess.Popen = original_popen
        events.close()


def _finished(pidfd: int) -> bool:
    return bool(select.select([pidfd], [], [], 0)[0])


def run_lifecycle_check(
    sandbox: PodmanSandbox, *, wall_seconds: int = 12, startup_seconds: int = 30, cleanup_seconds: int = 10
) -> dict:
    """Prove independent termination AND removal, using actual processes/PID FDs."""
    if os.geteuid() == 0:
        raise LifecycleError("lifecycle acceptance requires rootless Linux with PID descriptors")
    if not (
        type(wall_seconds) is int
        and 5 <= wall_seconds <= 30
        and type(startup_seconds) is int
        and 5 <= startup_seconds <= 60
        and type(cleanup_seconds) is int
        and 1 <= cleanup_seconds <= 20
    ):
        raise ValueError("lifecycle test durations must be bounded")
    settings = {
        "image": sandbox.image,
        "workspace": sandbox.workspace,
        "podman": sandbox.podman,
        "seccomp_profile": sandbox.seccomp_profile,
    }
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    marker = uuid.uuid4().hex
    driver = context.Process(target=_driver, args=(settings, wall_seconds, marker, send))
    name = None
    client_fd = None
    report = {
        "wall_seconds": wall_seconds,
        "cleanup_seconds": cleanup_seconds,
        "launchers_killed": False,
        "program_started": False,
        "container_processes_stopped": False,
        "container_removed": False,
    }

    def command(*arguments, timeout=5):
        return subprocess.run(
            sandbox._command(*arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=sandbox._environment(),
        )

    driver.start()
    send.close()
    try:
        with ExitStack() as descriptors:
            driver_fd = _pidfd_open(driver.pid)
            descriptors.callback(os.close, driver_fd)
            deadline = time.monotonic() + startup_seconds
            while time.monotonic() < deadline:
                if receive.poll(0.1):
                    try:
                        event = receive.recv()
                    except EOFError:
                        raise LifecycleError("lifecycle driver exited before startup") from None
                    if "client_pid" not in event:
                        raise LifecycleError("lifecycle driver did not start the container")
                    name = event["name"]
                    if not isinstance(name, str) or not re.fullmatch(r"probe-cpu-[a-z0-9_]{4,32}", name):
                        raise LifecycleError("unexpected lifecycle container identity")
                    client_fd = _pidfd_open(event["client_pid"])
                    descriptors.callback(os.close, client_fd)
                    # The real host timer starts only after this Popen returns.
                    # This conservative bound prevents it from starting an
                    # independent cleanup subprocess before crash injection.
                    host_deadline = event["launched_at"] + wall_seconds
                    break
                if not driver.is_alive():
                    raise LifecycleError("lifecycle driver exited before startup")
            if client_fd is None:
                raise LifecycleError("lifecycle client startup timed out")
            while time.monotonic() < deadline:
                if not driver.is_alive() or _finished(client_fd):
                    raise LifecycleError("lifecycle launcher exited before the program started")
                ready = command(
                    "exec",
                    name,
                    "/usr/local/bin/python",
                    "-I",
                    "-c",
                    "from pathlib import Path; p=Path('/output/lifecycle-ready'); print(p.read_text() if p.exists() else '')",
                )
                if ready.returncode == 0 and ready.stdout.decode().strip() == marker:
                    report["program_started"] = True
                    break
                time.sleep(0.2)
            if not report["program_started"]:
                raise LifecycleError("lifecycle program startup timed out")
            top = command("top", name, "hpid")
            rows = top.stdout.decode().splitlines()
            if top.returncode or len(rows) < 3 or rows[0].strip() != "HPID":
                raise LifecycleError("unable to identify the running container processes")
            process_fds = []
            for row in rows[1:]:
                if not row.strip().isdecimal() or int(row.strip()) <= 1:
                    raise LifecycleError("invalid container process identity")
                fd = _pidfd_open(int(row.strip()))
                descriptors.callback(os.close, fd)
                process_fds.append(fd)
            if any(_finished(fd) for fd in process_fds):
                raise LifecycleError("container process exited before crash injection")
            if time.monotonic() + 2 >= host_deadline:
                raise LifecycleError("insufficient time to isolate crash cleanup from the host timer")
            # Neither Python's finally/timer nor the attached Podman client can
            # perform cleanup after this point. The external monitor must do it.
            _kill_pidfd(driver_fd)
            _kill_pidfd(client_fd)
            killed_at = time.monotonic()
            driver.join(timeout=5)
            # SIGKILL delivery is asynchronous. Wait for kernel-reported exit,
            # rather than treating a scheduling delay as a failed injection.
            if not all(select.select([fd], [], [], 5)[0] for fd in (driver_fd, client_fd)):
                raise LifecycleError("launcher death could not be confirmed")
            if time.monotonic() >= host_deadline:
                raise LifecycleError("launcher deaths were not confirmed before the host timer deadline")
            report["launchers_killed"] = True
            report["host_timer_excluded"] = True
            while time.monotonic() - killed_at < wall_seconds + cleanup_seconds:
                exists = command("container", "exists", name)
                if exists.returncode not in (0, 1):
                    raise LifecycleError("container removal inspection failed")
                report["container_processes_stopped"] = all(_finished(fd) for fd in process_fds)
                report["container_removed"] = exists.returncode == 1
                if report["container_processes_stopped"] and report["container_removed"]:
                    report["seconds_after_launcher_death"] = round(time.monotonic() - killed_at, 3)
                    return report
                time.sleep(0.25)
            raise LifecycleError("container outlived its independent cleanup deadline")
    except BaseException as error:
        error.lifecycle_report = report
        raise
    finally:
        receive.close()
        if driver.is_alive():
            driver.kill()
        driver.join(timeout=5)
        if name is not None:
            # Post-observation cleanup is intentionally outside the pass path.
            try:
                cleanup = command("rm", "--force", "--time=0", "--ignore", name, timeout=15)
                if cleanup.returncode:
                    raise LifecycleError("lifecycle test cleanup failed")
            except BaseException as error:
                error.lifecycle_report = report
                raise
            finally:
                driver.close()
        else:
            driver.close()
