from __future__ import annotations

import subprocess
import sys
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[2]


def _declared_distributions() -> tuple[str, ...]:
    projects = []
    for pattern in ("packages/*/pyproject.toml", "tools/*/pyproject.toml"):
        for path in ROOT.glob(pattern):
            document = tomllib.loads(path.read_text(encoding="utf-8"))
            if document.get("project", {}).get("name"):
                projects.append(path.parent.relative_to(ROOT).as_posix())
    return tuple(sorted(projects))


DECLARED_DISTRIBUTIONS = _declared_distributions()


@pytest.mark.parametrize("project", DECLARED_DISTRIBUTIONS)
def test_declared_distribution_wheel_has_long_description(
    project: str, tmp_path: Path
) -> None:
    output = tmp_path / project.replace("/", "-")
    output.mkdir()
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--python",
            sys.executable,
            "--out-dir",
            str(output),
            str(ROOT / project),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1, wheels
    with zipfile.ZipFile(wheels[0]) as archive:
        members = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(members) == 1, members
        metadata = BytesParser(policy=policy.default).parsebytes(
            archive.read(members[0])
        )

    assert metadata.get_payload().strip(), wheels[0].name
