import json
import os
import time
from contextlib import contextmanager


def _replace_with_retry(tmp_path, path, attempts=30, delay=0.1):
    """os.replace 在 Windows 下，目标被别的句柄(如 Flask 读计数)打开时会 WinError5；短暂重试即可。"""
    for i in range(attempts):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


@contextmanager
def exclusive_file_lock(target_path, poll_interval=0.05):
    lock_path = f"{target_path}.lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    with open(lock_path, "a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(poll_interval)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path, text, encoding="utf-8"):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def atomic_write_json(path, payload, indent=2, ensure_ascii=False):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent, ensure_ascii=ensure_ascii)
            f.flush()
            os.fsync(f.fileno())
        _replace_with_retry(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def append_line_locked(path, line, encoding="utf-8"):
    with exclusive_file_lock(path):
        with open(path, "a", encoding=encoding) as f:
            f.write(line.rstrip("\n") + "\n")
            f.flush()
            os.fsync(f.fileno())
