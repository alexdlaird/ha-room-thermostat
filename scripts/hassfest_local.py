"""Run Home Assistant's hassfest against this custom integration without Docker.

Clones a sparse checkout of home-assistant/core (matching the installed HA version)
into .hassfest/core on first use, then runs:
    python -m script.hassfest --action validate --integration-path <this integration>
This mirrors the official hassfest GitHub action's entrypoint.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from homeassistant.const import __version__ as ha_version

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_DIR = REPO_ROOT / ".hassfest" / "core"
INTEGRATION = REPO_ROOT / "custom_components" / "room_thermostat"


def run(cmd: list[str], **kwargs: object) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, check=True, **kwargs)  # type: ignore[call-overload]


def main() -> int:
    if not (CORE_DIR / "script" / "hassfest").is_dir():
        CORE_DIR.parent.mkdir(parents=True, exist_ok=True)
        run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--branch",
                ha_version,
                "--filter=blob:none",
                "--sparse",
                "https://github.com/home-assistant/core",
                str(CORE_DIR),
            ]
        )
        run(["git", "-C", str(CORE_DIR), "sparse-checkout", "set", "script", "homeassistant"])
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "script.hassfest",
            "--action",
            "validate",
            "--integration-path",
            str(INTEGRATION),
        ],
        cwd=CORE_DIR,
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
