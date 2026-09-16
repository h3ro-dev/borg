"""Bound active work by host capacity, and serialize only shared resources."""
from __future__ import annotations

import asyncio
import os
import stat
import uuid
from pathlib import Path
from contextlib import AsyncExitStack, asynccontextmanager

from fastmcp.exceptions import ToolError


def _inode(info) -> str:
    return f"inode:{info.st_dev}:{info.st_ino}"


def _file_keys(path: str, namespace: bool = False) -> list[tuple[str, bool]]:
    """Shared ancestor locks plus stable directory-entry and inode identities.

    Directory-entry names are read from the filesystem, so case aliases agree
    without conflating distinct names on a case-sensitive volume. Missing paths
    take the nearest existing directory exclusively until creation completes.
    """
    path = os.path.abspath(path)
    keys = []
    for spelling in {path, os.path.realpath(path)}:
        target = Path(spelling)
        parent = target.parent
        missing = not os.path.lexists(target)
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        parent_info = parent.stat()
        keys.append((_inode(parent_info), missing))
        if not missing:
            info = target.lstat()
            actual_name = target.name
            # Only names with the same folded spelling and inode can alias this
            # entry. Distinct hard links elsewhere coordinate via the inode key.
            with os.scandir(target.parent) as entries:
                aliases = [entry.name for entry in entries
                           if entry.name.casefold() == target.name.casefold()
                           and entry.inode() == info.st_ino]
            if aliases:
                actual_name = min(aliases)
            keys.append((f"entry:{parent_info.st_dev}:{parent_info.st_ino}:{actual_name}", True))
            keys.append((_inode(info), namespace or not stat.S_ISDIR(info.st_mode)))
        for ancestor in parent.parents:
            keys.append((_inode(ancestor.stat()), False))
    return keys


def resource_keys(name: str, arguments: dict) -> tuple[tuple[str, bool], ...]:
    """Coordinate connector resources; shell side effects require work claims."""
    keys = []
    if name.startswith("computer_"):
        if "pid" in arguments:
            keys.append(("process:" + str(int(arguments["pid"])), True))
        if "search_id" in arguments:
            keys.append(("search:" + str(arguments["search_id"]), True))
        paths = [arguments[k] for k in ("path", "file_path", "source", "destination")
                 if isinstance(arguments.get(k), str)]
        if name == "computer_read_multiple_files":
            paths.extend(arguments.get("paths", [])[:50])
        if name == "computer_move_file" and os.path.isdir(arguments.get("destination", "")):
            paths.append(os.path.join(arguments["destination"], os.path.basename(arguments.get("source", ""))))
        for path in paths:
            keys.extend(_file_keys(path, namespace=name in {
                "computer_move_file", "computer_write_file", "computer_write_pdf",
                "computer_edit_block", "computer_create_directory"}))
    elif name.startswith("browser_"):
        if "session_id" in arguments:
            keys.append(("browser:" + str(uuid.UUID(str(arguments["session_id"]))), True))
        elif name == "browser_start_session":
            keys.append(("browser-launch", True))
    elif name.startswith(("job_", "remote_")):
        if "job_id" in arguments:
            domain = "remote-job:" if name.startswith("remote_") else "job:"
            keys.append((domain + str(uuid.UUID(str(arguments["job_id"]))), True))
        elif name in {"job_start", "remote_start", "job_list"}:
            keys.append((name, True))
    elif name.startswith(("ui_", "desktop_")):
        keys.append(("physical-desktop", True))
    elif name.startswith("credential_"):
        keys.append(("credential-handoff", True))
    merged = {}
    for key, exclusive in keys:
        merged[key] = merged.get(key, False) or exclusive
    return tuple(sorted(merged.items()))


class ResourceLock:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.readers = 0
        self.writer = False
        self.waiting_writers = 0

    @asynccontextmanager
    async def hold(self, exclusive):
        async with self.condition:
            if exclusive:
                self.waiting_writers += 1
                try:
                    await self.condition.wait_for(lambda: not self.writer and not self.readers)
                    self.writer = True
                finally:
                    self.waiting_writers -= 1
                    self.condition.notify_all()
            else:
                await self.condition.wait_for(lambda: not self.writer and not self.waiting_writers)
                self.readers += 1
        try:
            yield
        finally:
            async with self.condition:
                if exclusive:
                    self.writer = False
                else:
                    self.readers -= 1
                self.condition.notify_all()


class CallScheduler:
    def __init__(self, config: dict | None = None):
        config = config or {}
        self.max_in_flight = int(config.get("max_in_flight", 64))
        self.queue_limit = int(config.get("queue_limit", 256))
        self.wait_seconds = float(config.get("wait_seconds", 30))
        if self.max_in_flight < 1 or self.queue_limit < 0 or not 0 < self.wait_seconds < float("inf"):
            raise ValueError("BORG concurrency requires positive capacity/deadline and a nonnegative queue")
        self._slots = asyncio.Semaphore(self.max_in_flight)
        self._locks: dict[str, tuple[ResourceLock, int]] = {}
        self._tasks: set[asyncio.Task] = set()
        self.active = 0
        self.pending = 0
        self.rejected = 0

    def status(self) -> dict:
        return {"mode": "resource_scoped", "max_in_flight": self.max_in_flight,
                "queue_limit": self.queue_limit, "wait_seconds": self.wait_seconds,
                "active": self.active, "waiting": self.pending - self.active,
                "rejected": self.rejected, "locked_resources": len(self._locks),
                "connection_limit": None}

    @asynccontextmanager
    async def _resource(self, key, exclusive):
        lock, users = self._locks.get(key, (ResourceLock(), 0))
        self._locks[key] = (lock, users + 1)
        try:
            async with lock.hold(exclusive):
                yield
        finally:
            lock, users = self._locks[key]
            if users == 1:
                del self._locks[key]
            else:
                self._locks[key] = (lock, users - 1)

    @asynccontextmanager
    async def slot(self, keys=()):
        if self.pending >= self.max_in_flight + self.queue_limit:
            self.rejected += 1
            raise ToolError("BORG_BUSY: host work queue is full; operation was not started")
        self.pending += 1
        try:
            async with AsyncExitStack() as stack:
                try:
                    async with asyncio.timeout(self.wait_seconds):
                        for key, exclusive in sorted(set((key, True) if isinstance(key, str) else key for key in keys)):
                            await stack.enter_async_context(self._resource(key, exclusive))
                        await stack.enter_async_context(self._slots)
                except TimeoutError:
                    self.rejected += 1
                    raise ToolError("BORG_BUSY: resource wait deadline exceeded; operation was not started") from None
                self.active += 1
                try:
                    yield
                finally:
                    self.active -= 1
        finally:
            self.pending -= 1

    async def run(self, name, arguments, call):
        started = False
        deadline = asyncio.get_running_loop().time() + self.wait_seconds

        async def execute():
            nonlocal started
            while True:
                keys = await asyncio.to_thread(resource_keys, name, arguments)
                async with self.slot(keys):
                    # A queued symlink rewrite, directory move or atomic replace
                    # may change identity. Never execute with a stale footprint.
                    current = await asyncio.to_thread(resource_keys, name, arguments)
                    if current == keys:
                        started = True
                        return await call()
                if asyncio.get_running_loop().time() >= deadline:
                    self.rejected += 1
                    raise ToolError("BORG_BUSY: file identity changed while waiting; operation was not started")

        task = asyncio.create_task(execute())
        self._tasks.add(task)

        def finished(done):
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Retrieve failures even after the client leaves.

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Cancelling an await does not stop a native worker thread. Keep its
            # resource locks until execution ends; discard work still queued.
            if not started:
                task.cancel()
            raise
