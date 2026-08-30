"""Excel 截断的事后诊断。

test_values.py 保证 UID 在**我们手里**不丢精度。但如果 UID 在进 Base 之前就被
Excel 改坏了，我们拿到的字符串本身已经是错的，下游再严格也救不回来。
这里测的是识别那种损伤的启发式。

关键取舍：这是启发式，会误报。所以测试既要证明它抓得住真损伤，
也要证明它不会把合法 UID 一片片地误杀 —— 后者更重要，
一个天天喊狼来了的告警等于没有告警。
"""

from crm_basebot.lark.values import (
    EXCEL_SIGNIFICANT_DIGITS,
    assess_uid_health,
    chance_of_false_positive,
    looks_excel_truncated,
    looks_scientific_notation,
    uid_health_advice,
)

# 截图里的真实 UID
UID_18 = "577809207768677761"
UID_19 = "2141293991366272768"

# UID_18 过一遍 Excel 之后的样子：保留 15 位有效数字，后 3 位归零
UID_18_TRUNCATED = "577809207768678000"
UID_19_TRUNCATED = "2141293991366270000"


# ---------- 不能误报 ----------


def test_真实的18位和19位uid不被误报():
    assert not looks_excel_truncated(UID_18)
    assert not looks_excel_truncated(UID_19)


def test_短uid一律不报():
    # 15 位及以下 Excel 能完整表示，不存在截断问题，
    # 哪怕全是 0 结尾也不该报
    assert not looks_excel_truncated("123456789012345")
    assert not looks_excel_truncated("100000000000000")
    assert not looks_excel_truncated("1000")
    assert not looks_excel_truncated("0")


def test_合法uid只是尾部零不够多就不报():
    # 18 位需要尾部 3 个 0 才算命中，只有 2 个不算
    assert not looks_excel_truncated("577809207768678100")
    # 19 位需要 4 个，只有 3 个不算
    assert not looks_excel_truncated("2141293991366271000")


def test_非纯数字不当截断处理():
    assert not looks_excel_truncated("")
    assert not looks_excel_truncated("N/A")
    assert not looks_excel_truncated("UID-577809207768678000")


# ---------- 要抓得住 ----------


def test_被excel截断的18位uid被识别():
    assert looks_excel_truncated(UID_18_TRUNCATED)


def test_被excel截断的19位uid被识别():
    assert looks_excel_truncated(UID_19_TRUNCATED)


def test_尾零多于必需数量也算命中():
    # 舍入后低位可能不止 (长度-15) 个 0
    assert looks_excel_truncated("577809207768600000")


def test_科学计数法被识别():
    assert looks_scientific_notation("5.77809E+17")
    assert looks_scientific_notation("5.77809e+17")
    assert looks_scientific_notation("5.7780920776867789e+17")
    # 带小数点也是被当数字处理过的证据
    assert looks_scientific_notation("577809207768678000.0")
    assert not looks_scientific_notation(UID_18)


# ---------- 边界长度 ----------


def test_边界长度16位():
    # 16 位超出 Excel 的 15 位，需要尾部 1 个 0
    assert looks_excel_truncated("1234567890123450")
    assert not looks_excel_truncated("1234567890123456")


def test_正好15位是分界线():
    # 15 位是 Excel 能精确保留的上限，不检测
    assert len("123456789012340") == EXCEL_SIGNIFICANT_DIGITS
    assert not looks_excel_truncated("123456789012340")
    # 多一位就开始检测
    assert looks_excel_truncated("1234567890123400")


def test_误报概率随长度指数下降():
    # 这是「为什么 18/19 位可信而 16 位不可信」的量化依据
    assert chance_of_false_positive("1234567890123456") == 0.1
    assert chance_of_false_positive(UID_18) == 0.001
    assert chance_of_false_positive(UID_19) == 0.0001
    # 15 位及以下不参与判定
    assert chance_of_false_positive("123456789012345") == 0.0


# ---------- 聚合判定 ----------


def test_干净数据判定为clean():
    report = assess_uid_health([UID_18, UID_19, "577809207768677762"])
    assert report.verdict == "clean"
    assert report.suspicious_count == 0
    assert report.long_count == 3


def test_没有长uid时不做判断():
    report = assess_uid_health(["12345", "678", ""])
    assert report.verdict == "no_long_uid"
    assert report.long_count == 0


def test_整批被截断判定为likely_damaged():
    report = assess_uid_health([UID_18_TRUNCATED, UID_19_TRUNCATED, "577809207768600000"])
    assert report.verdict == "likely_damaged"
    assert len(report.truncated) == 3


def test_单个孤立命中不下结论():
    # 一个 18 位 UID 巧合以 3 个 0 结尾是完全可能的，
    # 不能因为这一个就让人去重导数据
    uids = [UID_18_TRUNCATED] + [f"5778092077686{i:05d}" for i in range(1, 60)]
    report = assess_uid_health(uids)
    assert report.verdict == "inconclusive"


def test_16位数据的巧合噪声不会被误判为损坏():
    # 16 位 UID 里天然有约 10% 以 0 结尾。如果只看单值命中数就会天天误报，
    # 这里验证聚合判定顶住了这种噪声。
    uids = [f"123456789012{i:04d}" for i in range(200)]
    report = assess_uid_health(uids)
    assert report.long_count == 200
    # 确实有一批命中（约 20 个），但都在巧合期望范围内
    assert len(report.truncated) > 10
    assert report.verdict == "inconclusive"


def test_科学计数法单个命中就足以下结论():
    # 不同于尾零，小数点/指数没有别的解释，不需要聚合证据
    report = assess_uid_health([UID_18, UID_19, "5.77809E+17"])
    assert report.verdict == "likely_damaged"
    assert report.scientific == ["5.77809E+17"]


def test_空值不计入统计():
    report = assess_uid_health([UID_18, "", "   ", UID_19])
    assert report.total == 2
    assert report.long_count == 2


def test_建议文案对损坏数据给出可执行动作():
    report = assess_uid_health([UID_18_TRUNCATED, UID_19_TRUNCATED])
    advice = uid_health_advice(report)
    # 用户需要知道去找谁、改什么
    assert "文本" in advice
    assert "导出" in advice


def test_建议文案对干净数据不吓人():
    advice = uid_health_advice(assess_uid_health([UID_18, UID_19]))
    assert "没有发现" in advice
