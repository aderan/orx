"""Generic shell adapter: any executable, used by every automated test.

``prompt_transport`` chooses delivery: ``stdin`` (prompt on stdin), an argv
slot containing ``{prompt}``, or ``{prompt_file}`` (prompt written to a file,
path substituted). Exit code is the result. ``actual_effort`` stays
``provider_default`` unless the process prints a line
``ORX_ACTUAL_EFFORT=<value>`` (tests use this; real shells stay default).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from orx.adapters.base import (
    EFFORT_PROVIDER_DEFAULT,
    EFFORT_SOURCE_REPORTED,
    EffortOutcome,
    Launch,
    ProbeReport,
    scan_marker,
)


class ShellAdapter:
    harness = "shell"

    def probe(self) -> ProbeReport:
        return ProbeReport(ok=True, detail="shell adapter launches as-is")

    def _substitute(self, profile, prompt: str, scratch: Path) -> tuple[list[str], str | None, Path | None]:
        transport = profile.prompt_transport
        prompt_path: Path | None = None
        stdin_text: str | None = None

        rendered: list[str] = []
        for arg in profile.args:
            if "{prompt}" in arg:
                if transport != "argument":
                    raise ValueError(
                        "profile args contain {prompt} but prompt_transport is"
                        f" '{transport}' (expected 'argument')"
                    )
                arg = arg.replace("{prompt}", prompt)
            elif "{prompt_file}" in arg:
                if transport != "file":
                    raise ValueError(
                        "profile args contain {prompt_file} but prompt_transport is"
                        f" '{transport}' (expected 'file')"
                    )
                prompt_path = self._write_prompt(scratch, prompt)
                arg = arg.replace("{prompt_file}", str(prompt_path))
            rendered.append(arg)

        if transport == "stdin":
            stdin_text = prompt
        elif transport == "file" and prompt_path is None:
            raise ValueError(
                "prompt_transport = 'file' requires a {prompt_file} slot in args"
            )
        return rendered, stdin_text, prompt_path

    def _write_prompt(self, scratch: Path, prompt: str) -> Path:
        scratch.mkdir(parents=True, exist_ok=True)
        path = scratch / "prompt.txt"
        path.write_text(prompt)
        return path

    def _launch(self, *, root: Path, scratch: Path, profile, prompt: str, timeout: int) -> Launch:
        args, stdin_text, prompt_path = self._substitute(profile, prompt, scratch)
        return Launch(
            argv=[profile.executable, *args],
            cwd=root,
            timeout=timeout,
            stdin_text=stdin_text,
            label=f"shell:{profile.name}",
            planned_effort=EffortOutcome(requested=profile.effort.value),
        )

    def build_worker_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        return self._launch(root=root, scratch=scratch, profile=profile, prompt=prompt, timeout=timeout)

    def build_planner_launch(self, *, root, scratch, profile, prompt, timeout, schema_path=None) -> Launch:
        return self._launch(root=root, scratch=scratch, profile=profile, prompt=prompt, timeout=timeout)

    def build_verifier_launch(self, *, root, scratch, profile, prompt, timeout) -> Launch:
        return self._launch(root=root, scratch=scratch, profile=profile, prompt=prompt, timeout=timeout)

    def effort_outcome(self, launch: Launch, run_result) -> EffortOutcome:
        requested = launch.planned_effort.requested if launch.planned_effort else "medium"
        reported = scan_marker(run_result.stdout, "ORX_ACTUAL_EFFORT")
        if reported:
            return EffortOutcome(
                requested=requested, actual=reported, source=EFFORT_SOURCE_REPORTED
            )
        return EffortOutcome(requested=requested, actual=EFFORT_PROVIDER_DEFAULT, source=None)

    def extract_text(self, launch: Launch, run_result) -> str:
        if launch.last_message_path and launch.last_message_path.exists():
            return launch.last_message_path.read_text()
        return run_result.stdout
