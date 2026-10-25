"""知识库的读写：初始化、上传、删除、列举。

放在 `rag` 层而不是接口层，是因为 CLI 和 HTTP 都要用同一套规则：
上传的扩展名白名单、文件名清洗、重名处理、以及"空库自动用示例语料兜底"。
分开实现会出现"网页能传 .docx、命令行不行"这种分叉。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..core.config import PROJECT_ROOT

#: 与检索层保持一致（app.rag.corpus.SUPPORTED_SUFFIXES）
SUPPORTED_SUFFIXES = {".md", ".markdown", ".txt"}
#: 单文件上限。语料是文本，2MB 已经远超单篇研报素材；超限多半是传错了文件。
MAX_FILE_BYTES = 2 * 1024 * 1024
#: 示例语料目录（仓库内，随代码分发）
SAMPLE_DIR = PROJECT_ROOT / "sample-corpus"

_UNSAFE = re.compile(r"[^\w\u4e00-\u9fff\-.]")


def sanitize_filename(name: str) -> str:
    """把上传的文件名清成安全的 doc_id。

    doc_id 会直接出现在正文角标 `[^doc_id]` 和接口路径里，
    所以路径分隔符、空格、控制字符都必须清掉，否则角标语法会被破坏。
    """
    stem = Path(name).name  # 只取文件名，丢掉客户端可能带来的目录
    cleaned = _UNSAFE.sub("_", stem).strip("._")
    return cleaned or "uploaded"


class CorpusStore:
    def __init__(self, docs_dir: str | Path) -> None:
        self.dir = Path(docs_dir)

    # ------------------------------------------------------------ 初始化

    def is_empty(self) -> bool:
        return not self._doc_files()

    def _doc_files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(
            p
            for p in self.dir.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        )

    def ensure(
        self, *, force: bool = False, source: Path | None = None,
        replace: bool = False,
    ) -> tuple[bool, int]:
        """知识库为空时，用自带语料初始化。

        返回 (是否复制了, 复制后文档数)。
        `force=True` 会覆盖同名文件——只在用户明确要求"重置示例库"时用。
        `source` 可指定语料来源，默认 `sample-corpus/`（虚构数据），
        传 `real-corpus/` 则用真实公开来源抓取的语料。

        `replace=True` 会**先清空**目标目录。
        切换语料来源必须用它：`force` 只覆盖同名文件，而两套语料的
        文件名不同，结果是把虚构和真实数据叠加进同一个库——
        那样生成的报告真假难辨，比用任何单一语料都糟。
        """
        if not force and not replace and not self.is_empty():
            return False, len(self._doc_files())
        src_dir = Path(source) if source else SAMPLE_DIR
        if not src_dir.is_dir():
            return False, 0

        if replace:
            for f in self._doc_files():
                f.unlink()

        self.dir.mkdir(parents=True, exist_ok=True)
        copied = 0
        for src in sorted(src_dir.rglob("*")):
            if not src.is_file() or src.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            # README 是给人看的说明，不是素材：进库只会污染检索结果
            if src.name.lower() == "readme.md":
                continue
            rel = src.relative_to(src_dir)
            dst = self.dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() and not force:
                continue
            shutil.copy2(src, dst)
            copied += 1
        return copied > 0, len(self._doc_files())

    # ------------------------------------------------------------ 列举

    def list_docs(self) -> list[dict]:
        """列出全部文档。title 取第一个一级标题，与检索层口径一致。"""
        out = []
        for path in self._doc_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            out.append(
                {
                    "doc_id": path.stem,
                    "title": _first_heading(text) or path.stem,
                    "chars": len(text),
                    "path": str(path.relative_to(self.dir)),
                }
            )
        return out

    def find(self, doc_id: str) -> Path | None:
        for path in self._doc_files():
            if path.stem == doc_id:
                return path
        return None

    # ------------------------------------------------------------ 写

    def save(self, filename: str, data: bytes) -> tuple[str, str]:
        """保存上传的文件。返回 (doc_id, 提示)。

        同名文件**不覆盖**，而是加后缀——覆盖会让正在引用的旧任务悄悄换新内容，
        而角标看起来没变，是最难排查的一类问题。
        """
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(
                f"文件超过 {MAX_FILE_BYTES // 1024 // 1024}MB 上限：{filename}"
            )
        raw_name = Path(filename).name
        suffix = Path(raw_name).suffix.lower()
        if suffix not in SUPPORTED_SUFFIXES:
            raise ValueError(f"只支持 {'/'.join(sorted(SUPPORTED_SUFFIXES))}：{filename}")
        # 先剥掉扩展名再清洗：否则会得到 "报告.md.md"，doc_id 变成 "报告.md"，
        # 而 doc_id 要直接写进正文角标，多一个点很难排查。
        stem = sanitize_filename(raw_name[: -len(suffix)] if suffix else raw_name)

        self.dir.mkdir(parents=True, exist_ok=True)
        target = self.dir / f"{stem}{suffix}"
        note = "已保存"
        if target.exists():
            idx = 2
            while (self.dir / f"{stem}_{idx}{suffix}").exists():
                idx += 1
            target = self.dir / f"{stem}_{idx}{suffix}"
            note = f"同名已存在，另存为 {target.name}"
        target.write_bytes(data)
        return target.stem, note

    def delete(self, doc_id: str) -> bool:
        path = self.find(doc_id)
        if path is None:
            return False
        path.unlink()
        return True


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped[:40]
    return None


__all__ = ["CorpusStore", "SAMPLE_DIR", "MAX_FILE_BYTES", "sanitize_filename"]
