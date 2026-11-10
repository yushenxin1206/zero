"""索引写的并发安全：进程内不丢更新，跨进程也不丢更新。"""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from zero.paths import current
from tests.support import make_store

CHANNEL = "cli"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _index_path() -> Path:
    return current().sessions / CHANNEL / "index.json"


def _read_index() -> dict:
    return json.loads(_index_path().read_text(encoding="utf-8"))


def test_threads_create_without_lost_updates():
    count = 50
    barrier = threading.Barrier(2)

    def worker(tag: str) -> None:
        barrier.wait()
        for i in range(count):
            make_store().create_session(CHANNEL, f"{tag}-{i}")

    threads = [threading.Thread(target=worker, args=(tag,))
               for tag in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = {f"{tag}-{i}" for tag in ("a", "b") for i in range(count)}
    assert set(_read_index()) == expected


def test_threads_update_without_lost_updates():
    count = 30
    session_ids = {}
    for i in range(count * 2):
        source = f"src-{i}"
        session_ids[source] = make_store().create_session(CHANNEL, source)

    barrier = threading.Barrier(2)

    def worker(tag: str, offset: int) -> None:
        barrier.wait()
        for i in range(offset, offset + count):
            make_store().set_title(
                CHANNEL, session_ids[f"src-{i}"], f"title-{tag}-{i}")

    threads = [
        threading.Thread(target=worker, args=("a", 0)),
        threading.Thread(target=worker, args=("b", count)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    titles = {source_id: e["title"]
              for source_id, e in _read_index().items()}
    for i in range(count):
        assert titles[f"src-{i}"] == f"title-a-{i}"
    for i in range(count, count * 2):
        assert titles[f"src-{i}"] == f"title-b-{i}"


_CHILD = """
import sys
import time
from pathlib import Path

from zero.session.store import SessionStore

root, tag, count, start_at = (
    sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]))
store = SessionStore(Path(root) / "sessions")
delay = start_at - time.time()
if delay > 0:
    time.sleep(delay)
for i in range(count):
    store.create_session("cli", f"{tag}-{i}")
"""


def test_processes_create_without_lost_updates():
    root = str(current().root)
    count = 40
    start_at = time.time() + 1.5
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, root, tag, str(count),
             str(start_at)],
            cwd=str(REPO_ROOT),
        )
        for tag in ("p1", "p2")
    ]
    for p in procs:
        assert p.wait(timeout=60) == 0

    expected = {f"{tag}-{i}" for tag in ("p1", "p2") for i in range(count)}
    # 文件仍是合法 JSON（能 json.loads 不抛），且没有丢更新
    assert set(_read_index()) == expected


def test_write_ignores_foreign_fixed_tmp_file():
    make_store().create_session(CHANNEL, "seed")
    fixed_tmp = _index_path().parent / "index.json.tmp"
    fixed_tmp.write_text("garbage", encoding="utf-8")

    make_store().create_session(CHANNEL, "after-collision")

    assert "after-collision" in _read_index()
    # 固定名的旧临时文件不被碰，唯一临时文件用完即删
    assert fixed_tmp.read_text(encoding="utf-8") == "garbage"
    assert list(_index_path().parent.glob(".index-*")) == []
