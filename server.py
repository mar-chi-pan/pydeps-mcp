import asyncio
import logging
import pathlib
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TypedDict

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

MAX_CONCURRENT = 10  # be polite to PyPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    client: httpx.AsyncClient


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[AppContext]:
    async with httpx.AsyncClient(timeout=10) as client:
        yield AppContext(client=client)


mcp = MCPServer("pydeps", lifespan=lifespan)


class RawRequirements(TypedDict):
    source: str
    dependencies: list[str]
    skipped: list[str]


class RequirementDetails(TypedDict):
    name: str
    specifier: str | None
    extras: list[str]
    marker: str | None


class DependencyList(TypedDict):
    source: str
    dependencies: list[RequirementDetails]
    unparsed: list[str]
    skipped: list[str]


class LatestPackage(TypedDict):
    name: str
    latest_version: str
    released: str | None
    requires_python: str | None
    provides_extra: list[str]
    changelog_url: str | None


class Status(StrEnum):
    UNCONSTRAINED = "unconstrained"  # no specifier
    ALLOWED = "allowed"  # the constraint already permits the latest version
    BLOCKED = "blocked"  # the constraint excludes the latest version


URGENCY = {Status.BLOCKED: 0, Status.ALLOWED: 1, Status.UNCONSTRAINED: 2}


class AuditEntry(TypedDict):
    name: str
    specifier: str | None
    extras: list[str]
    marker: str | None
    latest_version: str
    status: Status
    major_bump: bool
    changelog_url: str | None


class AuditReport(TypedDict):
    source: str
    summary: dict[str, int]  # e.g. {"blocked": 2, "allowed": 10, ...}
    entries: list[AuditEntry]  # sorted: blocked + major_bump first
    unparsed: list[str]
    skipped: list[str]
    errors: dict[str, str]


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
                "skipped": [],
            }
    if requirements_txt_file.is_file():
        lines = [
            line.split("#", 1)[0].strip()
            for line in requirements_txt_file.read_text(encoding="utf-8").splitlines()
        ]
        # Option lines (-r includes, -c constraints, -e, --index-url, ...)
        # are not followed; report them so the caller knows they were ignored.
        return {
            "source": "requirements.txt",
            "dependencies": [
                line for line in lines if line and not line.startswith("-")
            ],
            "skipped": [line for line in lines if line.startswith("-")],
        }
    return None


def read_dependencies(project_path: str) -> DependencyList:
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
        "skipped": dependencies_info["skipped"],
    }


async def fetch_latest(client: httpx.AsyncClient, package: str) -> LatestPackage:
    logger.info("Getting latest release for %s", package)
    package_name = canonicalize_name(package)
    try:
        response = await client.get(f"https://pypi.org/pypi/{package_name}/json")
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
        the call. requirements.txt option lines (e.g. `-r other.txt`,
        `-c constraints.txt`, `-e .`) are not followed and are listed in
        `skipped`; read those files yourself if their contents matter.
    """
    return read_dependencies(project_path)


@mcp.tool()
async def check_latest(package: str, ctx: Context[AppContext]) -> LatestPackage:
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
    return await fetch_latest(ctx.request_context.lifespan_context.client, package)


async def _fetch_limited(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, name: str
) -> LatestPackage:
    async with sem:
        return await fetch_latest(client, name)


def upgrade_status(specifier_set: SpecifierSet | None, version: str) -> Status:
    if specifier_set is None:
        return Status.UNCONSTRAINED

    if specifier_set.contains(version):
        return Status.ALLOWED
    return Status.BLOCKED


def max_allowed_major(specifier: Specifier) -> int | None:
    """Highest major version this specifier permits, or None if unbounded
    above."""
    version = Version(specifier.version.removesuffix(".*"))
    match specifier.operator:
        case "<":
            # <2 or <2.0 allows only 1.x; <2.5 still allows 2.x
            rest = version.release[1:]
            return version.major - 1 if all(x == 0 for x in rest) else version.major
        case "<=" | "==" | "~=":
            return version.major
        case _:  # >, >=, != don't cap the major
            return None


def major_bump(specifier_set: SpecifierSet | None, version: str) -> bool:
    if specifier_set is None:
        return False
    try:
        latest_major = Version(version).major
        caps = [m for s in specifier_set if (m := max_allowed_major(s)) is not None]
    except InvalidVersion:
        return False
    return bool(caps) and latest_major > min(caps)


@mcp.tool()
async def audit_project(project_path: str, ctx: Context[AppContext]) -> AuditReport:
    """Run a full dependency health check on a Python project in one call.

    Prefer this to calling check_latest for every package.

    Args:
        project_path: Absolute path to the project root directory
            (symlinks are fine).

    Returns:
        entries, sorted by urgency (blocked and major_bump first):
        - status "blocked": the declared constraint excludes the latest
            version; upgrading requires changing it.
        - status "allowed": the constraint already permits the latest version.
        - status "unconstrained": no version constraint is declared.
        - major_bump: the latest version is a major release above the highest
            version the constraint allows; check changelog_url for breaking
            changes before upgrading.
        summary counts entries per status, plus errors. Packages that could not
        be checked (e.g. not on PyPI) are in `errors`; lines that could not be
        parsed are in `unparsed`; requirements.txt option lines such as
        `-r other.txt` are not followed and are listed in `skipped`.
    """
    dependencies = read_dependencies(project_path)
    client = ctx.request_context.lifespan_context.client
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    # Each package once, even if it appears twice with different markers
    names = list(
        dict.fromkeys(
            canonicalize_name(d["name"]) for d in dependencies["dependencies"]
        )
    )
    results = await asyncio.gather(
        *(_fetch_limited(client, sem, n) for n in names),
        return_exceptions=True,
    )

    latest_by_name: dict[str, LatestPackage] = {}
    errors: dict[str, str] = {}
    for name, result in zip(names, results):
        if isinstance(result, ToolError):
            errors[name] = str(result)
        elif isinstance(result, BaseException):
            raise result
        else:
            latest_by_name[name] = result

    audit_entries: list[AuditEntry] = []
    summary: dict[str, int] = {s.value: 0 for s in Status}
    for parsed in dependencies["dependencies"]:
        latest = latest_by_name.get(canonicalize_name(parsed["name"]))
        if latest is None:
            continue  # already recorded in errors

        specifier_set = (
            SpecifierSet(parsed["specifier"], prereleases=True)
            if parsed["specifier"]
            else None
        )
        status = upgrade_status(specifier_set, latest["latest_version"])
        audit_entries.append(
            {
                "name": latest["name"],
                "specifier": parsed["specifier"],
                "extras": parsed["extras"],
                "marker": parsed["marker"],
                "latest_version": latest["latest_version"],
                "status": status,
                "major_bump": major_bump(specifier_set, latest["latest_version"]),
                "changelog_url": latest["changelog_url"],
            }
        )
        summary[status.value] += 1
    summary["errors"] = len(errors)
    audit_entries.sort(key=lambda x: (URGENCY[x["status"]], not x["major_bump"]))
    return {
        "source": dependencies["source"],
        "unparsed": dependencies["unparsed"],
        "skipped": dependencies["skipped"],
        "entries": audit_entries,
        "errors": errors,
        "summary": summary,
    }


if __name__ == "__main__":
    mcp.run()
