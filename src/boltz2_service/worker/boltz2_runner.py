from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from boltz2_service.config import Boltz2Settings


class JobCanceledException(Exception):
    """Worker detected a cancel signal and shut down gracefully."""


# Runtime option keys that map directly to --flag value CLI args
NUMERIC_FLAGS = {
    "diffusion_samples",
    "sampling_steps",
    "recycling_steps",
    "step_scale",
    "max_parallel_samples",
    "seed",
    "sampling_steps_affinity",
    "diffusion_samples_affinity",
}

# Boolean flags that are presence-based (--flag if True)
BOOLEAN_SWITCH_FLAGS = {
    "use_potentials",
    "write_full_pae",
    "affinity_mw_correction",
    "vs",
}


class Boltz2Runner:
    def __init__(self, settings: Boltz2Settings) -> None:
        self.settings = settings

    def build_command(
        self, spec_path: Path, output_dir: Path, runtime_options: dict
    ) -> list[str]:
        cmd = [
            self.settings.boltz2_bin,
            "predict",
            str(spec_path),
            "--out_dir", str(output_dir),
            "--model", "boltz2",
            "--accelerator", "gpu",
            "--cache", self.settings.boltz2_cache_dir,
            "--override",
            "--devices", str(self.settings.boltz2_devices),
        ]

        if runtime_options.get("use_msa_server", True):
            cmd += ["--use_msa_server", "--msa_server_url", self.settings.msa_server_url]

        output_format = runtime_options.get("output_format", "mmcif")
        if output_format == "pdb":
            cmd += ["--output_format", "pdb"]

        for key in NUMERIC_FLAGS:
            value = runtime_options.get(key)
            if value is not None:
                cmd += [f"--{key}", str(value)]

        for key in BOOLEAN_SWITCH_FLAGS:
            if runtime_options.get(key):
                cmd.append(f"--{key}")

        return cmd

    def run(
        self,
        spec_path: Path,
        output_dir: Path,
        runtime_options: dict,
        line_handler: Callable[[str], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = self.build_command(spec_path, output_dir, runtime_options)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        captured: list[str] = []

        def consume_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                captured.append(line)
                if line_handler is not None:
                    line_handler(line.rstrip("\n"))
                print(line, end="", flush=True)

        output_thread = threading.Thread(target=consume_output, daemon=True)
        output_thread.start()

        total_timeout = self.settings.boltz2_run_timeout_seconds
        elapsed = 0.0
        while elapsed < total_timeout:
            poll_interval = min(5, total_timeout - elapsed)
            try:
                returncode = process.wait(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                elapsed += poll_interval
                if cancel_event is not None and cancel_event.is_set():
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    output_thread.join(timeout=2)
                    process.wait()
                    raise JobCanceledException()
        else:
            process.kill()
            output_thread.join(timeout=2)
            raise subprocess.TimeoutExpired(process.args, total_timeout)

        output_thread.join(timeout=2)
        stdout = "".join(captured)
        if returncode != 0:
            raise subprocess.CalledProcessError(
                returncode, command, output=stdout, stderr=stdout
            )
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")
