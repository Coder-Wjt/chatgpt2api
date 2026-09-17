import os
import secrets
import tempfile
from pathlib import Path

_runtime = tempfile.TemporaryDirectory(prefix="chatgpt2api-security-")
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(_runtime.name) / "test.db")
os.environ["CHATGPT2API_AUTH_KEY"] = secrets.token_urlsafe(32)

# Service singletons must never read or recover a developer's real task files.
import importlib

_config_module = importlib.import_module("services.config")
_config_module.DATA_DIR = Path(_runtime.name)
_config_module.CONFIG_FILE = Path(_runtime.name) / "config.json"
_config_module.config.path = _config_module.CONFIG_FILE
