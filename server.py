import httpx
import logging
import pathlib
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from typing import TypedDict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    client: httpx.Client


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    with httpx.Client(timeout=10) as client:
        yield AppContext(client=client)


mcp = MCPServer("pydeps", lifespan=lifespan)


class RawRequirements(TypedDict):
    source: str
    dependencies: list[str]


class RequirementDetails(TypedDict):
    name: str
    specifier: str | None
    extras: list[str]
    marker: str | None


class DependencyList(TypedDict):
    source: str
    dependencies: list[RequirementDetails]
    unparsed: list[str]


class LatestPackage(TypedDict):
    name: str
    latest_version: str
    released: str | None
    requires_python: str | None
    provides_extra: list[str]
    changelog_url: str | None


def get_raw_dependencies(real_path: pathlib.Path) -> RawRequirements | None:
    pyproject_toml_file = real_path / "pyproject.toml"
    requirements_txt_file = real_path / "requirements.txt"

    if pyproject_toml_file.is_file():
        with pyproject_toml_file.open("rb") as pyproject_toml:
            try:
                project = tomllib.load(pyproject_toml).get("project", {})
            except tomllib.TOMLDecodeError as e:
                raise ToolError(
                    f"pyproject.toml in {real_path} is not valid TOML"
                ) from e
        if (
            "dependencies" in project.get("dynamic", [])
            and not requirements_txt_file.is_file()
        ):
            raise ToolError(
                f"{pyproject_toml_file} declares dependencies as dynamic, so "
                "they are defined by the build backend (e.g. "
                "[tool.setuptools.dynamic] or setup.py), not in "
                "[project].dependencies. Read those to find them."
            )
        dependencies = project.get("dependencies")
        if dependencies is not None:
            return {
                "source": "pyproject.toml",
                "dependencies": dependencies,
            }
    if requirements_txt_file.is_file():
        lines = [
            line.split("#", 1)[0].strip()
            for line in requirements_txt_file.read_text(encoding="utf-8").splitlines()
        ]
        return {
            "source": "requirements.txt",
            "dependencies": [
                line for line in lines if line and not line.startswith("-")
            ],
        }
    return None


@mcp.tool()
def list_dependencies(project_path: str) -> DependencyList:
    """List a Python project's declared dependencies.

    Call this first when asked about a project's dependencies, before
    checking versions or planning upgrades. Reads [project].dependencies
    from pyproject.toml, falling back to requirements.txt.

    Args:
        project_path: Absolute path to the project root directory, can
                      include symlinks.

    Returns:
        The dependencies found and the file they came from. Lines that
        could not be parsed are listed in `unparsed` rather than failing
        the call.
    """
    real_path = pathlib.Path(project_path).expanduser()
    logger.info("Looking for the dependency list in %s", real_path)
    if not real_path.is_dir():
        raise ToolError(
            f"{project_path} directory not found, use absolute path"
            " (symlinks are fine)."
        )

    dependencies_info = get_raw_dependencies(real_path)
    if dependencies_info is None:
        raise ToolError(
            f"No dependencies declared in {real_path}: no "
            "[project].dependencies in pyproject.toml and no "
            "requirements.txt. Check this is the project root."
        )

    parsed: list[RequirementDetails] = []
    unparsed: list[str] = []
    for dep in dependencies_info["dependencies"]:
        try:
            parsed_dep = Requirement(dep)
            parsed.append(
                {
                    "name": parsed_dep.name,
                    "specifier": str(parsed_dep.specifier) or None,
                    "extras": sorted(parsed_dep.extras),
                    "marker": str(parsed_dep.marker) if parsed_dep.marker else None,
                }
            )
        except InvalidRequirement:
            unparsed.append(dep)
    return {
        "source": dependencies_info["source"],
        "dependencies": parsed,
        "unparsed": unparsed,
    }


@mcp.tool()
def check_latest(package: str, ctx: Context[AppContext]) -> LatestPackage:
    """Get the latest package version from PyPI.

    Call this after list_dependencies, once per package you need to check.
    Calls PyPI and returns the latest release if found, including
    "changelog_url" which could name breaking changes.

    Args:
        package: Package name as it appears in the dependency list
                (case and -/_ differences are fine).

    Returns:
        Latest version, its release date, supported Python versions,
        and changelog_url (null if the project doesn't publish one).
        Read the changelog to judge whether an upgrade has breaking changes.
    """
    logger.info("Getting latest release for %s", package)
    package_name = canonicalize_name(package)
    client = ctx.request_context.lifespan_context.client
    try:
        response = client.get(f"https://pypi.org/pypi/{package_name}/json")
    except httpx.HTTPError as e:
        raise ToolError(f"Could not reach PyPI ({e}); retry later.") from e

    if response.status_code == 404:
        raise ToolError(
            f"'{package}' not found on PyPI. Check the spelling, or it may "
            "come from a private index."
        )
    if response.is_error:
        raise ToolError(
            f"PyPI returned {response.status_code} for '{package}'; retry later."
        )

    content = response.json()

    info = content["info"]
    files: list[dict[str, str]] = content.get("urls") or []
    released = files[0]["upload_time_iso_8601"] if files else None
    project_urls: dict[str, str] = info.get("project_urls") or {}
    by_lower = {k.lower(): v for k, v in project_urls.items()}
    changelog_url = next(
        (
            by_lower[k]
            for k in ("changelog", "changes", "release notes", "history")
            if k in by_lower
        ),
        None,
    )

    return {
        "name": canonicalize_name(info["name"]),
        "latest_version": info["version"],
        "provides_extra": info.get("provides_extra") or [],
        "released": released,
        "requires_python": info.get("requires_python") or None,
        "changelog_url": changelog_url,
    }


if __name__ == "__main__":
    mcp.run()
