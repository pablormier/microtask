"""Create the Compose demo once and preserve it across container restarts."""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from workspace import is_data_workspace, seed_demo_workspace


def seed(destination: Path) -> None:
    if is_data_workspace(destination):
        return
    if destination.exists():
        raise SystemExit(f"{destination} already exists and is not a workspace")

    with tempfile.TemporaryDirectory(prefix=".demo-", dir=destination.parent) as staging:
        root = Path(staging) / "workspace"
        seed_demo_workspace(root)
        if shutil.which("git") is not None:
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "Seed shared demo workspace"], cwd=root, check=True)
        root.rename(destination)


if __name__ == "__main__":
    seed(Path(sys.argv[1]))
