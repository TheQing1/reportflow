"""生成演示知识库（22 篇 markdown）。

内容本身不进版本库（data/docs/ 已在 .gitignore 里），
用这个脚本生成，保证任何人 clone 下来都能得到字节级一致的检索结果。

数据来自 `scripts/corpus_data.py`（单一事实源）：
同一指标在全库只有一个数，评测集的 must_cover 也从同一张表取，
避免"标注要求 8.4GWh、语料写 8.6GWh"这种自相矛盾的评测。

⚠️ 内容为演示构造，不是真实研报数据 —— 每篇开头都标注了这一点，
避免这些数字被当成事实引用出去。
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from .corpus_data import DOCS, FACTS, render_document

DISCLAIMER = "> 本文件为 ReportFlow 演示用构造文本，数值非真实统计，仅用于验证检索与引用链路。\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成演示语料")
    parser.add_argument("--out", default=None, help="输出目录，默认 <repo>/data/docs")
    parser.add_argument("--clean", action="store_true", help="先清空目标目录")
    args = parser.parse_args()

    root = Path(args.out) if args.out else Path(__file__).resolve().parents[2] / "data" / "docs"
    if args.clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)

    total_chars = 0
    for spec in DOCS:
        body = render_document(spec)
        # 标题后插入免责声明，正文仍保持可检索
        lines = body.split("\n", 1)
        body = lines[0] + "\n\n" + DISCLAIMER + lines[1] if len(lines) > 1 else body
        path = root / f"{spec.doc_id}.md"
        path.write_text(body, encoding="utf-8")
        total_chars += len(body)

    print(f"已生成 {len(DOCS)} 篇文档 -> {root}")
    print(f"事实条目 {len(FACTS)} 条 | 总字符 {total_chars}")


if __name__ == "__main__":
    main()
