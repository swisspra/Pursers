"""Strict source allowlists for the unpublished Personal import component."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist


PACKAGE = "pursers_personal_import"
RUNTIME_MODULES = {
    "__init__",
    "bind_identities",
    "journal",
    "locked_store",
    "native_import",
    "personal_import",
    "prepare_apply_rehearsal",
    "reconcile",
    "safe_tree",
    "scrub",
    "sqlite_store",
    "transactional_sqlite",
}
SDIST_FILES = sorted(
    {
        "MANIFEST.in",
        "LICENSE",
        "PERSONAL-IMPORT.md",
        "pyproject.toml",
        "setup.py",
        *(
            "src/pursers_personal_import/__init__.py"
            if name == "__init__"
            else f"{name}.py"
            for name in RUNTIME_MODULES
        ),
    }
)


class StrictBuildPy(build_py):
    def find_package_modules(self, package: str, package_dir: str):
        if package != PACKAGE:
            return []
        selected = []
        for module in sorted(RUNTIME_MODULES):
            filename = (
                Path("src/pursers_personal_import/__init__.py")
                if module == "__init__"
                else Path(f"{module}.py")
            )
            if not filename.is_file():
                raise RuntimeError(f"runtime module allowlist is incomplete: {module}")
            selected.append((PACKAGE, module, str(filename)))
        if {item[1] for item in selected} != RUNTIME_MODULES:
            raise RuntimeError("runtime module allowlist is inconsistent")
        return selected


class StrictSdist(sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        missing = [name for name in SDIST_FILES if not Path(name).is_file()]
        if missing:
            raise RuntimeError("source allowlist is incomplete")
        super().make_release_tree(base_dir, SDIST_FILES)


setup(
    packages=[PACKAGE],
    package_dir={PACKAGE: "src/pursers_personal_import"},
    include_package_data=False,
    cmdclass={"build_py": StrictBuildPy, "sdist": StrictSdist},
)
