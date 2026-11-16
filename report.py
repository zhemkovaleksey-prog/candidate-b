"""Отчёт по авансам договоров на выбранную дату.

Скрипт читает три CSV, восстанавливает состояние на дату, зачитывает аванс
и записывает два CSV: работы и итоги договоров.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path


logger = logging.getLogger("advance_report")

REQUIRED_FILES = ("contracts.csv", "works.csv", "payments.csv")
MAX_PATH_LENGTH = 4096
LOG_TEXT_LIMIT = 300
KOPECK = Decimal("0.01")
ZERO = Decimal("0.00")
WORK_STATUSES = ("planned", "done", "cancelled")
PAYMENT_STATUSES = ("paid", "queued", "refunded")
STATE_LABEL = {
    "cancelled": "отменена",
    "done": "принята",
    "planned": "в плане",
}


class PreflightError(Exception):
    """Входные данные или права не дают даже начать расчёт."""


@dataclass(frozen=True)
class Contract:
    contract_id: str
    contractor: str
    amount: Decimal
    signed_at: date


@dataclass(frozen=True)
class Work:
    work_id: str
    contract_id: str
    title: str
    price: Decimal
    status: str
    completed_at: date | None
    cancelled_at: date | None


@dataclass(frozen=True)
class Payment:
    payment_id: str
    contract_id: str
    work_id: str | None
    purpose: str
    amount: Decimal
    status: str
    created_at: date
    paid_at: date | None
    refunded_at: date | None


@dataclass(frozen=True)
class WorkAllocation:
    work: Work
    state: str
    paid: Decimal
    overpay: Decimal


@dataclass(frozen=True)
class ContractAllocation:
    contract: Contract
    works: tuple[WorkAllocation, ...]
    cash: Decimal
    queued: Decimal
    free_advance: Decimal


def for_log(value: object, limit: int = LOG_TEXT_LIMIT) -> str:
    text = str(value).replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + "...(обрезано)"
    return text


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def parse_report_date(value: str) -> date:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError(
            "Дата пустая. Ожидается ГГГГ-ММ-ДД или ДД.ММ.ГГГГ."
        )
    if len(text) > 32:
        raise argparse.ArgumentTypeError("Дата длиннее 32 символов.")
    if any(ch not in "0123456789.-" for ch in text):
        raise argparse.ArgumentTypeError(
            "В дате допустимы только цифры, точка и дефис."
        )
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        "Некорректная дата: {0!r}. Ожидается ГГГГ-ММ-ДД или ДД.ММ.ГГГГ.".format(text)
    )


def parse_path_arg(value: str) -> Path:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("Путь пустой.")
    if "\x00" in text:
        raise argparse.ArgumentTypeError("В пути есть символ NUL.")
    if len(text) > MAX_PATH_LENGTH:
        raise argparse.ArgumentTypeError(
            "Путь длиннее {0} символов.".format(MAX_PATH_LENGTH)
        )
    return Path(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Отчёт на дату: по каждой работе цена, выплачено и остаток, "
            "итоги по каждому договору."
        ),
    )
    parser.add_argument(
        "--as-of",
        type=parse_report_date,
        default=date(2026, 11, 10),
        help="Дата отчёта: ГГГГ-ММ-ДД или ДД.ММ.ГГГГ (по умолчанию 2026-11-10).",
    )
    parser.add_argument(
        "--data",
        type=parse_path_arg,
        default=Path("data"),
        help="Папка с contracts.csv, works.csv и payments.csv (по умолчанию data).",
    )
    parser.add_argument(
        "--out",
        type=parse_path_arg,
        default=Path("out"),
        help="Папка для CSV отчёта (по умолчанию out).",
    )
    return parser.parse_args(argv)


def resolve_existing_dir(path: Path, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise PreflightError(
            "{0}: путь не разбирается ({1}): {2}".format(label, for_log(path), exc.strerror or exc)
        ) from exc
    logger.info("%s: проверяю %s", label, for_log(resolved))
    if not resolved.exists():
        raise PreflightError("{0} не найдена: {1}".format(label, resolved))
    if not resolved.is_dir():
        raise PreflightError("{0} оказалась файлом, нужна папка: {1}".format(label, resolved))
    if not os.access(resolved, os.R_OK | os.X_OK):
        raise PreflightError("Нет прав на чтение папки {0}: {1}".format(label, resolved))
    return resolved


def check_source_file(directory: Path, name: str) -> None:
    path = directory / name
    logger.info("Файл %s: проверяю %s", name, for_log(path))
    if not path.exists():
        raise PreflightError("Нет файла {0}".format(path))
    if path.is_symlink():
        logger.info("Файл %s: это ссылка на %s", name, for_log(path.resolve()))
    if not path.is_file():
        raise PreflightError("{0} не является файлом".format(path))
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise PreflightError(
            "Не удалось прочитать сведения о {0}: {1}".format(path, exc.strerror or exc)
        ) from exc
    if size == 0:
        raise PreflightError("Файл пустой: {0}".format(path))
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except PermissionError as exc:
        raise PreflightError("Нет прав на чтение {0}".format(path)) from exc
    except OSError as exc:
        raise PreflightError(
            "Файл нельзя прочитать {0}: {1}".format(path, exc.strerror or exc)
        ) from exc
    logger.info("Файл %s: доступен, размер %s байт", name, size)


def check_output_dir(path: Path, data_dir: Path) -> Path:
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise PreflightError(
            "Папка отчёта не разбирается ({0}): {1}".format(for_log(path), exc.strerror or exc)
        ) from exc
    logger.info("Папка отчёта: проверяю %s", for_log(resolved))
    if resolved == data_dir:
        raise PreflightError(
            "Папка отчёта совпадает с папкой данных ({0}). "
            "Исходные CSV нельзя писать поверх выгрузки.".format(resolved)
        )
    if resolved.exists():
        if not resolved.is_dir():
            raise PreflightError("Папка отчёта оказалась файлом: {0}".format(resolved))
        probe_dir = resolved
    else:
        probe_dir = resolved.parent
        if not probe_dir.is_dir():
            raise PreflightError(
                "Нет родительской папки, куда класть отчёт: {0}".format(probe_dir)
            )
    try:
        with tempfile.NamedTemporaryFile(dir=probe_dir, prefix=".advance-report-write-test-"):
            pass
    except PermissionError as exc:
        raise PreflightError("Нет прав на запись в {0}".format(probe_dir)) from exc
    except OSError as exc:
        raise PreflightError(
            "Нельзя записать отчёт в {0}: {1}".format(probe_dir, exc.strerror or exc)
        ) from exc
    if resolved.exists():
        logger.info("Запись в папку отчёта доступна: %s", for_log(resolved))
    else:
        logger.info(
            "Папки отчёта ещё нет: %s. Запись в родительскую папку доступна: %s",
            for_log(resolved),
            for_log(probe_dir),
        )
    return resolved


def read_table(path: Path, columns: tuple[str, ...]) -> list[tuple[int, dict[str, str]]]:
    logger.info("Читаю %s", for_log(path))
    rows: list[tuple[int, dict[str, str]]] = []
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fieldnames = reader.fieldnames
            if not fieldnames:
                raise PreflightError("В {0} нет заголовка".format(path))
            missing = [name for name in columns if name not in fieldnames]
            if missing:
                raise PreflightError(
                    "В {0} нет колонок: {1}".format(path, ", ".join(missing))
                )
            for line_no, row in enumerate(reader, start=2):
                extra = row.get(None)
                if extra:
                    raise PreflightError(
                        "{0}:{1}: в строке больше колонок, чем в заголовке".format(path, line_no)
                    )
                if all(not (value or "").strip() for key, value in row.items() if key is not None):
                    logger.info("%s:%s: пустая строка, пропускаю", for_log(path), line_no)
                    continue
                cleaned: dict[str, str] = {}
                for name in columns:
                    value = row.get(name)
                    if value is None:
                        raise PreflightError(
                            "{0}:{1}: не хватает колонки {2}".format(path, line_no, name)
                        )
                    if "\n" in value or "\r" in value:
                        raise PreflightError(
                            "{0}:{1}: в поле {2} перенос строки, кавычка в CSV не закрыта".format(
                                path, line_no, name
                            )
                        )
                    cleaned[name] = value
                rows.append((line_no, cleaned))
    except PermissionError as exc:
        raise PreflightError("Нет прав на чтение {0}".format(path)) from exc
    except UnicodeDecodeError as exc:
        raise PreflightError(
            "{0}: файл не в UTF-8 ({1})".format(path, exc.reason)
        ) from exc
    except csv.Error as exc:
        raise PreflightError("{0}: CSV не разбирается: {1}".format(path, exc)) from exc
    if not rows:
        raise PreflightError("В {0} нет ни одной строки данных".format(path))
    logger.info("%s: строк данных %s", path.name, len(rows))
    return rows


def cell(row: dict[str, str], name: str) -> str:
    value = row.get(name)
    if value is None:
        return ""
    return value.strip()


def require_text(path: Path, line_no: int, name: str, value: str) -> str:
    if not value:
        raise PreflightError("{0}:{1}: пустое поле {2}".format(path, line_no, name))
    return value


def parse_money(path: Path, line_no: int, name: str, value: str) -> Decimal:
    text = require_text(path, line_no, name, value)
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise PreflightError(
            "{0}:{1}: поле {2} не является суммой: {3}".format(path, line_no, name, for_log(text))
        ) from exc
    if not amount.is_finite():
        raise PreflightError(
            "{0}:{1}: поле {2} не является конечной суммой".format(path, line_no, name)
        )
    if amount < ZERO:
        raise PreflightError(
            "{0}:{1}: поле {2} отрицательное: {3}".format(path, line_no, name, for_log(text))
        )
    kopecks = amount.quantize(KOPECK)
    if kopecks != amount:
        raise PreflightError(
            "{0}:{1}: в поле {2} дробь мельче копейки: {3}".format(
                path, line_no, name, for_log(text)
            )
        )
    return kopecks


def parse_iso_date(path: Path, line_no: int, name: str, value: str) -> date:
    text = require_text(path, line_no, name, value)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise PreflightError(
            "{0}:{1}: поле {2} не дата ГГГГ-ММ-ДД: {3}".format(path, line_no, name, for_log(text))
        ) from exc


def parse_optional_date(path: Path, line_no: int, name: str, value: str) -> date | None:
    if not value:
        return None
    return parse_iso_date(path, line_no, name, value)


def require_choice(
    path: Path, line_no: int, name: str, value: str, allowed: tuple[str, ...]
) -> str:
    text = require_text(path, line_no, name, value)
    if text not in allowed:
        raise PreflightError(
            "{0}:{1}: поле {2} равно {3}, допустимо: {4}".format(
                path, line_no, name, for_log(text), ", ".join(allowed)
            )
        )
    return text


def require_unique(path: Path, id_name: str, pairs: list[tuple[int, str]]) -> None:
    seen: dict[str, int] = {}
    for line_no, item_id in pairs:
        previous = seen.get(item_id)
        if previous is not None:
            raise PreflightError(
                "{0}: {1} {2} повторяется, строки {3} и {4}".format(
                    path, id_name, item_id, previous, line_no
                )
            )
        seen[item_id] = line_no


def load_contracts(path: Path) -> list[Contract]:
    columns = ("contract_id", "contractor", "amount", "signed_at")
    contracts: list[Contract] = []
    id_rows: list[tuple[int, str]] = []
    for line_no, row in read_table(path, columns):
        contract_id = require_text(path, line_no, "contract_id", cell(row, "contract_id"))
        id_rows.append((line_no, contract_id))
        contracts.append(
            Contract(
                contract_id=contract_id,
                contractor=require_text(path, line_no, "contractor", cell(row, "contractor")),
                amount=parse_money(path, line_no, "amount", cell(row, "amount")),
                signed_at=parse_iso_date(path, line_no, "signed_at", cell(row, "signed_at")),
            )
        )
    require_unique(path, "contract_id", id_rows)
    return contracts


def load_works(path: Path) -> list[Work]:
    columns = (
        "work_id",
        "contract_id",
        "title",
        "price",
        "status",
        "completed_at",
        "cancelled_at",
    )
    works: list[Work] = []
    id_rows: list[tuple[int, str]] = []
    for line_no, row in read_table(path, columns):
        work_id = require_text(path, line_no, "work_id", cell(row, "work_id"))
        id_rows.append((line_no, work_id))
        works.append(
            Work(
                work_id=work_id,
                contract_id=require_text(path, line_no, "contract_id", cell(row, "contract_id")),
                title=require_text(path, line_no, "title", cell(row, "title")),
                price=parse_money(path, line_no, "price", cell(row, "price")),
                status=require_choice(path, line_no, "status", cell(row, "status"), WORK_STATUSES),
                completed_at=parse_optional_date(
                    path, line_no, "completed_at", cell(row, "completed_at")
                ),
                cancelled_at=parse_optional_date(
                    path, line_no, "cancelled_at", cell(row, "cancelled_at")
                ),
            )
        )
    require_unique(path, "work_id", id_rows)
    return works


def load_payments(path: Path) -> list[Payment]:
    columns = (
        "payment_id",
        "contract_id",
        "work_id",
        "purpose",
        "amount",
        "status",
        "created_at",
        "paid_at",
        "refunded_at",
    )
    payments: list[Payment] = []
    id_rows: list[tuple[int, str]] = []
    for line_no, row in read_table(path, columns):
        payment_id = require_text(path, line_no, "payment_id", cell(row, "payment_id"))
        id_rows.append((line_no, payment_id))
        work_id = cell(row, "work_id") or None
        payments.append(
            Payment(
                payment_id=payment_id,
                contract_id=require_text(path, line_no, "contract_id", cell(row, "contract_id")),
                work_id=work_id,
                purpose=require_text(path, line_no, "purpose", cell(row, "purpose")),
                amount=parse_money(path, line_no, "amount", cell(row, "amount")),
                status=require_choice(
                    path, line_no, "status", cell(row, "status"), PAYMENT_STATUSES
                ),
                created_at=parse_iso_date(path, line_no, "created_at", cell(row, "created_at")),
                paid_at=parse_optional_date(path, line_no, "paid_at", cell(row, "paid_at")),
                refunded_at=parse_optional_date(
                    path, line_no, "refunded_at", cell(row, "refunded_at")
                ),
            )
        )
    require_unique(path, "payment_id", id_rows)
    return payments


def money(amount: Decimal) -> str:
    return format(amount.quantize(KOPECK), "f")


def csv_text(value: str) -> str:
    """Текст для CSV, который откроют в Excel.

    Ячейка, начинающаяся с = + - @, в таблице может считаться формулой.
    Суммы сюда не передаются: отрицательный остаток должен остаться числом.
    """
    if value[:1] in ("=", "+", "-", "@", "\t"):
        return "'" + value
    return value


def check_ledger(
    contracts: list[Contract], works: list[Work], payments: list[Payment]
) -> None:
    contract_ids = {contract.contract_id for contract in contracts}
    works_by_id = {work.work_id: work for work in works}
    for work in works:
        if work.contract_id not in contract_ids:
            raise PreflightError(
                "Работа {0} ссылается на неизвестный договор {1}".format(
                    work.work_id, work.contract_id
                )
            )
        if work.completed_at is not None and work.cancelled_at is not None:
            raise PreflightError(
                "У работы {0} заполнены и дата приёмки, и дата отмены".format(work.work_id)
            )
        if work.status == "done" and work.completed_at is None:
            raise PreflightError("Работа {0} в статусе done без completed_at".format(work.work_id))
        if work.status == "cancelled" and work.cancelled_at is None:
            raise PreflightError(
                "Работа {0} в статусе cancelled без cancelled_at".format(work.work_id)
            )
        if work.status == "planned" and (work.completed_at is not None or work.cancelled_at is not None):
            raise PreflightError(
                "Работа {0} в статусе planned, но дата приёмки или отмены заполнена".format(
                    work.work_id
                )
            )
    for payment in payments:
        if payment.contract_id not in contract_ids:
            raise PreflightError(
                "Платёж {0} ссылается на неизвестный договор {1}".format(
                    payment.payment_id, payment.contract_id
                )
            )
        if payment.work_id is not None:
            work = works_by_id.get(payment.work_id)
            if work is None:
                raise PreflightError(
                    "Платёж {0} ссылается на неизвестную работу {1}".format(
                        payment.payment_id, payment.work_id
                    )
                )
            if work.contract_id != payment.contract_id:
                raise PreflightError(
                    "Платёж {0} по договору {1}, а работа {2} по договору {3}".format(
                        payment.payment_id,
                        payment.contract_id,
                        payment.work_id,
                        work.contract_id,
                    )
                )
        if payment.paid_at is not None and payment.paid_at < payment.created_at:
            raise PreflightError(
                "Платёж {0}: paid_at раньше created_at".format(payment.payment_id)
            )
        if payment.refunded_at is not None:
            if payment.paid_at is None or payment.refunded_at < payment.paid_at:
                raise PreflightError(
                    "Платёж {0}: возврат стоит раньше выплаты или выплаты нет".format(
                        payment.payment_id
                    )
                )
        if payment.status == "paid" and (payment.paid_at is None or payment.refunded_at is not None):
            raise PreflightError(
                "Платёж {0} в статусе paid, но нет paid_at или уже заполнен refunded_at".format(
                    payment.payment_id
                )
            )
        if payment.status == "queued" and (payment.paid_at is not None or payment.refunded_at is not None):
            raise PreflightError(
                "Платёж {0} в статусе queued, но дата выплаты или возврата заполнена".format(
                    payment.payment_id
                )
            )
        if payment.status == "refunded" and (payment.paid_at is None or payment.refunded_at is None):
            raise PreflightError(
                "Платёж {0} в статусе refunded без paid_at или refunded_at".format(
                    payment.payment_id
                )
            )


def payment_cash(payment: Payment, as_of: date) -> Decimal:
    if payment.paid_at is None or payment.paid_at > as_of:
        return ZERO
    if payment.refunded_at is not None and payment.refunded_at <= as_of:
        return ZERO
    return payment.amount


def payment_queued(payment: Payment, as_of: date) -> Decimal:
    if payment.created_at > as_of:
        return ZERO
    if payment.paid_at is not None and payment.paid_at <= as_of:
        return ZERO
    return payment.amount


def work_state(work: Work, as_of: date) -> str:
    if work.cancelled_at is not None and work.cancelled_at <= as_of:
        return "cancelled"
    if work.completed_at is not None and work.completed_at <= as_of:
        return "done"
    return "planned"


def describe_payment(payment: Payment, as_of: date) -> str:
    if payment.created_at > as_of:
        return "{0}: в учёте ещё нет".format(payment.payment_id)
    cash = payment_cash(payment, as_of)
    if cash > ZERO:
        return "{0}: ушло {1}".format(payment.payment_id, money(cash))
    queued = payment_queued(payment, as_of)
    if queued > ZERO:
        return "{0}: в очереди {1}".format(payment.payment_id, money(queued))
    return "{0}: в кассе 0".format(payment.payment_id)


def log_as_of(
    contracts: list[Contract],
    works: list[Work],
    payments: list[Payment],
    as_of: date,
) -> None:
    logger.info("Состояние на %s", as_of.isoformat())
    for contract in contracts:
        own_payments = [
            payment for payment in payments if payment.contract_id == contract.contract_id
        ]
        cash = sum((payment_cash(payment, as_of) for payment in own_payments), ZERO)
        queued = sum((payment_queued(payment, as_of) for payment in own_payments), ZERO)
        logger.info(
            "Касса %s на %s: %s; очередь: %s",
            contract.contract_id,
            as_of.isoformat(),
            money(cash),
            money(queued),
        )
        logger.info(
            "Платежи %s: %s",
            contract.contract_id,
            "; ".join(describe_payment(payment, as_of) for payment in own_payments),
        )
        own_works = [work for work in works if work.contract_id == contract.contract_id]
        logger.info(
            "Работы %s: %s",
            contract.contract_id,
            "; ".join(
                "{0}: {1}".format(work.work_id, STATE_LABEL[work_state(work, as_of)])
                for work in own_works
            ),
        )


def work_remaining(row: WorkAllocation) -> Decimal:
    if row.state == "cancelled":
        return ZERO
    return row.work.price - row.paid


def live_amount(item: ContractAllocation) -> Decimal:
    return sum((row.work.price for row in item.works if row.state != "cancelled"), ZERO)


def accepted_amount(item: ContractAllocation) -> Decimal:
    return sum((row.work.price for row in item.works if row.state == "done"), ZERO)


def allocated_amount(item: ContractAllocation) -> Decimal:
    return sum((row.paid for row in item.works), ZERO)


def contract_remaining(item: ContractAllocation) -> Decimal:
    return live_amount(item) - item.cash


def acceptance_key(work: Work) -> tuple[date, str]:
    if work.completed_at is None:
        raise PreflightError(
            "Работа {0} числится принятой без completed_at".format(work.work_id)
        )
    return (work.completed_at, work.work_id)


def allocate_contract(
    contract: Contract,
    works: list[Work],
    payments: list[Payment],
    as_of: date,
) -> ContractAllocation:
    own_works = [work for work in works if work.contract_id == contract.contract_id]
    own_payments = [payment for payment in payments if payment.contract_id == contract.contract_id]
    state = {work.work_id: work_state(work, as_of) for work in own_works}
    paid = {work.work_id: ZERO for work in own_works}
    overpay = {work.work_id: ZERO for work in own_works}

    for payment in own_payments:
        amount = payment_cash(payment, as_of)
        if amount == ZERO or payment.work_id is None:
            continue
        room = ZERO
        target = next(
            (work for work in own_works if work.work_id == payment.work_id),
            None,
        )
        if target is None:
            raise PreflightError(
                "Платёж {0} ссылается на работу {1} не из договора {2}".format(
                    payment.payment_id, payment.work_id, contract.contract_id
                )
            )
        if state[target.work_id] != "cancelled":
            room = target.price - paid[target.work_id]
        if room < ZERO:
            room = ZERO
        take = min(room, amount)
        paid[target.work_id] += take
        overpay[target.work_id] += amount - take

    pool = sum(
        (
            payment_cash(payment, as_of)
            for payment in own_payments
            if payment.work_id is None
        ),
        ZERO,
    )
    accepted = sorted(
        (work for work in own_works if state[work.work_id] == "done"),
        key=acceptance_key,
    )
    for work in accepted:
        room = work.price - paid[work.work_id]
        if room < ZERO:
            room = ZERO
        take = min(room, pool)
        paid[work.work_id] += take
        pool -= take

    rows = tuple(
        WorkAllocation(
            work=work,
            state=state[work.work_id],
            paid=paid[work.work_id],
            overpay=overpay[work.work_id],
        )
        for work in own_works
    )
    cash = sum((payment_cash(payment, as_of) for payment in own_payments), ZERO)
    queued = sum((payment_queued(payment, as_of) for payment in own_payments), ZERO)
    covered = sum((row.paid + row.overpay for row in rows), pool)
    if covered != cash:
        raise PreflightError(
            "Зачёт договора {0} ({1}) не сходится с кассой {2}".format(
                contract.contract_id, money(covered), money(cash)
            )
        )
    return ContractAllocation(
        contract=contract,
        works=rows,
        cash=cash,
        queued=queued,
        free_advance=pool,
    )


def allocate(
    contracts: list[Contract],
    works: list[Work],
    payments: list[Payment],
    as_of: date,
) -> list[ContractAllocation]:
    return [
        allocate_contract(contract, works, payments, as_of) for contract in contracts
    ]


def log_allocation(rows: list[ContractAllocation]) -> None:
    for item in rows:
        parts = []
        for row in item.works:
            text = "{0}: выплачено {1}, осталось {2}".format(
                row.work.work_id, money(row.paid), money(work_remaining(row))
            )
            if row.overpay > ZERO:
                text += ", переплата {0}".format(money(row.overpay))
            parts.append(text)
        logger.info(
            "Зачёт %s: %s; свободный аванс %s",
            item.contract.contract_id,
            "; ".join(parts),
            money(item.free_advance),
        )


WORK_HEADER = (
    "дата",
    "договор",
    "подрядчик",
    "работа",
    "название",
    "состояние",
    "цена",
    "выплачено",
    "осталось",
    "переплата",
)
CONTRACT_HEADER = (
    "дата",
    "договор",
    "подрядчик",
    "сумма договора",
    "живой объём",
    "принято актами",
    "ушло денег",
    "разнесено по работам",
    "свободный аванс",
    "остаток",
    "в очереди",
)


def work_rows(as_of: date, items: list[ContractAllocation]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in items:
        for row in item.works:
            rows.append(
                [
                    as_of.isoformat(),
                    csv_text(item.contract.contract_id),
                    csv_text(item.contract.contractor),
                    csv_text(row.work.work_id),
                    csv_text(row.work.title),
                    csv_text(STATE_LABEL[row.state]),
                    money(row.work.price),
                    money(row.paid),
                    money(work_remaining(row)),
                    money(row.overpay),
                ]
            )
    return rows


def contract_rows(as_of: date, items: list[ContractAllocation]) -> list[list[str]]:
    rows: list[list[str]] = []
    for item in items:
        rows.append(
            [
                as_of.isoformat(),
                csv_text(item.contract.contract_id),
                csv_text(item.contract.contractor),
                money(item.contract.amount),
                money(live_amount(item)),
                money(accepted_amount(item)),
                money(item.cash),
                money(allocated_amount(item)),
                money(item.free_advance),
                money(contract_remaining(item)),
                money(item.queued),
            ]
        )
    return rows


def publish_csv(path: Path, header: tuple[str, ...], rows: list[list[str]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8-sig",
            newline="",
            dir=path.parent,
            prefix=".{0}.".format(path.name),
            delete=False,
        )
    except OSError as exc:
        raise PreflightError(
            "Не удалось подготовить {0}: {1}".format(path, exc.strerror or exc)
        ) from exc
    tmp_path = Path(handle.name)
    published = False
    try:
        with handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(header)
            writer.writerows(rows)
        os.replace(tmp_path, path)
        published = True
    except OSError as exc:
        raise PreflightError(
            "Не удалось записать {0}: {1}".format(path, exc.strerror or exc)
        ) from exc
    finally:
        if not published and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                logger.warning("Не удалился временный файл %s", for_log(tmp_path))
    if not path.is_file() or path.stat().st_size == 0:
        raise PreflightError("Файл отчёта пустой: {0}".format(path))
    logger.info("Записан %s, строк данных %s", for_log(path), len(rows))


def write_report(out_dir: Path, as_of: date, items: list[ContractAllocation]) -> None:
    publish_csv(out_dir / "works.csv", WORK_HEADER, work_rows(as_of, items))
    publish_csv(out_dir / "contracts.csv", CONTRACT_HEADER, contract_rows(as_of, items))


def log_loaded(
    contracts: list[Contract], works: list[Work], payments: list[Payment]
) -> None:
    logger.info(
        "Договоров: %s, работ: %s, платежей: %s",
        len(contracts),
        len(works),
        len(payments),
    )
    for contract in contracts:
        logger.info(
            "Договор %s, подрядчик %s, сумма %s",
            contract.contract_id,
            for_log(contract.contractor),
            contract.amount,
        )
    unlinked = [payment.payment_id for payment in payments if payment.work_id is None]
    logger.info(
        "Платежей без работы: %s (%s)",
        len(unlinked),
        ", ".join(unlinked) if unlinked else "нет",
    )
    for payment in payments:
        if "," in payment.purpose:
            logger.info(
                "Платёж %s, назначение с запятой: %s",
                payment.payment_id,
                for_log(payment.purpose),
            )


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logger.info(
        "Старт. Сетевых запросов нет, скрипт работает только с локальными файлами."
    )
    logger.info("Дата отчёта: %s", args.as_of.isoformat())
    logger.info("Аргумент --data: %s", for_log(args.data))
    logger.info("Аргумент --out: %s", for_log(args.out))

    data_dir = resolve_existing_dir(args.data, "Папка данных")
    for name in REQUIRED_FILES:
        check_source_file(data_dir, name)
    out_dir = check_output_dir(args.out, data_dir)

    contracts = load_contracts(data_dir / "contracts.csv")
    works = load_works(data_dir / "works.csv")
    payments = load_payments(data_dir / "payments.csv")
    log_loaded(contracts, works, payments)
    check_ledger(contracts, works, payments)
    log_as_of(contracts, works, payments, args.as_of)
    allocation = allocate(contracts, works, payments, args.as_of)
    log_allocation(allocation)
    write_report(out_dir, args.as_of, allocation)

    logger.info("Отчёт на %s записан в %s", args.as_of.isoformat(), for_log(out_dir))
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    try:
        return run(argv)
    except PreflightError as exc:
        logger.error("%s", for_log(exc, limit=1000))
        return 1
    except KeyboardInterrupt:
        logger.warning("Остановлено пользователем")
        return 130
    except BrokenPipeError:
        return 1
    except Exception:
        logger.exception("Непредвиденная ошибка")
        return 1


if __name__ == "__main__":
    sys.exit(main())
