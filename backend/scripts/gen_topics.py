"""生成评测集 YAML（20 主题）。

为什么用脚本生成而不是手写：

1. must_cover 从 `corpus_data.FACTS` 直接取，**标注与语料不可能不一致**；
2. 20 个主题手抄 90 多条要点，出错概率远高于写一个生成器；
3. forbidden 用「数字近似错误」而不是「方向性错误」——方向性错误太容易识破
   （装机下降 vs 增长 62%），近似错误才真正考验模型有没有核对原文。

用法：
    python -m scripts.gen_topics            # 覆盖写入 eval/topics/
    python -m scripts.gen_topics --dry-run  # 只打印
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .corpus_data import FACTS

#: (id, topic, sections, must_cover 的 fact key, must_cite, forbidden, notes)
TOPICS: tuple[tuple[str, str, int, tuple[str, ...], tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        "cig-region",
        "2026 年中国工商业储能行业",
        5,
        ("f_region_cig", "f_region_cig_split", "f_region_price", "f_scale_cig", "f_cost_irr"),
        ("doc_04_scale_cig", "doc_07_region_cig", "doc_10_cost"),
        (
            "2025年工商业储能新增装机约9.1GWh",
            "浙江一般工业电价峰谷价差约0.75元/kWh",
            "三省合计占工商业储能新增装机的35%",
        ),
        "区域集中与价差驱动，最能体现数据可溯源",
    ),
    (
        "cig-scale",
        "工商业储能的装机规模与增长节奏",
        4,
        ("f_scale_cig", "f_scale_cig_share", "f_out_cig", "f_scale_total"),
        ("doc_04_scale_cig", "doc_03_scale_overall", "doc_21_outlook"),
        ("2025年工商业储能新增装机约6.8GWh", "工商业储能占新型储能新增装机的比重约为37%"),
        "规模口径题，考数字准确性",
    ),
    (
        "cig-business",
        "工商业储能的商业模式与收益结构",
        4,
        ("f_biz_arb", "f_biz_emc", "f_scale_cig", "f_cost_epc"),
        ("doc_16_business_model", "doc_10_cost", "doc_04_scale_cig"),
        ("峰谷套利占工商业储能项目收益的50%左右", "合同能源管理模式渗透率不足20%"),
        "收益单一性判断",
    ),
    (
        "grid-storage",
        "电网侧储能的发展与区域布局",
        4,
        ("f_scale_grid", "f_region_nw", "f_biz_shared", "f_policy_ratio"),
        ("doc_03_scale_overall", "doc_06_scale_grid", "doc_08_region_grid"),
        ("电网侧储能占新型储能新增装机不足20%", "内蒙古、新疆、甘肃合计占电网侧新增装机的28%"),
        "与工商业形成对照，分布逻辑跟着新能源基地走",
    ),
    (
        "overseas",
        "储能海外市场：美国、欧洲与中东",
        4,
        ("f_ovs_us", "f_ovs_eu", "f_ovs_me", "f_ovs_export"),
        ("doc_18_overseas_us", "doc_19_overseas_eu_me"),
        ("2025年美国储能新增装机约12GW", "2025年欧洲户用储能新增装机同比增长15%"),
        "三个区域方向不一，考「不混淆」",
    ),
    (
        "export-chain",
        "储能产业链出口与海外产能布局",
        4,
        ("f_ovs_export", "f_ovs_us", "f_ovs_me", "f_risk_overcap"),
        ("doc_18_overseas_us", "doc_19_overseas_eu_me", "doc_20_risk"),
        ("2025年中国企业储能电池出口量约120GWh", "美国储能装机增长主要由户用项目驱动"),
        "出口与产能过剩的对冲关系",
    ),
    (
        "technology",
        "储能技术路线：锂电、钠电与液流",
        4,
        ("f_tech_lfp", "f_tech_na", "f_tech_flow", "f_out_na"),
        ("doc_11_tech_lfp", "doc_12_tech_na", "doc_13_tech_flow"),
        ("钠离子电池已在储能领域实现规模化商用", "磷酸铁锂在新型储能中的占比已降至86.5%"),
        "三条路线成熟度差异极大，考「不要把示范写成商用」",
    ),
    (
        "na-battery",
        "钠离子电池的产业化进展与前景",
        3,
        ("f_tech_na", "f_out_na", "f_tech_lfp"),
        ("doc_12_tech_na", "doc_11_tech_lfp"),
        ("2025年钠离子电池储能装机约420MWh", "钠离子电池单位成本已低于磷酸铁锂"),
        "示范阶段 vs 商用阶段的口径",
    ),
    (
        "long-duration",
        "长时储能需求与液流电池",
        3,
        ("f_tech_flow", "f_cost_system", "f_out_total"),
        ("doc_13_tech_flow", "doc_10_cost", "doc_21_outlook"),
        ("全钒液流电池2025年新增装机约150MWh", "液流电池单位造价约为磷酸铁锂的0.8倍"),
        "长时场景的经济性判断",
    ),
    (
        "pcs-equipment",
        "储能变流器与关键设备环节",
        3,
        ("f_tech_pcs", "f_tech_lfp", "f_comp_cr5"),
        ("doc_14_tech_pcs", "doc_11_tech_lfp", "doc_09_competition"),
        ("2025年国内储能PCS出货约25GW", "1500V机型占比降至38%"),
        "设备环节的集中度与技术迭代",
    ),
    (
        "competition",
        "储能系统集成的竞争格局与盈利压力",
        4,
        ("f_comp_cr5", "f_comp_small", "f_cost_system", "f_risk_margin"),
        ("doc_09_competition", "doc_10_cost", "doc_20_risk"),
        ("储能系统集成环节CR5已超过58%", "2025年储能系统均价同比上涨"),
        "集中度低 + 价格战 + 毛利率下滑，三个判断互相印证",
    ),
    (
        "profitability",
        "储能行业的产能过剩与盈利压力",
        4,
        ("f_risk_overcap", "f_risk_margin", "f_cost_system", "f_risk_price"),
        ("doc_20_risk", "doc_10_cost"),
        ("2025年储能电芯名义产能利用率约75%", "集成商平均毛利率升至28.6%"),
        "产能与盈利的连锁反应",
    ),
    (
        "price-cost",
        "储能系统价格下行与成本结构",
        4,
        ("f_cost_system", "f_cost_cell", "f_cost_epc", "f_cost_irr"),
        ("doc_10_cost", "doc_07_region_cig"),
        ("2025年储能系统均价降至0.68元/Wh", "磷酸铁锂储能电芯均价降至0.42元/Wh"),
        "价格链条：电芯 → 系统 → EPC → 项目 IRR",
    ),
    (
        "safety",
        "储能电站安全与标准体系",
        3,
        ("f_safety_acc", "f_safety_std", "f_risk_price"),
        ("doc_15_safety", "doc_20_risk"),
        ("2025年国内公开报道的储能电站火灾事故共3起", "新版安全规程不再要求消防系统独立配置"),
        "事故数据 + 标准收紧 + 低价对安全的挤压",
    ),
    (
        "policy-target",
        "储能国家规划目标与配储要求",
        4,
        ("f_policy_target", "f_policy_ratio", "f_policy_subsidy", "f_scale_total"),
        ("doc_01_policy_national", "doc_02_policy_local", "doc_03_scale_overall"),
        ("到2027年全国新型储能装机规模达到2.8亿千瓦以上", "配储比例要求普遍为装机容量的10%"),
        "规划目标 vs 已实现规模，容易混淆",
    ),
    (
        "capacity-mechanism",
        "储能容量补偿与租赁机制",
        3,
        ("f_policy_cap", "f_biz_lease", "f_biz_shared"),
        ("doc_01_policy_national", "doc_16_business_model", "doc_08_region_grid"),
        ("2025年储能容量租赁均价约160元/千瓦·年", "共有13个省级行政区出台容量补偿政策"),
        "机制类主题，考「政策数量与价格」的准确复述",
    ),
    (
        "market-mechanism",
        "电力市场与储能收益机制",
        4,
        ("f_mkt_spot", "f_mkt_aux", "f_biz_arb", "f_policy_cap"),
        ("doc_17_market", "doc_16_business_model", "doc_01_policy_national"),
        ("2025年已有24个省份开展电力现货市场长周期结算试运行", "山西调频补偿标准约为3元/千瓦"),
        "现货 + 辅助服务 + 容量，三条收益路径",
    ),
    (
        "investment",
        "储能产业链投融资",
        3,
        ("f_fin_ipo", "f_fin_fund", "f_risk_overcap", "f_risk_margin"),
        ("doc_22_finance", "doc_20_risk"),
        ("2025年储能产业链共有21家企业完成IPO", "2025年新设储能产业基金规模不足100亿元"),
        "一级市场热度与产业资本方向",
    ),
    (
        "policy-outlook",
        "储能政策机制与 2026 年展望",
        4,
        ("f_policy_target", "f_policy_cap", "f_out_total", "f_mkt_spot"),
        ("doc_01_policy_national", "doc_17_market", "doc_21_outlook"),
        ("截至2025年底全国新型储能装机已达到1.8亿千瓦", "预计2026年新型储能装机增速将超过100%"),
        "规划目标不等于已实现，是典型混淆点",
    ),
    (
        "industry-overview",
        "中国储能行业全景：规模、格局与展望",
        5,
        ("f_scale_total", "f_scale_new", "f_comp_cr5", "f_cost_system", "f_out_total"),
        ("doc_03_scale_overall", "doc_09_competition", "doc_10_cost", "doc_21_outlook"),
        ("2025年新型储能新增装机2800万千瓦", "预计2026年新型储能新增装机8000万千瓦"),
        "综合题，5 章需要跨 4 篇以上文档",
    ),
    # ------------------------------------------------------------------
    # 第二批（21~40）：刻意换角度，避免成为前 20 个的复述
    # 挑选原则：优先用前 20 个主题覆盖不足的事实，并把章节数打散（3/4/5）
    # ------------------------------------------------------------------
    (
        "resi-storage",
        "户用储能市场：增长失速与海外依赖",
        3,
        ("f_scale_resi", "f_ovs_eu", "f_scale_cig"),
        ("doc_05_scale_resi", "doc_19_overseas_eu_me", "doc_04_scale_cig"),
        ("2025年户用储能新增装机约3.9GWh", "2025年欧洲户用储能新增装机同比下降25%"),
        "户用是本知识库里唯一负增长的细分，考「不要把下降读成增长」",
    ),
    (
        "top-manufacturers",
        "头部系统集成商的份额与集中度",
        3,
        ("f_comp_top3", "f_comp_cr5", "f_comp_small"),
        ("doc_09_competition",),
        ("储能系统出货量前三合计份额约36%", "2025年储能系统集成环节CR5约为48%"),
        "集中度数字最容易被近似写错，且三处数字互相咬合",
    ),
    (
        "aux-service",
        "调频辅助服务与储能收益补充",
        3,
        ("f_mkt_aux", "f_mkt_spot", "f_biz_arb"),
        ("doc_17_market", "doc_16_business_model"),
        ("山西调频辅助服务补偿标准约12元/千瓦", "2025年已有24个省份开展电力现货长周期结算试运行"),
        "辅助服务是收益的补充项，不能写成为主项",
    ),
    (
        "project-finance",
        "储能产业链的投融资热度",
        4,
        ("f_fin_ipo", "f_fin_fund", "f_risk_margin", "f_out_total"),
        ("doc_22_finance", "doc_20_risk", "doc_21_outlook"),
        ("2025年储能产业链共有21家企业完成IPO", "2025年新设储能相关产业基金规模合计约300亿元"),
        "融资热与盈利能力下滑同时存在，考「并存而非矛盾」",
    ),
    (
        "subsidy-policy",
        "地方补贴、容量补偿与租赁价格",
        4,
        ("f_policy_subsidy", "f_policy_cap", "f_biz_lease"),
        ("doc_02_policy_local", "doc_16_business_model", "doc_01_policy_national"),
        ("2025年工商业储能补贴政策覆盖22个省份", "2025年储能容量租赁均价约150元/千瓦·年"),
        "三种价格工具混在一起，最容易张冠李戴",
    ),
    (
        "cell-cost",
        "电芯成本下行与系统价格传导",
        3,
        ("f_cost_cell", "f_cost_system", "f_risk_overcap"),
        ("doc_10_cost", "doc_20_risk", "doc_11_tech_lfp"),
        ("磷酸铁锂储能电芯均价降至0.38元/Wh", "电芯占系统成本的比重约70%"),
        "成本结构题，考「电芯占比约一半」这个常被夸大的数字",
    ),
    (
        "northwest-grid",
        "西北地区电网侧储能的集群化布局",
        3,
        ("f_region_nw", "f_scale_grid", "f_biz_shared"),
        ("doc_08_region_grid", "doc_06_scale_grid", "doc_16_business_model"),
        ("内蒙古、新疆、甘肃合计占电网侧新增装机的58%", "2025年电网侧储能新增装机1200万千瓦"),
        "区域集中度与新能源基地的因果关系",
    ),
    (
        "shared-storage",
        "共享储能模式的落地与收益",
        3,
        ("f_biz_shared", "f_biz_lease", "f_policy_cap"),
        ("doc_16_business_model", "doc_02_policy_local", "doc_17_market", "doc_08_region_grid", "doc_01_policy_national"),
        ("共享储能模式在青海、湖南等省份落地项目超过14个", "容量租赁价格同比上涨22%"),
        "模式类题目，最容易写成泛泛而谈而没有数字",
    ),
    (
        "lithium-dominance",
        "磷酸铁锂路线的主导地位与边界",
        4,
        ("f_tech_lfp", "f_cost_cell", "f_safety_std"),
        ("doc_11_tech_lfp", "doc_10_cost", "doc_15_safety"),
        ("磷酸铁锂在新型储能装机中的占比达到86.5%", "磷酸铁锂在新型储能中的占比已降至76.5%"),
        "占比数字是高频误写点，两次给不同错值",
    ),
    (
        "fire-safety",
        "储能电站事故特征与安全规程",
        4,
        ("f_safety_acc", "f_safety_std", "f_tech_lfp"),
        ("doc_15_safety", "doc_11_tech_lfp"),
        ("2025年国内公开报道的储能电站火灾事故共17起", "事故集中发生在投运5年以上的项目"),
        "事故的时间特征（投运3年内）是反直觉点",
    ),
    (
        "pcs-upgrade",
        "储能变流器的出货结构与技术升级",
        3,
        ("f_tech_pcs", "f_cost_system", "f_comp_cr5"),
        ("doc_14_tech_pcs", "doc_10_cost", "doc_09_competition"),
        ("2025年国内储能PCS出货25GW", "1500V机型占比降至38%"),
        "出货量级 GW 与占比同时出现，容易互相污染",
    ),
    (
        "emc-model",
        "合同能源管理模式的经济性",
        3,
        ("f_biz_emc", "f_biz_arb", "f_cost_irr"),
        ("doc_16_business_model", "doc_10_cost", "doc_07_region_cig"),
        ("合同能源管理模式在浙江、广东的渗透率超过30%", "浙江工商业储能项目全投资内部收益率约8.5%"),
        "IRR 与回收期必须成对正确才成立",
    ),
    (
        "spot-market",
        "电力现货市场对储能收益的重塑",
        4,
        ("f_mkt_spot", "f_mkt_aux", "f_biz_arb", "f_region_price"),
        ("doc_17_market", "doc_07_region_cig", "doc_16_business_model"),
        ("2025年已有4个省份开展电力现货长周期结算试运行", "浙江一般工业电价峰谷价差约0.65元/kWh"),
        "试点省份数、价差金额两处都容易写偏",
    ),
    (
        "overcapacity",
        "产能过剩、价格战与毛利率压缩",
        4,
        ("f_risk_overcap", "f_risk_margin", "f_risk_price", "f_cost_system"),
        ("doc_20_risk", "doc_10_cost", "doc_09_competition"),
        ("2025年储能电芯名义产能利用率不足75%", "储能系统集成商平均毛利率降至28.6%"),
        "三个风险指标同向恶化，考「不把单个数字写反」",
    ),
    (
        "us-market",
        "美国储能市场与出口依赖",
        3,
        ("f_ovs_us", "f_ovs_export", "f_risk_overcap"),
        ("doc_18_overseas_us", "doc_20_risk"),
        ("2025年美国储能新增装机约8GW", "2025年中国企业储能电池出口量约80GWh"),
        "18 与 8、180 与 80 是典型量级错误",
    ),
    (
        "eu-me-market",
        "欧洲户储调整与中东招标放量",
        3,
        ("f_ovs_eu", "f_ovs_me", "f_ovs_export"),
        ("doc_19_overseas_eu_me", "doc_18_overseas_us"),
        ("2025年中东地区储能招标规模超过10GWh", "欧洲户用储能新增装机约19GWh"),
        "两个区域方向相反，考「不把一升一降混为一谈」",
    ),
    (
        "export-scale",
        "储能电池出口规模与结构",
        3,
        ("f_ovs_export", "f_ovs_me", "f_ovs_us"),
        ("doc_18_overseas_us", "doc_19_overseas_eu_me"),
        ("2025年中国企业储能电池出口量约280GWh", "2025年中国储能电池出口同比下降52%"),
        "出口是本题唯一主轴，考「围绕一个数字讲清楚」",
    ),
    (
        "new-install-record",
        "新型储能新增装机的全球位次",
        3,
        ("f_scale_new", "f_scale_total", "f_out_total"),
        ("doc_03_scale_overall", "doc_21_outlook"),
        ("2025年新型储能新增装机1800万千瓦", "连续五年位居全球第一"),
        "累计/新增、连续几年，两个口径都易错",
    ),
    (
        "outlook-2026",
        "2026 年展望：增速回落与结构分化",
        4,
        ("f_out_total", "f_out_cig", "f_out_na", "f_risk_overcap"),
        ("doc_21_outlook", "doc_20_risk", "doc_12_tech_na"),
        ("预计2026年新型储能新增装机增速回落至5%左右", "预计2026年工商业储能新增装机达到31GWh"),
        "预测数字与基数必须成对，否则增速对不上",
    ),
    (
        "policy-quota",
        "新能源配储比例要求与执行",
        3,
        ("f_policy_ratio", "f_policy_target", "f_scale_grid"),
        ("doc_01_policy_national", "doc_02_policy_local", "doc_06_scale_grid"),
        ("多省将新能源配储比例要求提高到15%、时长2小时", "到2027年全国新型储能装机达到3.8亿千瓦"),
        "比例+时长是一对参数，只写对一个也算错",
    ),
)


def render_yaml(spec) -> str:
    topic_id, topic, sections, cover, cite, forbidden, notes = spec
    lines = [
        f"id: {topic_id}",
        f"topic: {topic}",
        f"sections: {sections}",
        f"notes: {notes}",
        "must_cover:",
    ]
    for key in cover:
        if key not in FACTS:
            raise KeyError(f"{topic_id}: 未知 fact key {key}")
        lines.append(f"  - {FACTS[key]}")
    lines.append("must_cite:")
    for doc in cite:
        lines.append(f"  - {doc}")
    lines.append("forbidden:")
    for bad in forbidden:
        lines.append(f"  - {bad}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.out) if args.out else Path(__file__).resolve().parents[2] / "eval" / "topics"
    ids = [t[0] for t in TOPICS]
    if len(set(ids)) != len(ids):
        raise SystemExit("主题 id 重复")

    total_points = sum(len(t[3]) for t in TOPICS)
    if args.dry_run:
        for spec in TOPICS:
            print(f"--- {spec[0]} ({len(spec[3])} 要点)")
            print(render_yaml(spec))
        return

    root.mkdir(parents=True, exist_ok=True)
    for old in root.glob("*.yaml"):
        old.unlink()
    for spec in TOPICS:
        (root / f"{spec[0]}.yaml").write_text(render_yaml(spec), encoding="utf-8")

    print(f"已生成 {len(TOPICS)} 个主题 -> {root}")
    print(f"要点合计 {total_points} 条 | 错误说法 {sum(len(t[5]) for t in TOPICS)} 条")


if __name__ == "__main__":
    main()
