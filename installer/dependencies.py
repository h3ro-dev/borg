"""Install verified runtimes and dependency locks without touching global profiles."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import urllib.request

from installer.config import write_private, managed_python
from installer.downloads import executable, unpack

SOURCE = Path(__file__).resolve().parent


def run(command: list[str], *, env: dict | None = None, cwd: Path | None = None) -> None:
    subprocess.run(command, env=env, cwd=cwd, check=True)


def prepare(doc: dict, *, system_dependencies: bool = False) -> None:
    root = Path(doc["home"])
    from installer.config import validate_managed_paths
    validate_managed_paths(doc)
    python = root / "mem0/venv/bin/python"
    if python.exists() or python.is_symlink():
        managed_python(root, python)
    # The locked FalkorDB Lite wheel bundles its server, module and crypto libraries.
    # Do not infer machine-wide dependencies from older source-build instructions.
    for name in ["uv", "node", "qdrant", "beads", "cloudflared"]:
        unpack(root, name)
    uv = str(executable(root, "uv", "uv"))
    env = {**os.environ, "UV_CACHE_DIR": str(root / "cache/uv"),
           "UV_PYTHON_INSTALL_DIR": str(root / "runtime/python"), "UV_NO_MODIFY_PATH": "1"}
    run([uv, "python", "install", "3.12.12", "--no-bin"], env=env)
    managed = subprocess.check_output([uv, "python", "find", "--managed-python", "3.12.12"], env=env, text=True).strip()
    managed_python(root, Path(managed))
    if not Path(managed).resolve().is_relative_to(root / "runtime/python"):
        raise RuntimeError("The Python interpreter must belong to this BORG installation")
    python = root / "mem0/venv/bin/python"
    correct_python = python.exists() and Path(subprocess.check_output(
        [str(python), "-c", "import sys; print(sys.base_prefix)"], text=True).strip()).is_relative_to(root / "runtime/python")
    if not correct_python:
        run([uv, "venv", "--clear", "--python", managed, str(python.parent.parent)], env=env)
    run([uv, "pip", "sync", "--python", str(python), "--require-hashes", str(SOURCE / "requirements.lock")], env=env)
    # Linux Ollama archives use zstd, supplied by the locked Python environment.
    unpack(root, "ollama")
    node = executable(root, "node", "node")
    npm = node.parent / "npm"
    npm_root = root / "runtime/npm"
    npm_root.mkdir(mode=0o700, exist_ok=True)
    for name in ["package.json", "package-lock.json"]:
        target = npm_root / name
        if target.exists() or target.is_symlink():
            from installer.installation import check_component
            check_component(target, SOURCE / "npm" / name)
        else:
            write_private(target, (SOURCE / "npm" / name).read_text())
    env.update({"PATH": str(node.parent) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                "npm_config_cache": str(root / "cache/npm"), "npm_config_userconfig": "/dev/null",
                "npm_config_registry": "https://registry.npmjs.org", "PLAYWRIGHT_BROWSERS_PATH": str(root / "runtime/browsers")})
    run([str(npm), "ci", "--prefix", str(npm_root), "--ignore-scripts", "--no-audit", "--no-fund"], env=env)
    from installer import browser
    chrome = str(browser.prepare(doc))
    command = [str(python), "-B", "-c", "import sys; from playwright.sync_api import sync_playwright\nwith sync_playwright() as p:\n b=p.chromium.launch(headless=True, executable_path=sys.argv[1])\n page=b.new_page()\n page.set_content('<title>BORG browser ready</title>')\n assert page.title() == 'BORG browser ready'\n b.close()", chrome]
    run(command, env=env)
    from installer.config import read_private
    connector_path = root / "borg-context/config.json"
    connector = read_private(connector_path)
    connector["browser"]["chrome_path"] = chrome
    write_private(connector_path, json.dumps(connector, indent=2) + "\n", replace=True)
    versions = {"python": subprocess.check_output([str(python), "--version"], text=True).strip(),
                "node": subprocess.check_output([str(node), "--version"], text=True).strip(),
                "codex": subprocess.check_output([str(npm_root / "node_modules/.bin/codex"), "--version"], env=env, text=True).strip()}
    receipt = root / "runtime/installed.json"
    write_private(receipt, json.dumps({"schema": "borg-runtimes/v1", "versions": versions,
                                      "browser_executable": chrome,
                                      "browser_artifact": browser.specification()[2]}, indent=2) + "\n", replace=receipt.exists())


def pull_models(doc: dict) -> dict:
    root = Path(doc["home"])
    ollama = executable(root, "ollama", "ollama")
    url = f"http://127.0.0.1:{doc['ports']['ollama']}"
    env = {**os.environ, "OLLAMA_HOST": url, "OLLAMA_MODELS": str(root / "models/ollama"), "OLLAMA_NO_CLOUD": "1"}
    names = list(dict.fromkeys(doc["models"][key] for key in ["extraction", "graph", "embedding"]))
    lock = json.loads((SOURCE / "models.lock.json").read_text())["models"]
    for name in names:
        if name not in lock:
            raise ValueError("Model has no reviewed manifest lock: " + name)
        run([str(ollama), "pull", name], env=env)
    with urllib.request.urlopen(url + "/api/tags", timeout=10) as response:
        actual = {row["name"]: row["digest"] for row in json.load(response)["models"]}
    for name in names:
        if actual.get(name) != lock[name]["manifest_digest"].removeprefix("sha256:"):
            # Some Ollama releases include the algorithm prefix in /api/tags.
            if actual.get(name) != lock[name]["manifest_digest"]:
                raise RuntimeError("Installed model digest differs from reviewed manifest: " + name)
    for key in ["extraction", "embedding"]:
        name = doc["models"][key]
        doc["models"][key + "_id"] = "ollama:" + lock[name]["manifest_digest"]
    write_private(root / "config.json", json.dumps(doc, indent=2) + "\n", replace=True)
    return doc
