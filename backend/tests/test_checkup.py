"""发布前体检的纯函数部分。

体检的价值全在"指出该数据实际出自哪篇"——所以这条逻辑必须单独锁死，
它错一次就会把人引到另一篇错误文档上，比不说更糟。
"""

from __future__ import annotations

import unittest

from app.checkup import CheckReport, Issue, locate_citation, render_check

CORPUS = {
    "doc_04_scale_cig": "2025年工商业储能新增装机约8.4GWh，同比增长62%。",
    "doc_07_region_cig": "浙江一省占比约22%，广东约19%。峰谷价差约0.95元/kWh。",
    "doc_11_tech_lfp": "磷酸铁锂电池在新型储能装机中的占比达到96.5%。",
}


def _full_text(doc_id: str) -> str:
    return CORPUS.get(doc_id, "")


class LocateCitationTest(unittest.TestCase):
    def test_finds_doc_containing_the_number(self) -> None:
        got = locate_citation(
            "2025年工商业储能新增装机约8.4GWh，同比增长62%",
            doc_ids=list(CORPUS),
            full_text=_full_text,
        )
        self.assertEqual(got[0], "doc_04_scale_cig")

    def test_suggests_correct_doc_when_citation_is_wrong(self) -> None:
        # 角标挂了 doc_07，但 96.5% 只在 doc_11 里 —— 这正是单 Agent 的典型错误
        got = locate_citation(
            "磷酸铁锂在新型储能装机中的占比达到96.5%",
            doc_ids=list(CORPUS),
            full_text=_full_text,
        )
        self.assertEqual(got[0], "doc_11_tech_lfp")

    def test_returns_empty_when_no_number(self) -> None:
        got = locate_citation(
            "收益来源单一带来安全配置成本上升", doc_ids=list(CORPUS), full_text=_full_text
        )
        self.assertEqual(got, [])

    def test_returns_empty_when_number_not_in_any_doc(self) -> None:
        got = locate_citation(
            "新增装机约9.1GWh", doc_ids=list(CORPUS), full_text=_full_text
        )
        self.assertEqual(got, [])

    def test_ignores_whitespace_difference(self) -> None:
        got = locate_citation(
            "新增装机约 8.4 GWh", doc_ids=list(CORPUS), full_text=_full_text
        )
        self.assertEqual(got[0], "doc_04_scale_cig")


class RenderCheckTest(unittest.TestCase):
    def test_clean_report_says_so(self) -> None:
        lines = render_check(CheckReport(total=6, supported=6))
        self.assertTrue(any("没有发现引用对不上" in line for line in lines))

    def test_issue_shows_wrong_citation_and_suggestion(self) -> None:
        report = CheckReport(
            total=6,
            supported=5,
            unsupported=1,
            issues=[
                Issue(
                    chapter=2,
                    sentence="磷酸铁锂占比达到96.5%",
                    cited="doc_12_tech_na",
                    verdict="unsupported",
                    reason="原文讲钠电，未提磷酸铁锂占比",
                    likely_docs=["doc_11_tech_lfp"],
                )
            ],
        )
        text = "\n".join(render_check(report))
        self.assertIn("第 2 章", text)
        self.assertIn("doc_12_tech_na", text)
        self.assertIn("其他含这些数字的文档：doc_11_tech_lfp", text)

    def test_error_report_is_not_reported_as_clean(self) -> None:
        report = CheckReport(error="正文里没有找到引用")
        self.assertFalse(report.clean)
        self.assertTrue(any("未完成" in line for line in render_check(report)))


if __name__ == "__main__":
    unittest.main()
