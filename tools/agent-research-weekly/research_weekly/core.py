import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


class CommandError(RuntimeError):
    def __init__(self, message, stdout="", stderr=""):
        super().__init__(message)
        self.stdout, self.stderr = stdout, stderr


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def canonical_url(url):
    if not isinstance(url, str) or len(url) > 4096 or re_invalid_url(url):
        raise ValueError("URL 含控制字符或非法分隔符，或超过长度限制")
    p = urlsplit(url.strip())
    if p.scheme not in ("https", "http") or not p.hostname or p.username or p.password:
        raise ValueError("只允许不含凭据的公开 HTTP(S) URL")
    if p.port not in (None, 80, 443):
        raise ValueError("只允许标准 HTTP(S) 端口")
    secret = {"token", "access_token", "api_key", "key", "signature", "authorization"}
    pairs = parse_qsl(p.query, keep_blank_values=True)
    if any(k.lower() in secret or k.lower().startswith("x-amz-") for k, _ in pairs):
        raise ValueError("候选 URL 不得含令牌或签名；请使用原始永久链接")
    pairs = [(k, v) for k, v in pairs if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
    host = p.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    if p.port and p.port != (443 if p.scheme == "https" else 80):
        host += f":{p.port}"
    path = p.path or "/"
    if host in ("huggingface.co", "hf-mirror.com") and "/blob/" in path and path.lower().endswith(".pdf"):
        path = path.replace("/blob/", "/resolve/", 1)
    return urlunsplit((p.scheme, host, path, urlencode(sorted(pairs)), ""))


def re_invalid_url(value):
    return any(ord(c) < 32 or c in '<>"\\' for c in value)


def safe_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    config["_path"] = str(path)
    config["state_dir"] = str((path.parent / config["state_dir"]).resolve())
    ZoneInfo(config["timezone"])
    review = config.get("questioning_review", {})
    if not isinstance(review, dict) or set(review) - {"run_seconds", "max_calls"}:
        raise ValueError("questioning_review 只接受 run_seconds 和 max_calls，不支持关闭追问")
    for name, default, minimum, maximum in (("run_seconds", 3600, 60, 7200), ("max_calls", 96, 23, 96)):
        value = review.get(name, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"questioning_review.{name} 必须在 {minimum} 至 {maximum} 之间")
    discovery = config.get("discovery", {})
    if not 3 <= discovery.get("min_rounds", 3) <= discovery.get("max_rounds", 5) <= 10:
        raise ValueError("开放检索至少3轮，最多10轮，最小轮数不能大于最大轮数")
    experience_topics = discovery.get("experience_topics", [])
    if (not isinstance(experience_topics, list) or len(experience_topics) > 12
            or any(not isinstance(topic, str) or not topic.strip() for topic in experience_topics)):
        raise ValueError("discovery.experience_topics 必须为最多12条非空查询方向；空列表关闭历史经验检索")
    for section in ("fetch", "codex", "limits"):
        for key, value in config[section].items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value <= 0:
                raise ValueError(f"{section}.{key} 必须大于零")
    return config


def atomic_json(path, obj):
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


@contextlib.contextmanager
def run_lock(state):
    path = Path(state)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path / "run.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有任务运行中，不启动重叠任务") from None
        yield


def command(args, *, stdin=None, timeout=60, cwd=None, output_limit=3_000_000):
    """不经 shell；超时或输出超限时终止本次进程组，保留已收集输出。"""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as inp:
        inp.write((stdin or "").encode())
        inp.seek(0)
        proc = subprocess.Popen(args, stdin=inp, stdout=out, stderr=err, cwd=cwd, start_new_session=True)
        deadline = time.monotonic() + timeout
        failure = None
        try:
            while proc.poll() is None:
                if time.monotonic() > deadline:
                    failure = "命令超时"
                    break
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > output_limit:
                    failure = "命令输出超过预算"
                    break
                time.sleep(0.05)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
        out.seek(0)
        err.seek(0)
        stdout = out.read(output_limit).decode(errors="replace")
        stderr = err.read(output_limit).decode(errors="replace")
        if failure or proc.returncode:
            raise CommandError(f"{failure or '命令失败'} ({Path(args[0]).name}): {stderr[-1500:]}", stdout, stderr)
        if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > output_limit:
            raise CommandError("命令输出超过预算", stdout, stderr)
        return stdout, stderr


class Store:
    def __init__(self, directory):
        self.root = Path(directory)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
                published_at TEXT, discovered_at TEXT NOT NULL, origins TEXT NOT NULL,
                focus TEXT NOT NULL, alternatives TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                evidence_hash TEXT, fetched_at TEXT, retry_at TEXT, failures INTEGER NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '', review TEXT, reviewed_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, week TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT NOT NULL,
                finished_at TEXT, report TEXT, detail TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS reported (
                finding_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
                evidence_hash TEXT NOT NULL, title TEXT NOT NULL, conclusion TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY, finding_id TEXT NOT NULL, verdict TEXT NOT NULL,
                note TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def add(self, item):
        url = canonical_url(item["url"])
        identifier = digest(url)[:16]
        alternatives = [canonical_url(v) for v in item.get("alternatives", [])]
        published = item.get("published_at")
        if published:
            published = dt.date.fromisoformat(published[:10]).isoformat()
        old = self.get(identifier)
        origins = sorted(set(([item.get("origin", "manual")]) + (json.loads(old["origins"]) if old else [])))
        with self.db:
            if old:
                self.db.execute("UPDATE candidates SET origins=?, alternatives=?, focus=? WHERE id=?", (
                    json.dumps(origins), json.dumps(sorted(set(alternatives + json.loads(old["alternatives"])))),
                    item.get("focus") or old["focus"], identifier))
            else:
                self.db.execute("INSERT INTO candidates(id,url,title,published_at,discovered_at,origins,focus,alternatives) VALUES(?,?,?,?,?,?,?,?)", (
                    identifier, url, item.get("title") or url, published, utcnow(), json.dumps(origins),
                    item.get("focus", ""), json.dumps(alternatives)))
        return identifier

    def get(self, identifier):
        row = self.db.execute("SELECT * FROM candidates WHERE id=?", (identifier,)).fetchone()
        return dict(row) if row else None

    def update(self, identifier, **fields):
        allowed = {"status", "evidence_hash", "fetched_at", "retry_at", "failures", "reason", "review", "reviewed_hash"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("非法状态字段")
        with self.db:
            self.db.execute(f"UPDATE candidates SET {','.join(k+'=?' for k in fields)} WHERE id=?", (*fields.values(), identifier))

    def list(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM candidates ORDER BY discovered_at,id")]

    def history(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM reported ORDER BY rowid DESC LIMIT 100")]

    def feedback(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM feedback ORDER BY id DESC LIMIT 30")]
