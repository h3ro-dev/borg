"""Prepare the included, inactive adapters with reproducible MLX base models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import urllib.request

from installer.config import write_private
from installer.downloads import executable, sha256


def inventory(doc: dict) -> list[dict]:
    root = Path(doc["home"])
    from installer.adapter_contract import read_manifest
    return read_manifest(root / "app")


def verify_weights(root: Path, row: dict) -> Path:
    weight = row["weights"]
    path = root / "app" / weight["path"]
    if (path.is_symlink() or not path.resolve().is_relative_to(root / "app/adapters")
            or path.stat().st_size != weight["bytes"] or sha256(path) != weight["sha256"]):
        raise ValueError("Included adapter weight does not match its release manifest")
    return path


def fetch(target: Path, url: str, digest: str, size: int) -> None:
    if target.exists() or target.is_symlink():
        if not target.is_symlink() and target.stat().st_size == size and sha256(target) == digest:
            return
        raise ValueError("Existing MLX model file differs from the manifest: " + target.name)
    fd, name = tempfile.mkstemp(prefix=".model-download-", dir=target.parent)
    temporary = Path(name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "BORG-installer/1"})
        with os.fdopen(fd, "wb") as output, urllib.request.urlopen(request, timeout=60) as response:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > size:
                    raise ValueError("MLX artifact exceeds its pinned size")
                output.write(chunk)
        if temporary.stat().st_size != size or sha256(temporary) != digest:
            raise ValueError("MLX artifact checksum mismatch")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def model_files(pin: dict) -> list[dict]:
    from urllib.parse import urlsplit
    files = [{"path": "config.json", **pin["config"]},
             {"path": "model.safetensors.index.json", **pin["tensor_index"]},
             {"path": "model.safetensors", "url": pin["model_weights"]["url"],
              "sha256": pin["model_weights"]["lfs_sha256"], "bytes": pin["model_weights"]["bytes"]},
             *pin["auxiliary_files"]]
    prefix = f"/{pin['model_id']}/resolve/{pin['revision']}/"
    for row in files:
        url = urlsplit(row["url"])
        if (url.scheme != "https" or url.netloc != "huggingface.co" or url.query or url.fragment
                or Path(row["path"]).name != row["path"] or row["path"] in {".", ".."}
                or url.path != prefix + row["path"] or not isinstance(row.get("bytes"), int)
                or not 0 < row["bytes"] < 4 * 1024**3):
            raise ValueError("Invalid immutable MLX model artifact")
    return files


def prepare(doc: dict, name: str) -> dict:
    if platform.system() != "Darwin" or platform.machine().lower() != "arm64":
        raise ValueError("The included MLX adapters require an Apple Silicon Mac")
    root = Path(doc["home"])
    rows = [row for row in inventory(doc) if name == "all" or row["name"] == name]
    if not rows:
        raise ValueError("Unknown adapter; run borg adapters list")
    for row in rows:
        verify_weights(root, row)
        model_files(row["release_base_pin"])
    env = {**os.environ, "UV_CACHE_DIR": str(root / "cache/uv"),
           "HF_HOME": str(root / "models/huggingface"), "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
           "HF_HUB_OFFLINE": "1", "HF_TOKEN": "", "PYTHONDONTWRITEBYTECODE": "1"}
    uv = str(executable(root, "uv", "uv"))
    runtime = root / "models/mlx/venv"
    runtime.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not runtime.exists():
        subprocess.run([uv, "venv", "--python", str(root / "mem0/venv/bin/python"), str(runtime)], env=env, check=True)
    python = runtime / "bin/python"
    base = subprocess.check_output([str(python), "-c", "import sys; print(sys.base_prefix)"], env=env, text=True).strip()
    if not Path(base).resolve().is_relative_to(root / "runtime/python"):
        raise ValueError("MLX Python must belong to this BORG installation")
    subprocess.run([uv, "pip", "sync", "--python", str(python), "--require-hashes",
                    str(root / "app/installer/mlx-requirements.lock")], env=env, check=True)
    prepared = []
    for row in rows:
        pin = row["release_base_pin"]
        base = root / "models/mlx" / (pin["model_id"].replace("/", "--") + "--" + pin["revision"])
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        if base.is_symlink() or not base.resolve().is_relative_to(root / "models/mlx"):
            raise ValueError("Model directory must remain inside this BORG home")
        for item in model_files(pin):
            fetch(base / item["path"], item["url"], item["sha256"], item["bytes"])
        adapter = verify_weights(root, row).parent
        prepared.append({"name": row["name"], "base_model": str(base), "adapter": str(adapter),
                         "state": "prepared_for_canary", "active": False,
                         "generate_command": [str(python), "-m", "mlx_lm.generate", "--model", str(base),
                                              "--adapter-path", str(adapter), "--max-tokens", "128"]})
    receipt = root / "models/mlx/prepared.json"
    write_private(receipt, json.dumps({"adapters": prepared}, indent=2) + "\n", replace=receipt.exists())
    return {"state": "prepared_for_canary", "adapters": prepared,
            "notice": "Base and adapter integrity verified. Historical training revisions are unknown; no adapter is promoted."}


def list_adapters(doc: dict) -> dict:
    root = Path(doc["home"])
    rows = []
    for row in inventory(doc):
        verify_weights(root, row)
        rows.append({"name": row["name"], "state": row["status"], "active": False,
                     "weights": "verified", "base_model": row["release_base_pin"]["model_id"],
                     "base_revision": row["release_base_pin"]["revision"]})
    return {"adapters": rows}
