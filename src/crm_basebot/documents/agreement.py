"""HTS Referral Agreement（转介协议）：个人 / 企业两种，填表生成 Word。

从另一个机器人（Palebluedot-ai/onboard-bot-lark 的「填单」，Cloudflare Worker + JS）
搬过来的，**范本和填法一模一样**：

  · 范本 ``templates/agreement-{individual,corporate}.docx`` 里每个要填的地方都已经换成
    干净的 ``{{TOKEN}}``（没有高亮、没有方括号）。填写就是在 ``word/document.xml``
    里逐个替换，值先做 XML 转义。
  · 替换完还剩 ``{{...}}`` 就报错，不发一份半空的协议出去。
  · 结算周期不填默认 quarterly；生效日期不填默认今天（新加坡时间），写成
    ``September 29, 2026``；「通知地址」不填就用正文地址；企业的「联系人」不填就用签署人。

**不写 Base。** 证件号、注册号这些只用来填这一份协议，生成完就丢，不进任何表。
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import date

from .docx import safe_filename, template_path

KIND_INDIVIDUAL = "individual"
KIND_CORPORATE = "corporate"
KINDS = (KIND_INDIVIDUAL, KIND_CORPORATE)

KIND_LABEL = {KIND_INDIVIDUAL: "个人 (Individual)", KIND_CORPORATE: "企业 (Corporate)"}

# 协议里「对账单几天内发」—— 两个范本都写死这一句，照搬原 bot。
STATEMENT_DAYS = "five (5)"


class AgreementError(ValueError):
    """填的东西不对。消息直接给人看。"""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    placeholder: str
    required: bool = True


# 表单上的格子，顺序就是卡片上的顺序。key 同时是卡片 input 的 name。
FIELDS: dict[str, tuple[Field, ...]] = {
    KIND_INDIVIDUAL: (
        Field("client_name", "姓名", "Zhang San"),
        Field("id_number", "证件号", "A1234567"),
        Field("full_address", "地址", "Flat 1, 5/F, Example Building, Central, Hong Kong"),
        Field("email", "邮箱", "name@example.com"),
        Field("fee_pct", "费率 (%)", "40"),
        Field("notice_address", "通知地址（不填就用上面的地址）", "", required=False),
    ),
    KIND_CORPORATE: (
        Field("company_name", "公司名称", "Acme Trading Ltd"),
        Field("registration_number", "注册号", "CR123456"),
        Field("jurisdiction", "成立地", "Hong Kong"),
        Field("registered_address", "注册地址", "Suite 1, 2/F, ABC Tower, Central, Hong Kong"),
        Field("email", "邮箱", "name@example.com"),
        Field("signatory_name", "签署人姓名", "Jane Doe"),
        Field("signatory_title", "签署人职位", "Director"),
        Field("fee_pct", "费率 (%)", "40"),
        Field("notice_attention", "联系人（不填就用签署人）", "", required=False),
        Field("notice_address", "通知地址（不填就用注册地址）", "", required=False),
    ),
}

FEE_PERIODS = ("quarterly", "monthly")

_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
_FEE = re.compile(r"^\d+(\.\d+)?$")
# 飞书会把消息里的邮箱自动变成 [a@b.com](mailto:a@b.com)。表单输入一般不会，
# 但原 bot 在这上面吃过亏（协议里印出了方括号），照样剥一遍，不花什么。
_MD_LINK = re.compile(r"\[([^\[\]]+)\]\((?:mailto:)?[^()]*\)")


def _clean(value: object) -> str:
    return _MD_LINK.sub(r"\1", str(value or "")).strip()


def normalize_fee_pct(raw: str) -> str:
    """``40`` / ``40%`` / ``40 %`` -> ``40%``。不是 0–100 的数就报错。"""
    text = _clean(raw).replace(" ", "").rstrip("%")
    if not _FEE.match(text) or not 0 < float(text) <= 100:
        raise AgreementError(f"费率要填 0–100 的数字，例如 40 表示 40%，你填的是「{raw}」")
    return f"{text}%"


def normalize_fee_period(raw: str) -> str:
    text = _clean(raw).lower()
    if not text:
        return "quarterly"
    if "月" in text or "month" in text:
        return "monthly"
    if "季" in text or "quarter" in text:
        return "quarterly"
    raise AgreementError(f"结算周期只能是 monthly 或 quarterly，你选的是「{raw}」")


def english_date(day: date) -> str:
    """2026-09-29 -> ``September 29, 2026``（和原 bot 的 en-US 写法一样）。"""
    return f"{day.strftime('%B')} {day.day}, {day.year}"


@dataclass(frozen=True)
class AgreementPlan:
    kind: str
    primary_name: str
    tokens: dict[str, str]

    @property
    def filename(self) -> str:
        suffix = " - Corporate" if self.kind == KIND_CORPORATE else ""
        return f"HTS Referral Agreement{suffix} - {safe_filename(self.primary_name)}.docx"


def plan(
    kind: str,
    values: dict[str, object],
    *,
    fee_period: str = "",
    effective: date,
) -> AgreementPlan:
    """表单填的值 -> 要替换进范本的 token。缺必填、邮箱 / 费率不对都抛 AgreementError。"""
    if kind not in KINDS:
        raise AgreementError(f"不认识的协议类型：{kind}")
    got = {field.key: _clean(values.get(field.key)) for field in FIELDS[kind]}
    missing = [f.label for f in FIELDS[kind] if f.required and not got[f.key]]
    if missing:
        raise AgreementError(f"还没填：{'、'.join(missing)}")
    if not _EMAIL.match(got["email"]):
        raise AgreementError(f"邮箱格式不对：「{got['email']}」")

    common = {
        "EFFECTIVE_DATE": english_date(effective),
        "FEE_PERIOD": normalize_fee_period(fee_period),
        "STATEMENT_DAYS": STATEMENT_DAYS,
        "NOTICE_EMAIL": got["email"],
        "REFERRAL_FEE_PCT": normalize_fee_pct(got["fee_pct"]),
    }
    if kind == KIND_INDIVIDUAL:
        tokens = {
            **common,
            "CLIENT_NAME": got["client_name"],
            "ID_NUMBER": got["id_number"],
            "FULL_ADDRESS": got["full_address"],
            "NOTICE_ADDRESS": got["notice_address"] or got["full_address"],
            "NOTICE_ATTENTION": got["client_name"],
        }
        return AgreementPlan(kind, got["client_name"], tokens)

    tokens = {
        **common,
        "COMPANY_NAME": got["company_name"],
        "REGISTRATION_NUMBER": got["registration_number"],
        "INCORPORATION_JURISDICTION": got["jurisdiction"],
        "REGISTERED_ADDRESS": got["registered_address"],
        "NOTICE_ADDRESS": got["notice_address"] or got["registered_address"],
        "NOTICE_ATTENTION": got["notice_attention"] or got["signatory_name"],
        "SIGNATORY_NAME": got["signatory_name"],
        "SIGNATORY_TITLE": got["signatory_title"],
    }
    return AgreementPlan(kind, got["company_name"], tokens)


def _escape_xml(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_LEFTOVER = re.compile(r"\{\{[A-Z_]+\}\}")


def render(agreement: AgreementPlan) -> bytes:
    """填好范本，返回 .docx 的字节。范本里其余的文件原样拷贝。"""
    source = template_path(f"agreement-{agreement.kind}.docx")
    out = io.BytesIO()
    with zipfile.ZipFile(source) as template, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for item in template.infolist():
            data = template.read(item.filename)
            if item.filename == "word/document.xml":
                xml = data.decode("utf-8")
                for token, value in agreement.tokens.items():
                    xml = xml.replace("{{" + token + "}}", _escape_xml(value))
                leftover = sorted(set(_LEFTOVER.findall(xml)))
                if leftover:
                    raise AgreementError(f"范本里还有没填的地方：{', '.join(leftover)}")
                data = xml.encode("utf-8")
            z.writestr(item, data)
    return out.getvalue()
