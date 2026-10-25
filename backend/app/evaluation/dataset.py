"""评测集加载。

数据集放在仓库根 `eval/topics/`（数据与代码分离），这里只负责解析与校验。
校验是必要的：一个拼错的 doc_id、一个空的 must_cover，都会让指标悄悄失真。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..contracts.eval import TopicCase

REQUIRED_KEYS = {"id", "topic", "must_cover"}


class DatasetError(ValueError):
    pass


def load_case(path: str | Path) -> TopicCase:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise DatasetError(f"{path}: 顶层必须是 mapping")
    missing = REQUIRED_KEYS - set(raw)
    if missing:
        raise DatasetError(f"{path}: 缺少字段 {sorted(missing)}")

    must_cover = tuple(str(x).strip() for x in raw.get("must_cover") or [] if str(x).strip())
    if not must_cover:
        raise DatasetError(f"{path}: must_cover 不能为空（否则覆盖率恒为 0）")

    return TopicCase(
        id=str(raw["id"]),
        topic=str(raw["topic"]),
        sections=int(raw.get("sections", 5)),
        must_cover=must_cover,
        must_cite=tuple(str(x).strip() for x in raw.get("must_cite") or []),
        forbidden=tuple(str(x).strip() for x in raw.get("forbidden") or []),
        notes=str(raw.get("notes", "")),
    )


def load_dataset(directory: str | Path) -> list[TopicCase]:
    root = Path(directory)
    if not root.is_dir():
        raise DatasetError(f"评测集目录不存在: {root}")
    cases = [load_case(p) for p in sorted(root.glob("*.yaml"))]
    if not cases:
        raise DatasetError(f"{root} 下没有 .yaml 文件")
    ids = [c.id for c in cases]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise DatasetError(f"主题 id 重复: {sorted(dup)}")
    return cases
