"""Проверки отчёта по авансам.

Файловые случаи используют временную папку pytest. Сети и базы в скрипте нет,
поэтому мокать там нечего: платёж и зачёт проверяются прямым вызовом.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import report


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
AS_OF = date(2026, 11, 10)


def write_csv(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def minimal_ledger(directory: Path, contract_row: str, work_row: str, payment_row: str) -> None:
    write_csv(
        directory,
        "contracts.csv",
        "contract_id,contractor,amount,signed_at\n" + contract_row + "\n",
    )
    write_csv(
        directory,
        "works.csv",
        "work_id,contract_id,title,price,status,completed_at,cancelled_at\n" + work_row + "\n",
    )
    write_csv(
        directory,
        "payments.csv",
        "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
        + payment_row
        + "\n",
    )


def read_report(directory: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    import csv

    with (directory / "contracts.csv").open(encoding="utf-8-sig", newline="") as handle:
        contracts = list(csv.DictReader(handle))
    with (directory / "works.csv").open(encoding="utf-8-sig", newline="") as handle:
        works = list(csv.DictReader(handle))
    return contracts, works


def test_happy_path_on_check_date(tmp_path: Path) -> None:
    code = report.main(["--as-of", "10.11.2026", "--data", str(DATA), "--out", str(tmp_path)])
    assert code == 0
    contracts, works = read_report(tmp_path)
    by_contract = {row["договор"]: row for row in contracts}
    by_work = {row["работа"]: row for row in works}

    assert by_contract["D-1"]["остаток"] == "185000.00"
    assert by_contract["D-1"]["свободный аванс"] == "15000.00"
    assert by_contract["D-1"]["ушло денег"] == "315000.00"
    assert by_contract["D-1"]["в очереди"] == "150000.00"
    assert by_contract["D-1"]["живой объём"] == "500000.00"
    assert by_contract["D-2"]["остаток"] == "15000.00"
    assert by_contract["D-2"]["свободный аванс"] == "25000.00"
    assert by_contract["D-2"]["подрядчик"] == "Иван Петров, дизайнер"
    assert by_work["W-14"]["выплачено"] == "100000.00"
    assert by_work["W-13"]["состояние"] == "отменена"
    assert by_work["W-13"]["осталось"] == "0.00"
    assert by_work["W-23"]["состояние"] == "в плане"
    assert by_work["W-23"]["выплачено"] == "0.00"
    assert Decimal(by_contract["D-2"]["ушло денег"]) == Decimal("75000.00")


def test_refund_still_in_cash_before_return_date() -> None:
    payments = report.load_payments(DATA / "payments.csv")
    mistaken = next(payment for payment in payments if payment.payment_id == "P-2")
    assert report.payment_cash(mistaken, date(2026, 9, 5)) == Decimal("50000.00")
    assert report.payment_cash(mistaken, AS_OF) == Decimal("0.00")


def test_future_payment_and_acceptance() -> None:
    works = report.load_works(DATA / "works.csv")
    payments = report.load_payments(DATA / "payments.csv")
    packaging = next(work for work in works if work.work_id == "W-23")
    last = next(payment for payment in payments if payment.payment_id == "P-8")
    assert report.work_state(packaging, AS_OF) == "planned"
    assert report.work_state(packaging, date(2026, 11, 12)) == "done"
    assert report.payment_cash(last, AS_OF) == Decimal("0.00")
    assert report.payment_queued(last, AS_OF) == Decimal("0.00")
    assert report.payment_cash(last, date(2026, 11, 12)) == Decimal("20000.00")


def test_overpay_on_later_date(tmp_path: Path) -> None:
    code = report.main(["--as-of", "2026-11-12", "--data", str(DATA), "--out", str(tmp_path)])
    assert code == 0
    contracts, works = read_report(tmp_path)
    ivan = next(row for row in contracts if row["договор"] == "D-2")
    packaging = next(row for row in works if row["работа"] == "W-23")
    assert ivan["остаток"] == "-5000.00"
    assert ivan["свободный аванс"] == "5000.00"
    assert packaging["состояние"] == "принята"
    assert packaging["выплачено"] == "40000.00"


def test_cancelled_linked_payment_becomes_overpay() -> None:
    contract = report.Contract("D-X", "x", Decimal("100000.00"), date(2026, 1, 1))
    work = report.Work("W-X", "D-X", "x", Decimal("100000.00"), "cancelled", None, date(2026, 1, 2))
    payment = report.Payment(
        "P-X",
        "D-X",
        "W-X",
        "материалы",
        Decimal("150000.00"),
        "paid",
        date(2026, 1, 1),
        date(2026, 1, 3),
        None,
    )
    item = report.allocate_contract(contract, [work], [payment], date(2026, 1, 10))
    assert item.works[0].paid == Decimal("0.00")
    assert item.works[0].overpay == Decimal("150000.00")
    assert item.free_advance == Decimal("0.00")


def test_missing_work_link_is_an_error_not_a_crash() -> None:
    contract = report.Contract("D-X", "x", Decimal("10.00"), date(2026, 1, 1))
    payment = report.Payment(
        "P-X",
        "D-X",
        "W-MISSING",
        "аванс",
        Decimal("10.00"),
        "paid",
        date(2026, 1, 1),
        date(2026, 1, 2),
        None,
    )
    with pytest.raises(report.PreflightError, match="W-MISSING"):
        report.allocate_contract(contract, [], [payment], date(2026, 1, 10))


def test_negative_amount_rejected(tmp_path: Path) -> None:
    data = tmp_path / "data"
    out = tmp_path / "out"
    minimal_ledger(
        data,
        "D-1,Студия,100.00,2026-01-01",
        "W-1,D-1,Работа,100.00,planned,,",
        "P-1,D-1,,аванс,-10.00,paid,2026-01-01,2026-01-02,",
    )
    assert report.main(["--data", str(data), "--out", str(out), "--as-of", "2026-01-10"]) == 1


def test_subkopeck_and_infinity_rejected(tmp_path: Path) -> None:
    path = tmp_path / "contracts.csv"
    path.write_text(
        "contract_id,contractor,amount,signed_at\nD-1,A,10.001,2026-01-01\n",
        encoding="utf-8",
    )
    with pytest.raises(report.PreflightError, match="копейки"):
        report.load_contracts(path)
    path.write_text(
        "contract_id,contractor,amount,signed_at\nD-1,A,Infinity,2026-01-01\n",
        encoding="utf-8",
    )
    with pytest.raises(report.PreflightError, match="конечной"):
        report.load_contracts(path)


def test_empty_file_missing_dir_and_bad_date(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "contracts.csv").write_text("", encoding="utf-8")
    (data / "works.csv").write_text("x\n", encoding="utf-8")
    (data / "payments.csv").write_text("x\n", encoding="utf-8")
    assert report.main(["--data", str(data), "--out", str(tmp_path / "out")]) == 1
    assert report.main(["--data", str(tmp_path / "no-such"), "--out", str(tmp_path / "out")]) == 1
    with pytest.raises(SystemExit) as stopped:
        report.main(["--as-of", "31.02.2026", "--data", str(DATA), "--out", str(tmp_path / "out")])
    assert stopped.value.code == 2
    with pytest.raises(SystemExit) as empty_date:
        report.main(["--as-of", " ", "--data", str(DATA), "--out", str(tmp_path / "out")])
    assert empty_date.value.code == 2


def test_shell_metacharacters_stay_inside_the_date_parser() -> None:
    with pytest.raises(SystemExit):
        report.parse_args(["--as-of", "2026-11-10; rm -rf /"])


def test_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "contracts.csv"
    path.write_text("contract_id,contractor,amount,signed_at\n", encoding="utf-8")
    path.chmod(0)
    try:
        with pytest.raises(report.PreflightError):
            report.load_contracts(path)
    finally:
        path.chmod(0o644)


def test_bom_and_quoted_comma(tmp_path: Path) -> None:
    path = tmp_path / "contracts.csv"
    path.write_text(
        'contract_id,contractor,amount,signed_at\nD-2,"Иван Петров, дизайнер",90000.00,2026-09-20\n',
        encoding="utf-8-sig",
    )
    loaded = report.load_contracts(path)
    assert loaded[0].contractor == "Иван Петров, дизайнер"
    assert loaded[0].amount == Decimal("90000.00")


def test_formula_text_is_quoted_money_is_not(tmp_path: Path) -> None:
    data = tmp_path / "data"
    out = tmp_path / "out"
    minimal_ledger(
        data,
        "D-1,=evil,100.00,2026-01-01",
        "W-1,D-1,=1+1,100.00,done,2026-01-02,",
        "P-1,D-1,,аванс,150.00,paid,2026-01-01,2026-01-01,",
    )
    assert report.main(["--data", str(data), "--out", str(out), "--as-of", "2026-01-10"]) == 0
    contracts, works = read_report(out)
    assert contracts[0]["подрядчик"] == "'=evil"
    assert works[0]["название"] == "'=1+1"
    assert contracts[0]["остаток"].startswith("-")
    assert not contracts[0]["остаток"].startswith("'")


def test_many_works_finish_and_balance(tmp_path: Path) -> None:
    data = tmp_path / "data"
    lines = ["work_id,contract_id,title,price,status,completed_at,cancelled_at"]
    for index in range(1, 2001):
        lines.append("W-{0},D-1,Работа {0},1.00,done,2026-01-02,".format(index))
    write_csv(data, "works.csv", "\n".join(lines) + "\n")
    write_csv(
        data,
        "contracts.csv",
        "contract_id,contractor,amount,signed_at\nD-1,Студия,2000.00,2026-01-01\n",
    )
    write_csv(
        data,
        "payments.csv",
        "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
        "P-1,D-1,,аванс,10.50,paid,2026-01-01,2026-01-01,\n",
    )
    assert report.main(["--data", str(data), "--out", str(tmp_path / "out"), "--as-of", "2026-01-10"]) == 0
    contracts, works = read_report(tmp_path / "out")
    assert len(works) == 2000
    assert contracts[0]["ушло денег"] == "10.50"
    paid = sum(Decimal(row["выплачено"]) for row in works)
    assert paid + Decimal(contracts[0]["свободный аванс"]) == Decimal("10.50")


def test_duplicate_id_and_short_row(tmp_path: Path) -> None:
    path = tmp_path / "payments.csv"
    path.write_text(
        "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
        "P-1,D-1,,аванс,1.00,paid,2026-01-01,2026-01-01,\n"
        "P-1,D-1,,ещё,1.00,paid,2026-01-01,2026-01-01,\n",
        encoding="utf-8",
    )
    with pytest.raises(report.PreflightError, match="повторяется"):
        report.load_payments(path)
    path.write_text(
        "payment_id,contract_id,work_id,purpose,amount,status,created_at,paid_at,refunded_at\n"
        "P-1,D-1,,аванс,1.00,paid,2026-01-01\n",
        encoding="utf-8",
    )
    with pytest.raises(report.PreflightError, match="не хватает колонки"):
        report.load_payments(path)


def test_out_dir_cannot_overwrite_source(tmp_path: Path) -> None:
    data = tmp_path / "data"
    minimal_ledger(
        data,
        "D-1,Студия,100.00,2026-01-01",
        "W-1,D-1,Работа,100.00,planned,,",
        "P-1,D-1,,аванс,10.00,paid,2026-01-01,2026-01-02,",
    )
    with pytest.raises(report.PreflightError, match="совпадает"):
        report.check_output_dir(data, data.resolve())
    assert report.main(["--data", str(data), "--out", str(data), "--as-of", "2026-01-10"]) == 1
