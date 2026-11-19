"""Build an sdist, build its wheel, and verify a clean offline wheel install.

Run with the development environment: python scripts/check_install.py.
Build isolation may download setuptools; the installed tool needs no dependencies.
All commands and data checks run in temporary directories, away from real sources.
"""

from __future__ import annotations

import ast
from email.parser import Parser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def run(command: list[str], cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True,
                            text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def require_installed_module(module_path: Path, environment: Path) -> None:
    # macOS exposes the same temporary directory as /var and /private/var.
    require(module_path.resolve().is_relative_to(environment.resolve()),
            f"CLI module {module_path} is outside its installed environment {environment}")


def source_version() -> str:
    tree = ast.parse((ROOT / "src/orxanalytics/__init__.py").read_text())
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            return ast.literal_eval(statement.value)
    raise RuntimeError("src/orxanalytics/__init__.py must declare __version__")


def check_install() -> None:
    version = source_version()
    with tempfile.TemporaryDirectory(prefix="orx-analytics-install-") as tmp:
        work = Path(tmp)
        source = work / "source"
        source.mkdir()
        # Stage only distributable inputs. Never build with local databases,
        # historical bundles, editable-install metadata or runtime state.
        for name in ("pyproject.toml", "MANIFEST.in", "README.md",
                     "CHANGELOG.md", "CONTRIBUTING.md", "src", "tests",
                     "docs", "scripts"):
            path = ROOT / name
            if path.is_dir():
                shutil.copytree(path, source / name, ignore=shutil.ignore_patterns(
                    "__pycache__", "*.pyc", "*.egg-info", ".DS_Store"))
            elif path.is_file():
                shutil.copy2(path, source / name)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONNOUSERSITE"] = "1"
        env["ORX_ANALYTICS_HOME"] = str(work / "data")
        env["ORX_ANALYTICS_NOTIFY"] = "0"
        artifacts = work / "dist"
        print("Building source archive and wheel from that archive...", flush=True)
        # With no --sdist/--wheel flags, PyPA build first creates the sdist,
        # then builds the wheel from it (rather than from this checkout).
        run([sys.executable, "-m", "build", "--outdir", str(artifacts),
             str(source)], work, env)
        wheels = list(artifacts.glob("*.whl"))
        sdists = list(artifacts.glob("*.tar.gz"))
        require(len(wheels) == len(sdists) == 1, "Expected one wheel and one sdist")
        with tarfile.open(sdists[0]) as archive:
            names = archive.getnames()
            require(any(name.endswith("/README.md") for name in names),
                    "Source archive is missing README.md")
            require(any(name.endswith("/scripts/check_install.py") for name in names),
                    "Source archive is missing its installation check")
            require(not any("__pycache__" in name or name.endswith(".pyc")
                            or "/var/" in name or "/reports/" in name
                            or "/.orx/" in name for name in names),
                    "Source archive includes generated or local data")
        with zipfile.ZipFile(wheels[0]) as archive:
            names = archive.namelist()
            metadata = Parser().parsestr(archive.read(next(
                name for name in names if name.endswith(".dist-info/METADATA")
            )).decode())
            require(metadata["Name"] == "orx-analytics", "Wrong distribution name")
            require(metadata["Version"] == version, "Package and CLI versions differ")
            require(metadata["Requires-Python"] == ">=3.11", "Wrong Python requirement")
            require(metadata["Description-Content-Type"] == "text/markdown",
                    "Package metadata is missing the README")
            require(not any("extra ==" not in item for item in
                            metadata.get_all("Requires-Dist", [])),
                    "Tool has unexpected runtime dependencies")
            require("orxanalytics/__main__.py" in names, "Module entry missing from wheel")
            require(not any(name.endswith(".pyc") or name.startswith("tests/")
                            for name in names), "Wheel includes development files")
        environment = work / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        bindir = environment / ("Scripts" if os.name == "nt" else "bin")
        python = bindir / ("python.exe" if os.name == "nt" else "python")
        cli = bindir / ("orx-analytics.exe" if os.name == "nt" else "orx-analytics")
        print("Installing wheel offline into a fresh environment...", flush=True)
        run([str(python), "-m", "pip", "install", "--no-index", "--no-deps",
             str(wheels[0])], work, env)
        require(run([str(cli), "--version"], work, env) == version,
                "Console entry reports the wrong version")
        require(run([str(python), "-m", "orxanalytics", "--version"], work, env)
                == version, "Module entry reports the wrong version")
        help_text = run([str(cli), "--help"], work, env)
        require("findings" in help_text and "watch" in help_text, "CLI help is incomplete")
        installed = json.loads(run([str(python), "-c",
            "import json, orxanalytics; from importlib.metadata import version; "
            "print(json.dumps({'file': orxanalytics.__file__, "
            "'version': version('orx-analytics'), 'reader': orxanalytics.READER_VERSION}))"
        ], work, env))
        require_installed_module(Path(installed["file"]), environment)
        require(installed["version"] == version and version in installed["reader"],
                "Installed metadata, reader identity and CLI version disagree")
        require(not (work / "data").exists(), "Help/version unexpectedly creates data")
        project = work / "example project"
        project.mkdir()
        run([str(cli), "projects", "add", str(project), "--name", "Example"], work, env)
        require(str(project.resolve()) in run([str(cli), "projects", "list"], work, env),
                "Installed project registry does not retain the project")
        result = json.loads(run([str(cli), "collect", "--skip-global", "--json"], work, env))
        require(result == {"global": [], "orx": []}, "Isolated collection touched a source")
        override = work / "override"
        run([str(cli), "--data-dir", str(override), "projects", "list"], work, env)
        require((override / "analytics.db").exists(), "--data-dir did not override the environment")
        print(f"PASS: orx-analytics {version}; sdist, wheel, metadata, both CLI entries, "
              "registry, isolated collection and data-directory precedence")


if __name__ == "__main__":
    check_install()
