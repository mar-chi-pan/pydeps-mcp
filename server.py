import logging
import pathlib
import tomllib
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from typing import TypedDict

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
mcp = MCPServer("pydeps")


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


if __name__ == "__main__":
    mcp.run()
