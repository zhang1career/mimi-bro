"""Load .env from a directory so CURSOR_API_KEY etc. are available when not set in shell."""
import os
from pathlib import Path


def get_env_value(root: Path, key: str) -> str | None:
    """Read a specific key from root/.env file without modifying os.environ."""
    env_file = Path(root).resolve() / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, value = line.partition("=")
            k = k.strip()
            if k != key:
                continue
            value = value.strip()
            if len(value) >= 2 and (value[0], value[-1]) in (('"', '"'), ("'", "'")):
                value = value[1:-1]
            return value
    return None


def load_dotenv_from_dir(root: Path) -> None:
    """Load root/.env into os.environ. Existing env vars are not overwritten."""
    env_file = Path(root).resolve() / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            if not key:
                continue
            if key in os.environ:
                continue
            value = value.strip()
            if len(value) >= 2 and (value[0], value[-1]) in (('"', '"'), ("'", "'")):
                value = value[1:-1]
            os.environ[key] = value
