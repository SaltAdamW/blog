"""只打包受审源码白名单，不读取运行配置、证据、草稿或模型日志。"""

import argparse
import gzip
import hashlib
import io
from pathlib import Path
import re
import tarfile


FILES = (
    "AGENTS.md", "README.md", "VERSION", "RELEASE_NOTES.md", "editorial.md", "review-policy.md",
    "config.example.json", "requirements-article.txt", "release/package.py",
    "research_weekly/__init__.py", "research_weekly/__main__.py", "research_weekly/article.py",
    "research_weekly/article_cli.py", "research_weekly/article_editor.py", "research_weekly/article_publish.py",
    "research_weekly/cli.py", "research_weekly/codex.py", "research_weekly/core.py",
    "research_weekly/discovery.py", "research_weekly/fetch.py", "research_weekly/pipeline.py",
    "research_weekly/publish.py", "research_weekly/questioning.py", "research_weekly/scope.py",
    "tests/smoke_article.py", "tests/smoke_questioning.py", "tests/test_article.py",
    "tests/test_automation.py", "tests/test_experience.py", "tests/test_questioning.py",
    "tests/test_weekly.py", "tests/test_release.py", "tests/test_publication_queue.py",
)


def build(root, destination):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    version = (root / "VERSION").read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("VERSION 必须为三段版本号")
    contents = {}
    for name in FILES:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise ValueError("发布白名单文件缺失或路径不安全：" + name)
        contents[name] = path.read_bytes()
    contents[".gitignore"] = b"var/\n.venv/\n__pycache__/\n*.pyc\n*.egg-info/\n.env\nconfig.json\nconfig.local.json\n"
    contents["MANIFEST.sha256"] = ("".join(hashlib.sha256(data).hexdigest() + "  " + name + "\n"
                                           for name, data in sorted(contents.items()))).encode()
    prefix = "agent-research-weekly-" + version
    destination.mkdir(parents=True, exist_ok=True)
    archive = destination / (prefix + ".tar.gz")
    if archive.exists():
        raise FileExistsError("不覆盖已有发布包：" + str(archive))
    with archive.open("xb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as tar:
            for name, data in sorted(contents.items()):
                info = tarfile.TarInfo(prefix + "/" + name)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                tar.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS").write_text(checksum + "  " + archive.name + "\n")
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(build(Path(__file__).resolve().parents[1], args.output))
