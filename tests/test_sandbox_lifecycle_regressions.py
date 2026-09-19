"""Deterministic host-observer regressions; real containment is a separate gate."""
import io
import json
from types import SimpleNamespace

import pytest

from probe_core import sandbox as module


@pytest.mark.parametrize("timer_during_final_select", [True, False])
def test_eof_after_attestation_retains_observed_timer_outcome(tmp_path, monkeypatch, timer_during_final_select):
    """The last select may return EOF after timeout(), with no next loop turn."""
    limits = module.SandboxLimits(wall_seconds=10)
    attestation = {"uid": 1000, "cap_eff": "0000000000000000", "seccomp": "2", "no_new_privs": "1",
        "socket_denied": True, "input_readonly": True, "root_readonly": True,
        "memory_max": str(limits.memory_bytes), "pids_max": str(limits.pids),
        "cpu_max": "100000 100000", "interfaces": ["lo"]}
    frame = b"PROBE_RUNTIME:" + json.dumps(attestation).encode() + b"\nPROBE_TIME_STARTED\n"
    streams = [io.BytesIO() for _ in range(3)]
    process = SimpleNamespace(stdin=streams[0], stdout=streams[1], stderr=streams[2],
                              pid=1234567, returncode=255 if timer_during_final_select else 0)
    process.poll = lambda: process.returncode
    process.wait = lambda timeout: process.returncode
    timers, commands, outcomes = [], [], []

    class Timer:
        def __init__(self, seconds, callback):
            assert seconds == limits.wall_seconds
            self.callback = callback
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            outcomes.append("cancelled_timer")

    class Selector:
        def __init__(self):
            self.keys = {}
            self.turn = 0

        def register(self, stream, events, channel):
            key = SimpleNamespace(fd=id(stream), fileobj=stream, data=channel)
            self.keys[id(stream)] = key

        def get_map(self):
            return self.keys

        def select(self, timeout):
            self.turn += 1
            if self.turn == 1:
                return [(self.keys[id(process.stdout)], 1)]
            assert self.turn == 2, "both EOFs end the loop before its next timeout check"
            if timer_during_final_select:
                timers[0].callback()
                outcomes.append("timer_fired")
            return [(key, 1) for key in self.keys.values()]

        def unregister(self, stream):
            del self.keys[id(stream)]

        def close(self):
            pass

    reads = {id(process.stdout): [frame, b""], id(process.stderr): [b""]}
    monkeypatch.setattr(module.os, "read", lambda fd, size: reads[fd].pop(0))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs:
                        commands.append(command) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(module.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(module.threading, "Timer", Timer)
    sandbox = module.PodmanSandbox(image="sha256:" + "a" * 64, workspace=tmp_path)
    monkeypatch.setattr(sandbox, "check_runtime", lambda: {})
    result = sandbox.run("pass", limits=limits)
    assert result.termination_reason == ("wall_time_limit" if timer_during_final_select else None)
    assert result.stdout == "PROBE_TIME_STARTED\n" and result.artifacts == ()
    assert ("timer_fired" in outcomes) is timer_during_final_select
    assert all(stream.closed for stream in streams)
    assert commands[-1][2:5] == ["rm", "--force", "--ignore"]
