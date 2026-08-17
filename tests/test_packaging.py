"""Phase 16 packaging tests: wheel/sdist contents, metadata, twine check, wheel smoke.

The distributions are built once per session with `python -m build` into a
temporary directory and inspected with the standard library only. The
fresh-venv smoke installs the wheel with pip and imports Sentinel from the
installed artifact — the decisive proof that the wheel is independently
installable (editable installs can hide packaging mistakes). All artifact
tests are `slow`-marked (existing slow CI job); the version tripwire is fast
and runs in the default suite.
"""

import email.parser
import importlib.metadata
import os
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import sentinel
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SENTINEL_SOURCE = REPO_ROOT / "sentinel"
EXPECTED_LUA_RESOURCES = (
    "sentinel/lua/token_bucket.lua",
    "sentinel/lua/sliding_window.lua",
)
FORBIDDEN_WHEEL_DIRS = ("tests/", "benchmarks/", "examples/")
EXPECTED_MODULES = sorted(p.name for p in SENTINEL_SOURCE.glob("*.py"))


def _run(
    cmd: list[str], *, cwd: Path | None = None, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)


def _check(
    cmd: list[str], *, cwd: Path | None = None, timeout: int = 600
) -> subprocess.CompletedProcess[str]:
    result = _run(cmd, cwd=cwd, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(scope="module")
def dist_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    _check([sys.executable, "-m", "build", "--outdir", str(out)], cwd=REPO_ROOT)
    return out


@pytest.fixture(scope="module")
def wheel_path(dist_dir: Path) -> Path:
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


@pytest.fixture(scope="module")
def sdist_path(dist_dir: Path) -> Path:
    sdists = list(dist_dir.glob("*.tar.gz"))
    assert len(sdists) == 1, sdists
    return sdists[0]


def test_version_matches_distribution_metadata() -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    distribution_name = pyproject["project"]["name"]
    try:
        metadata_version = importlib.metadata.version(distribution_name)
    except importlib.metadata.PackageNotFoundError:
        metadata_version = pyproject["project"]["version"]
    assert sentinel.__version__ == metadata_version


@pytest.mark.slow
def test_wheel_contains_sentinel_modules_and_resources(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        names = set(wheel.namelist())
    for module in EXPECTED_MODULES:
        assert f"sentinel/{module}" in names, module
    assert "sentinel/py.typed" in names
    for resource in EXPECTED_LUA_RESOURCES:
        assert resource in names, resource


@pytest.mark.slow
def test_wheel_top_level_entries_are_only_sentinel_and_dist_info(
    wheel_path: Path,
) -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    distribution_name = pyproject["project"]["name"].replace("-", "_")
    dist_info = f"{distribution_name}-{sentinel.__version__}.dist-info"
    with zipfile.ZipFile(wheel_path) as wheel:
        top_level = {name.split("/")[0] for name in wheel.namelist()}
    assert top_level == {"sentinel", dist_info}


@pytest.mark.slow
def test_wheel_leaks_no_repository_infrastructure(wheel_path: Path) -> None:
    with zipfile.ZipFile(wheel_path) as wheel:
        names = wheel.namelist()
    leaked = [name for name in names if name.startswith(FORBIDDEN_WHEEL_DIRS)]
    assert not leaked, f"wheel leaks development infrastructure: {leaked}"


@pytest.mark.slow
def test_sdist_contains_sources_and_readme(sdist_path: Path) -> None:
    with tarfile.open(sdist_path, "r:gz") as sdist:
        names = set(sdist.getnames())
    root = next(name.split("/")[0] for name in names if name)
    for member in ("README.md", "LICENSE", "pyproject.toml", *EXPECTED_LUA_RESOURCES):
        assert f"{root}/{member}" in names, member


@pytest.mark.slow
def test_wheel_metadata_matches_pyproject(wheel_path: Path) -> None:
    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_name = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = email.parser.Parser().parsestr(wheel.read(metadata_name).decode("utf-8"))
    assert metadata["Name"] == pyproject["project"]["name"]
    assert metadata["Version"] == sentinel.__version__
    assert metadata["Requires-Python"] == pyproject["project"]["requires-python"]
    runtime_dependencies = [
        dep for dep in metadata.get_all("Requires-Dist", []) if "extra ==" not in dep
    ]
    expected_dependencies = pyproject["project"]["dependencies"]
    assert {
        (req.name, frozenset(str(spec) for spec in req.specifier))
        for req in map(Requirement, runtime_dependencies)
    } == {
        (req.name, frozenset(str(spec) for spec in req.specifier))
        for req in map(Requirement, expected_dependencies)
    }


@pytest.mark.slow
def test_twine_check_accepts_distributions(wheel_path: Path, sdist_path: Path) -> None:
    _check([sys.executable, "-m", "twine", "check", str(wheel_path), str(sdist_path)])


@pytest.mark.slow
def test_wheel_installs_and_imports_in_fresh_venv(tmp_path: Path, wheel_path: Path) -> None:
    venv_dir = tmp_path / "venv"
    _check([sys.executable, "-m", "venv", str(venv_dir)])
    venv_python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    assert venv_python.is_file()
    _check([str(venv_python), "-m", "pip", "install", str(wheel_path)], timeout=900)
    probe = (
        "import importlib.resources\n"
        "import sentinel\n"
        "import sentinel.lua\n"
        "import sentinel.http\n"
        "for name in ('lua/token_bucket.lua', 'lua/sliding_window.lua'):\n"
        "    path = importlib.resources.files('sentinel').joinpath(name)\n"
        "    assert path.is_file(), name\n"
        "print(sentinel.__version__)\n"
    )
    result = _run([str(venv_python), "-c", probe])
    assert result.returncode == 0, result.stdout + result.stderr
