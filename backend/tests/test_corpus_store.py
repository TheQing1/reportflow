"""知识库读写的测试。

上传是唯一让用户"自己喂数据"的入口，它出错的方式都很隐蔽：
文件名带路径 → 写到别处；扩展名没校验 → 塞进二进制；
同名直接覆盖 → 旧任务悄悄换了内容而角标看不出变化。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.rag.store import CorpusStore, SAMPLE_DIR, sanitize_filename


class SanitizeTest(unittest.TestCase):
    def test_strips_directory_traversal(self) -> None:
        # Path().name 先丢掉目录部分，剩下的分隔符再被清成下划线
        self.assertEqual(sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(sanitize_filename("a/b.md"), "b.md")
        self.assertNotIn("/", sanitize_filename("../../etc/x.md"))

    def test_keeps_chinese_and_separators(self) -> None:
        self.assertEqual(sanitize_filename("客户 访谈-2026.md"), "客户_访谈-2026.md")

    def test_falls_back_when_nothing_left(self) -> None:
        self.assertEqual(sanitize_filename("///"), "uploaded")


class CorpusStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="rfstore-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = CorpusStore(self.tmp)

    def test_save_strips_extension_from_doc_id(self) -> None:
        doc_id, note = self.store.save("报告.md", "# 报告\n\n内容".encode("utf-8"))
        self.assertEqual(doc_id, "报告")
        self.assertEqual(note, "已保存")

    def test_same_name_is_renamed_not_overwritten(self) -> None:
        first, _ = self.store.save("笔记.md", "# 第一版\n\n旧内容".encode("utf-8"))
        second, note = self.store.save("笔记.md", "# 第二版\n\n新内容".encode("utf-8"))
        self.assertNotEqual(first, second)
        self.assertIn("另存为", note)
        # 旧内容必须还在：覆盖会让已有任务的引用悄悄换掉
        self.assertIn("旧内容", (self.tmp / f"{first}.md").read_text(encoding="utf-8"))

    def test_rejects_unsupported_extension(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("报价.docx", b"\x00binary")

    def test_rejects_oversized_file(self) -> None:
        with self.assertRaises(ValueError):
            self.store.save("大文件.md", b"x" * (2 * 1024 * 1024 + 1))

    def test_delete_and_list(self) -> None:
        doc_id, _ = self.store.save("临时.md", "# 临时\n\n待删除".encode("utf-8"))
        self.assertIn(doc_id, [d["doc_id"] for d in self.store.list_docs()])
        self.assertTrue(self.store.delete(doc_id))
        self.assertFalse(self.store.delete(doc_id), "重复删除应返回 False")
        self.assertNotIn(doc_id, [d["doc_id"] for d in self.store.list_docs()])

    @unittest.skipUnless(SAMPLE_DIR.is_dir(), "示例语料目录不存在")
    def test_ensure_fills_empty_corpus_from_sample(self) -> None:
        self.assertTrue(self.store.is_empty())
        copied, total = self.store.ensure()
        self.assertTrue(copied)
        self.assertGreaterEqual(total, 10)
        # README 是说明不是素材，不该进库污染检索
        self.assertNotIn("README", [d["doc_id"] for d in self.store.list_docs()])

    @unittest.skipUnless(SAMPLE_DIR.is_dir(), "示例语料目录不存在")
    def test_ensure_does_not_overwrite_existing(self) -> None:
        self.store.save("自建.md", "# 自建\n\n我自己的资料".encode("utf-8"))
        self.store.ensure()
        self.assertIn("自建", [d["doc_id"] for d in self.store.list_docs()])
        self.assertEqual(
            (self.tmp / "自建.md").read_text(encoding="utf-8").strip().splitlines()[-1],
            "我自己的资料",
        )


if __name__ == "__main__":
    unittest.main()
