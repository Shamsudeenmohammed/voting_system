"""Bulk CSV/Excel voter import with validation, duplicate detection and an
error report. Designed for 100k+ rows: streams the upload, uses
``bulk_create`` batches, and never loads credentials from the file.

Required columns: voter_number, full_name
Optional: email, phone, unit (name), unit_type
"""
from __future__ import annotations

import csv
import io

from django.db import IntegrityError, transaction
from django.utils.text import slugify

from apps.orgs.models import Unit
from .models import Voter, VoterImportBatch

REQUIRED = ("voter_number", "full_name")
OPTIONAL = ("email", "phone", "unit", "unit_type")
BATCH_SIZE = 1000
MAX_ERRORS = 500


def _rows_from_upload(upload):
    name = (upload.name or "").lower()
    if name.endswith(".csv") or name.endswith(".txt"):
        raw = upload.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
        reader = csv.DictReader(io.StringIO(text))
        for i, row in enumerate(reader, start=2):  # header = row 1
            yield i, { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
    elif name.endswith(".xlsx") or name.endswith(".xls"):
        import openpyxl

        wb = openpyxl.load_workbook(upload, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            return
        keys = [str(h).strip().lower() if h else "" for h in header]
        for i, values in enumerate(rows, start=2):
            yield i, {
                k: ("" if v is None else str(v).strip())
                for k, v in zip(keys, values)
            }
    else:
        raise ValueError("Unsupported file type. Use CSV or XLSX.")


def run_import(batch: VoterImportBatch, upload) -> VoterImportBatch:
    """Process an import synchronously (Celery task wraps this for large files)."""
    batch.status = VoterImportBatch.Status.PROCESSING
    batch.save(update_fields=["status"])

    errors: list[dict] = []
    seen_in_file: set[str] = set()
    total = imported = rejected = duplicates = 0
    org = batch.organization

    unit_cache: dict[tuple[str, str], Unit] = {}

    def get_unit(name: str, unit_type: str) -> Unit | None:
        if not name:
            return None
        key = (name.lower(), unit_type)
        if key not in unit_cache:
            unit, _ = Unit.objects.get_or_create(
                organization=org,
                name=name,
                unit_type=unit_type or Unit.UnitType.OTHER,
            )
            unit_cache[key] = unit
        return unit_cache[key]

    try:
        iterator = _rows_from_upload(upload)
    except ValueError as exc:
        batch.status = VoterImportBatch.Status.FAILED
        batch.error_report = [{"row": 0, "error": str(exc)}]
        batch.save(update_fields=["status", "error_report"])
        return batch

    to_create: list[Voter] = []
    existing: set[str] = set(
        Voter.objects.filter(organization=org).values_list("voter_number", flat=True)
    )

    def add_error(row_no, msg):
        rejected += 1
        if len(errors) < MAX_ERRORS:
            errors.append({"row": row_no, "error": msg})

    for row_no, row in iterator:
        total += 1
        number = (row.get("voter_number") or "").strip()
        name = (row.get("full_name") or "").strip()
        if not number or not name:
            add_error(row_no, "Missing voter_number or full_name")
            continue
        if number.lower() in seen_in_file:
            duplicates += 1
            add_error(row_no, f"Duplicate voter_number in file: {number}")
            continue
        seen_in_file.add(number.lower())
        if number in existing:
            duplicates += 1
            add_error(row_no, f"Already registered: {number}")
            continue
        unit = get_unit(row.get("unit", ""), row.get("unit_type", ""))
        voter = Voter(
            organization=org,
            voter_number=number,
            full_name=name,
            email=row.get("email", ""),
            phone=row.get("phone", ""),
            unit=unit,
        )
        try:
            voter.full_clean(exclude=["units"])
        except Exception as exc:  # ValidationError with messages
            add_error(row_no, str(exc).replace("\n", " ")[:300])
            continue
        to_create.append(voter)
        existing.add(number)
        if len(to_create) >= BATCH_SIZE:
            _flush(to_create, errors_ref=errors)
            imported += len(to_create)
            to_create = []

    if to_create:
        _flush(to_create, errors_ref=errors)
        imported += len(to_create)

    batch.total_rows = total
    batch.imported_rows = imported
    batch.rejected_rows = rejected
    batch.duplicate_rows = duplicates
    batch.error_report = errors
    batch.status = VoterImportBatch.Status.COMPLETED
    batch.save(
        update_fields=[
            "total_rows",
            "imported_rows",
            "rejected_rows",
            "duplicate_rows",
            "error_report",
            "status",
            "updated_at",
        ]
    )
    return batch


def _flush(to_create: list[Voter], errors_ref: list[dict]):
    try:
        with transaction.atomic():
            Voter.objects.bulk_create(to_create, batch_size=BATCH_SIZE)
    except IntegrityError:
        # Fall back to per-row creation to attribute errors precisely.
        for voter in to_create:
            try:
                with transaction.atomic():
                    voter.save()
            except IntegrityError:
                if len(errors_ref) < MAX_ERRORS:
                    errors_ref.append(
                        {"row": "-", "error": f"Duplicate: {voter.voter_number}"}
                    )
    to_create.clear()
