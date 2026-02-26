from pathlib import Path
import sys


def validate_workspace(path: Path, verbose: bool = True):
    if not path.exists():
        print(f"[broker][error] workspace does not exist: {path}")
        sys.exit(2)

    if not path.is_dir():
        print(f"[broker][error] workspace is not a directory: {path}")
        sys.exit(2)

    if verbose:
        print(f"[broker] using workspace: {path}")
