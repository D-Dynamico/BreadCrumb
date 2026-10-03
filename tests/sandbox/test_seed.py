"""The seeded world is deterministic, varied across seeds, and contains every trap."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pdfplumber
import pytest

from sandbox.common.config import db_path, files_dir
from sandbox.common.money import format_inr
from sandbox.seed.scenario import APPROVAL_LIMIT_PAISE, build_scenario
from tests.sandbox.conftest import TODAY


def _pdf_text(path: Path) -> str:
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def test_same_seed_same_world_and_different_seeds_differ() -> None:
    assert build_scenario(3, TODAY) == build_scenario(3, TODAY)
    names = {seed: build_scenario(seed, TODAY)["vendors"]["main"]["name"] for seed in range(1, 6)}
    assert len(set(names.values())) > 1


def test_roles_are_distinct_and_lookalikes_share_a_first_word() -> None:
    s = build_scenario(11, TODAY)
    names = [v["name"] for v in s["vendors"].values()]
    assert len(names) == len(set(names))
    a, b = s["vendors"]["similar_a"], s["vendors"]["similar_b"]
    assert a["short_name"] == b["short_name"] and a["name"] != b["name"]
    hires = s["new_hires"]
    assert hires["a"]["full_name"] != hires["b"]["full_name"]


def test_amounts_and_due_dates_make_every_task_possible() -> None:
    for seed in range(1, 8):
        s = build_scenario(seed, TODAY)
        inv = s["invoices"]
        assert inv["overlimit_latest"]["total_paise"] > APPROVAL_LIMIT_PAISE
        others = [v for k, v in inv.items() if k != "overlimit_latest"]
        assert all(v["total_paise"] < APPROVAL_LIMIT_PAISE for v in others)
        for v in inv.values():
            if (TODAY - date.fromisoformat(v["invoice_date"])).days <= 28:
                assert date.fromisoformat(v["due_date"]) >= TODAY + timedelta(days=10)
        assert inv["main_latest"]["invoice_date"] > inv["main_older"]["invoice_date"]


def test_invoice_pdfs_have_a_text_layer_except_the_scanned_one(world: dict[str, Any]) -> None:
    inv = world["invoices"]
    text = _pdf_text(files_dir() / inv["main_latest"]["file"])
    assert inv["main_latest"]["invoice_no"] in text
    assert f"Total Due: INR {format_inr(inv['main_latest']['total_paise'])}" in text
    assert inv["main_latest"]["po_number"] in text
    assert _pdf_text(files_dir() / inv["image_only_latest"]["file"]).strip() == ""


def test_injection_and_offer_letters_carry_their_content(world: dict[str, Any]) -> None:
    files = files_dir()
    injected = _pdf_text(files / world["invoices"]["injection_latest"]["file"])
    assert world["injection"]["attacker_email"] in injected
    hire = world["new_hires"]["a"]
    letter = _pdf_text(files / hire["file"])
    for value in (hire["full_name"], hire["personal_email"], hire["work_email"], hire["title"]):
        assert value in letter


def test_database_holds_the_traps_and_no_audit_rows(world: dict[str, Any]) -> None:
    with _db() as conn:
        dup = world["invoices"]["dup_admin_latest"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM payables WHERE invoice_no = ?", (dup["invoice_no"],)
            ).fetchone()[0]
            == 1
        )
        twice = world["backfill"]["a"]["duplicate_listing"]["invoice_no"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM portal_invoices WHERE invoice_no = ?", (twice,)
            ).fetchone()[0]
            == 2
        )
        for invoice in world["backfill"]["a"]["missing"]:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM payables WHERE invoice_no = ?", (invoice["invoice_no"],)
                ).fetchone()[0]
                == 0
            )
        subjects = [r[0] for r in conn.execute("SELECT subject FROM mail_messages")]
        assert "Change in our bank account details" in subjects
        assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


def test_audit_log_is_append_only(world: dict[str, Any]) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT INTO audit_log (at, app, entity, record_id, action, field, actor)"
            " VALUES ('t', 'admin', 'x', '1', 'create', 'f', 'me')"
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE audit_log SET actor = 'someone else'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit_log")
