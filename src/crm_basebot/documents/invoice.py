"""佣金 invoice（Referral Fee Statement）：读两张结算表，套 Word 范本，发给点按钮的人。

从 invoice 小工具（okyterrance/4-8-HashKey-Referral-Invoice-Test，本机 Flask）搬过来的，
**范本和填法不变**：

  · 范本 ``templates/invoice-bank.docx`` / ``invoice-crypto.docx``，按渠道的收款方式选；
  · docxtpl 渲染后把 ``wp:docPr`` 的 id 还原成范本原值（docxtpl 会整体 +1000），
    Word / WPS 打开和手做的范本一模一样；
  · 日期写法：期间 ``01/09/2026``，明细行 ``30-September-2026``，付款日 ``3 October 2026``；
  · 转 PDF 只用 Microsoft Word（docx2pdf），不用 LibreOffice —— 原工具就这么定的，格式才一致。

**金额只认结算表**（Commission Summary / ECAS Commission Summary）：那是月结写进去、
实际要付的数。结算之后客户、比例可能又变了（例如 2026-09 补登记了一批客户），现算会和
付出去的对不上。所以：

  · 每个渠道的 invoice 总额 = 结算表里那一行的「应付佣金」；
  · 明细行（每个客户一行）用现算的分摊，**只有现算的合计和结算表一分不差时才用**；
    对不上就只印一行总额，并在结果卡上说出是哪几个渠道 —— 宁可少列明细，不印错数。

交易佣金和 ECAS 各出各的 invoice（两份文件），和原工具一样。
"""

from __future__ import annotations

import calendar
import io
import logging
import re
import subprocess
import sys
import threading
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path

from ..bot.auth import Sales
from ..domain import ecas, schema
from ..domain.ecas_query import load_applications, load_payees
from ..domain.payment import PaymentInfo, PaymentService
from ..lark.bitable import BitableClient
from ..lark.values import extract_text, to_number
from .docx import safe_filename, template_path

logger = logging.getLogger(__name__)

KIND_TRADE = "trade"
KIND_ECAS = "ecas"
KIND_LABEL = {KIND_TRADE: "交易佣金", KIND_ECAS: "ECAS 返佣"}

# 月份下拉里最多列几个月。
PERIOD_OPTIONS = 12

# Word 转 PDF 的超时。第一次要等 Word 启动，还可能弹出「允许访问文件夹」要人点。
PDF_TIMEOUT_SECONDS = 300

CENTS = Decimal("0.01")


# ---------- 格式（照搬原工具） ----------


def last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def period_date(year: int, month: int, day: int) -> str:
    return f"{day:02d}/{month:02d}/{year}"


def table_date(year: int, month: int, day: int) -> str:
    return f"{day:02d}-{date(year, month, 1).strftime('%B')}-{year}"


def payment_date_text(day: date) -> str:
    return f"{day.day} {day.strftime('%B')} {day.year}"


def month_label(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    return f"{date(year, month, 1).strftime('%B')} {year}"


def money(amount: Decimal) -> str:
    return f"{amount:,.2f}"


def rate_text(rate_percent: Decimal) -> str:
    return f"{rate_percent:.2f}%"


# ---------- 一份 invoice ----------


@dataclass(frozen=True)
class InvoiceRow:
    description: str
    amount: Decimal


@dataclass(frozen=True)
class Invoice:
    kind: str
    period: str
    referral_no: str
    referral_name: str
    fee_rate: str
    rows: tuple[InvoiceRow, ...]
    total: Decimal
    payment: PaymentInfo
    itemized: bool = True
    """明细是不是逐客户列的。False = 这个渠道现在一个客户都找不到，只能印一行总额。"""
    reallocated: bool = False
    """明细是按结算金额重新分摊的（结算之后比例或客户变过，现算的数和结算表不一样）。"""

    @property
    def filename(self) -> str:
        suffix = "Referral Fee Statement"
        if self.kind == KIND_ECAS:
            suffix = "ECAS " + suffix
        name = safe_filename(self.referral_name, fallback=self.referral_no)
        return f"{name} - {suffix} ({month_label(self.period)}).docx"

    def context(self, paid_on: date) -> dict:
        year, month = int(self.period[:4]), int(self.period[5:7])
        row_date = table_date(year, month, last_day(year, month))
        clients = list(dict.fromkeys(row.description for row in self.rows))
        lines = list(self.payment.address_lines) + ["", "", ""]
        ctx = {
            "referrer_name": self.referral_name,
            "addr1": lines[0],
            "addr2": lines[1],
            "addr3": lines[2],
            "period_start": period_date(year, month, 1),
            "period_end": period_date(year, month, last_day(year, month)),
            # 客户多于 4 个时一行逗号隔开，否则一行一个 —— 原工具的排版。
            "referral_clients": ", ".join(clients) if len(clients) > 4 else "\n".join(clients),
            "fee_rate": self.fee_rate,
            "payment_date": payment_date_text(paid_on),
            "rows": [
                {"date": row_date, "description": row.description, "amount": money(row.amount)}
                for row in self.rows
            ],
            "total_amount": money(self.total),
        }
        if self.payment.is_bank:
            account_name = self.payment.bank_account_name or self.referral_name
            ctx["account_line1"] = f"ACCOUNT NAME: {account_name}"
            ctx["account_line2"] = f" BANK NAME: {self.payment.bank_name}"
            ctx["account_no"] = self.payment.bank_account_no
        else:
            ctx["crypto_type"] = self.payment.crypto_type or "USDT"
            ctx["wallet_address"] = self.payment.wallet_address
            ctx["usdt_rate"] = "1.0000"
            ctx["total_usdt"] = money(self.total)
        return ctx


def _restore_docpr_ids(rendered: bytes, template: bytes) -> bytes:
    """docxtpl 把每个 wp:docPr 的 id 加了 1000 防撞号，这里改回范本原值。"""
    result = rendered
    for original in re.findall(rb'\bwp:docPr\b[^>]*?\bid="(\d+)"', template):
        bumped = str(int(original) + 1000).encode()
        result = re.sub(
            rb'(wp:docPr\b[^>]*?\bid=)"' + bumped + rb'"', rb'\1"' + original + rb'"', result
        )
    return result


def render(invoice: Invoice, paid_on: date) -> bytes:
    """一份 invoice 的 .docx 字节。"""
    from docxtpl import DocxTemplate  # 只在出 invoice 时才要，别拖慢机器人启动

    name = "invoice-bank.docx" if invoice.payment.is_bank else "invoice-crypto.docx"
    source = template_path(name)
    tpl = DocxTemplate(str(source))
    tpl.render(invoice.context(paid_on))
    buffer = io.BytesIO()
    tpl.save(buffer)

    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as rendered_zip:
        rendered_xml = rendered_zip.read("word/document.xml")
    out = io.BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        rendered_xml = _restore_docpr_ids(rendered_xml, original.read("word/document.xml"))
        for item in original.infolist():
            data = rendered_xml if item.filename == "word/document.xml" else original.read(item)
            z.writestr(item, data)
    return out.getvalue()


# ---------- 转 PDF ----------


# 同一时间只转一批：几个人同时点「生成 Invoice」时共用同一对固定目录，不能互相踩。
_PDF_LOCK = threading.Lock()


def _empty(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for item in folder.iterdir():
        if item.is_file():
            item.unlink()


def to_pdf(docx_files: dict[str, bytes], work_dir: Path) -> tuple[dict[str, bytes], str]:
    """用 Word 把一批 docx 转成 PDF。返回 ({pdf 文件名: 字节}, 出错说明)。

    只在装了 Microsoft Word 的 Mac 上能转（docx2pdf 通过 AppleScript 让 Word 另存）。
    在子进程里跑、带超时：Word 卡住不能把机器人一起卡死。

    **永远用同一对目录** ``work_dir/docx`` 和 ``work_dir/pdf``，不用随机的临时目录。
    Word 有沙盒：第一次读写某个文件夹要人在 mac mini 上点「授权访问」，它只记住点过的
    那个文件夹。换成每次一个随机目录的话，每次都要有人去 mac mini 前点一次（2026-09-29
    真机上撞到的）。转完两个目录都清空：里面有收款账号，不留在磁盘上。
    """
    if not docx_files:
        return {}, ""
    if sys.platform != "darwin":
        return {}, "这台机器不是 Mac，转不了 PDF（只有装了 Word 的 Mac 能转），先发 Word 版。"

    source, target = work_dir / "docx", work_dir / "pdf"
    with _PDF_LOCK:
        _empty(source)
        _empty(target)
        try:
            for name, data in docx_files.items():
                (source / name).write_bytes(data)
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; from docx2pdf import convert; "
                        "convert(sys.argv[1], sys.argv[2])",
                        str(source),
                        str(target),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=PDF_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                return {}, (
                    "Word 转 PDF 超时了（mac mini 上 Word 可能弹了窗口在等人点），先发 Word 版。"
                )
            except subprocess.CalledProcessError as exc:
                logger.error("docx2pdf 失败: %s", exc.stderr.decode(errors="replace")[-2000:])
                return {}, (
                    "Word 转 PDF 失败了，先发 Word 版。管理员可以在 mac mini 的日志里看原因。"
                )

            pdfs: dict[str, bytes] = {}
            for name in docx_files:
                pdf = target / (Path(name).stem + ".pdf")
                if pdf.is_file():
                    pdfs[pdf.name] = pdf.read_bytes()
            missing = len(docx_files) - len(pdfs)
            return pdfs, (f"有 {missing} 份没转出 PDF，那几份只有 Word 版。" if missing else "")
        finally:
            _empty(source)
            _empty(target)


# ---------- 一批 ----------


@dataclass(frozen=True)
class Skipped:
    referral_no: str
    referral_name: str
    missing: tuple[str, ...]


@dataclass
class InvoiceBatch:
    period: str
    paid_on: date
    invoices: list[Invoice] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    docx: dict[str, bytes] = field(default_factory=dict)
    pdf: dict[str, bytes] = field(default_factory=dict)
    pdf_note: str = ""

    @property
    def summary_only(self) -> list[Invoice]:
        return [invoice for invoice in self.invoices if not invoice.itemized]

    @property
    def reallocated(self) -> list[Invoice]:
        return [invoice for invoice in self.invoices if invoice.itemized and invoice.reallocated]

    @property
    def no_address(self) -> list[Invoice]:
        """出了，但地址是空的 —— 结果卡上提醒一句。"""
        seen: dict[str, Invoice] = {}
        for invoice in self.invoices:
            if not invoice.payment.address_lines:
                seen.setdefault(invoice.referral_no, invoice)
        return list(seen.values())

    def files(self) -> list[tuple[str, bytes]]:
        """要发出去的文件。一份就发 Word + PDF 两个；多份就打成一个 zip，免得刷屏。"""
        if not self.docx:
            return []
        if len(self.docx) == 1:
            return [*self.docx.items(), *self.pdf.items()]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
            for name, data in self.docx.items():
                z.writestr(f"Word/{name}", data)
            for name, data in self.pdf.items():
                z.writestr(f"PDF/{name}", data)
        return [(f"Invoices {month_label(self.period)}.zip", buffer.getvalue())]


@dataclass(frozen=True)
class SettledRow:
    referral_no: str
    payable: Decimal
    rate: str


def _summary_rows(
    bitable: BitableClient, table_id: str, period: str, *, rate_field: str, numeric_rate: bool
) -> dict[str, SettledRow]:
    rows: dict[str, SettledRow] = {}
    if not table_id:
        return rows
    for record in bitable.iter_records(table_id):
        fields = record.fields
        if extract_text(fields.get(schema.COMM_PERIOD)).strip() != period:
            continue
        no = extract_text(fields.get(schema.COMM_REFERRAL_NO)).strip()
        payable = to_number(fields.get(schema.COMM_PAYABLE))
        if not no or payable is None:
            continue
        if numeric_rate:
            raw = to_number(fields.get(rate_field))
            rate = rate_text(Decimal(str(raw))) if raw is not None else ""
        else:
            rate = extract_text(fields.get(rate_field)).strip()
        amount = Decimal(str(payable)).quantize(CENTS)
        previous = rows.get(no)
        # 同一个月同一个渠道只该有一行；万一有两行（手工补的），加起来，别丢钱。
        rows[no] = SettledRow(no, amount + (previous.payable if previous else 0), rate)
    return rows


def _periods(bitable: BitableClient, table_id: str, visible: set[str]) -> set[str]:
    found: set[str] = set()
    if not table_id:
        return found
    for record in bitable.iter_records(
        table_id, field_names=[schema.COMM_PERIOD, schema.COMM_REFERRAL_NO]
    ):
        no = extract_text(record.fields.get(schema.COMM_REFERRAL_NO)).strip()
        period = extract_text(record.fields.get(schema.COMM_PERIOD)).strip()
        if no in visible and re.fullmatch(r"\d{4}-\d{2}", period):
            found.add(period)
    return found


@dataclass(frozen=True)
class Part:
    """明细里的一行：谁、按多少分（权重）、现算是多少钱。"""

    description: str
    weight: Decimal
    fresh: Decimal


def allocate(total: Decimal, parts: list[Part]) -> tuple[InvoiceRow, ...]:
    """把结算金额按权重分给各行，**加起来一分不差**等于 ``total``。

    最大余数法：先都往下取到分，差的那几分给被截掉最多的几行。和佣金查询里分客户份额
    是同一个做法（commission_query.ReferralBreakdown.client_shares）。
    """
    weight_sum = sum((p.weight for p in parts), Decimal("0"))
    exact = [total * p.weight / weight_sum for p in parts]
    shares = [value.quantize(CENTS, rounding=ROUND_FLOOR) for value in exact]
    missing = int(((total - sum(shares, Decimal("0"))) / CENTS).to_integral_value())
    order = sorted(range(len(parts)), key=lambda i: (shares[i] - exact[i], i))
    for i in order[:missing]:
        shares[i] += CENTS
    return tuple(InvoiceRow(p.description, share) for p, share in zip(parts, shares, strict=True))


def _rows_for(
    kind: str, period: str, payable: Decimal, parts: list[Part] | None
) -> tuple[tuple[InvoiceRow, ...], bool, bool]:
    """(明细行, 是否逐客户列, 是否重新分摊)。**只要找得到客户就逐个列**（2026-09-29 定的）。"""
    parts = [p for p in parts or [] if p.weight > 0]
    if not parts:
        return _single_row(kind, period, payable), False, False
    if sum((p.fresh for p in parts), Decimal("0")) == payable:
        return tuple(InvoiceRow(p.description, p.fresh) for p in parts), True, False
    return allocate(payable, parts), True, True


def _single_row(invoice_kind: str, period: str, amount: Decimal) -> tuple[InvoiceRow, ...]:
    what = "ECAS referral fee" if invoice_kind == KIND_ECAS else "Referral fee"
    return (InvoiceRow(f"{what} for {month_label(period)}", amount),)


class InvoiceService:
    """出 invoice。谁能出哪些渠道和别处一样：销售自己名下的，管理员全部。"""

    def __init__(
        self,
        bitable: BitableClient,
        *,
        settings,
        payments: PaymentService,
        commission_query,
        tz,
        work_dir: Path,
    ) -> None:
        self._bitable = bitable
        self._settings = settings
        self._payments = payments
        self._commission_query = commission_query
        self._tz = tz
        self._work_dir = work_dir

    def periods_for(self, sales: Sales) -> list[str]:
        """两张结算表里、这个人看得到的渠道出现过的月份，新的在前。"""
        visible = {item.no for item in self._payments.visible(sales)}
        periods = _periods(self._bitable, self._settings.table_commission, visible)
        periods |= _periods(
            self._bitable, getattr(self._settings, "table_ecas_commission", ""), visible
        )
        return sorted(periods, reverse=True)[:PERIOD_OPTIONS]

    def build(self, sales: Sales, period: str, *, kinds: Iterable[str]) -> InvoiceBatch:
        """算出这一批要出哪些 invoice（还没生成文件）。"""
        wanted = set(kinds)
        channels = {item.no: item for item in self._payments.visible(sales)}
        batch = InvoiceBatch(period=period, paid_on=date.today())

        settled: list[tuple[str, SettledRow]] = []
        if KIND_TRADE in wanted:
            trade = _summary_rows(
                self._bitable,
                self._settings.table_commission,
                period,
                rate_field=schema.COMM_RATE,
                numeric_rate=True,
            )
            settled += [(KIND_TRADE, row) for row in trade.values()]
        if KIND_ECAS in wanted:
            ecas_rows = _summary_rows(
                self._bitable,
                getattr(self._settings, "table_ecas_commission", ""),
                period,
                rate_field=ecas.ECOMM_RATE_NOTE,
                numeric_rate=False,
            )
            settled += [(KIND_ECAS, row) for row in ecas_rows.values()]

        settled = [
            (kind, row) for kind, row in settled if row.referral_no in channels and row.payable > 0
        ]
        if not settled:
            return batch

        trade_breakdown = self._trade_breakdown(sales, period) if KIND_TRADE in wanted else {}
        ecas_breakdown = self._ecas_breakdown(period, channels) if KIND_ECAS in wanted else {}

        skipped: dict[str, Skipped] = {}
        # 同一个渠道交易佣金在前、ECAS 在后。
        settled.sort(key=lambda item: (item[1].referral_no, item[0] != KIND_TRADE))
        for kind, row in settled:
            channel = channels[row.referral_no]
            # 地址不算缺（2026-09-29 定的）：照出，结果卡上提醒。缺的只有收款方式和账户。
            lacking = channel.info.missing_for_payment()
            if lacking:
                skipped[channel.no] = Skipped(channel.no, channel.name, tuple(lacking))
                continue
            parts = (trade_breakdown if kind == KIND_TRADE else ecas_breakdown).get(channel.no)
            rows, itemized, reallocated = _rows_for(kind, period, row.payable, parts)
            batch.invoices.append(
                Invoice(
                    kind=kind,
                    period=period,
                    referral_no=channel.no,
                    referral_name=channel.name or channel.no,
                    fee_rate=row.rate,
                    rows=rows,
                    total=row.payable,
                    payment=channel.info,
                    itemized=itemized,
                    reallocated=reallocated,
                )
            )
        batch.skipped = list(skipped.values())
        return batch

    def generate(
        self, sales: Sales, period: str, *, kinds: Iterable[str], paid_on: date
    ) -> InvoiceBatch:
        """算好、生成 Word、转 PDF。文件在返回的 batch 里，由调用方发出去。"""
        batch = self.build(sales, period, kinds=kinds)
        batch.paid_on = paid_on
        for invoice in batch.invoices:
            batch.docx[invoice.filename] = render(invoice, paid_on)
        batch.pdf, batch.pdf_note = to_pdf(batch.docx, self._work_dir)
        return batch

    # ---------- 明细（现算，只在和结算表对得上时用） ----------

    def _trade_breakdown(self, sales: Sales, period: str) -> dict[str, list[Part]]:
        if self._commission_query is None:
            return {}
        result = self._commission_query.query(sales, [period])
        out: dict[str, list[Part]] = {}
        for referral in result.referrals_in(period):
            shares = referral.client_shares()
            out[referral.referral_no] = [
                Part(client.name or client.uid, client.revenue, shares[uid])
                for uid, client in sorted(
                    referral.clients.items(), key=lambda item: item[1].name or item[0]
                )
            ]
        return out

    def _ecas_breakdown(self, period: str, channels: dict) -> dict[str, list[Part]]:
        table = getattr(self._settings, "table_ecas", "")
        if not table:
            return {}
        payees = load_payees(self._bitable, self._settings.table_referral)
        out: dict[str, list[Part]] = {}
        for app in load_applications(self._bitable, table, payees, tz=self._tz):
            if app.period != period or app.payee is None or app.payee.code not in channels:
                continue
            out.setdefault(app.payee.code, []).append(Part(app.client_name, app.fee, app.fee))
        return out
