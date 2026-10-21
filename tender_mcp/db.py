"""
tender_mcp/db.py  –  SQLite persistence layer
==============================================
No ORM; raw sqlite3. All writes go through upsert_tender().
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

DB_PATH = Path(__file__).parent.parent / "tenders.db"

_local = threading.local()


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    """Return a thread-local connection with WAL mode and row_factory."""
    if not hasattr(_local, "conn") or _local.conn is None:
        conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        _local.conn = conn
    yield _local.conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

TABLES_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    id                TEXT PRIMARY KEY,           -- "<source>:<raw_id>"
    source            TEXT NOT NULL,              -- 'cppp' | 'gem' | 'hmel'
    title             TEXT,
    organisation      TEXT,
    category          TEXT,
    published_date    TEXT,                       -- ISO-8601 or source string
    closing_date      TEXT,
    opening_date      TEXT,
    emd_value         TEXT,
    tender_value      TEXT,
    link              TEXT,
    status            TEXT DEFAULT 'new',         -- 'new' | 'interested' | 'bidding' | 'skipped' | 'submitted'
    owner             TEXT DEFAULT '',
    notes             TEXT DEFAULT '',
    status_updated_at TEXT,
    relevance_score   INTEGER DEFAULT 0,
    matched_terms     TEXT DEFAULT '',
    buyer_is_oil_gas  INTEGER DEFAULT 0,          -- 1 if org matches oil_gas_buyers
    tier              TEXT DEFAULT 'C',           -- A/B/C
    tender_kind       TEXT DEFAULT 'unknown',     -- service/product/mixed/unknown
    is_consumable     INTEGER DEFAULT 0,          -- 1 if text matches keywords.json consumables (no score effect)
    match_basis       TEXT DEFAULT 'item',        -- 'item' or 'location_only'
    raw_json          TEXT,                       -- full source record as JSON
    first_seen_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    date_check        TEXT DEFAULT 'not_checked',
    similar_category  TEXT,
    category_noise_reason TEXT,
    buyer_match       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    source      TEXT NOT NULL,
    fetched_count INTEGER DEFAULT 0,
    new_count   INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    error_msg   TEXT
);

CREATE TABLE IF NOT EXISTS closing_date_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id   TEXT NOT NULL,
    old_value   TEXT,
    new_value   TEXT,
    seen_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS status_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tender_id   TEXT NOT NULL,
    old_status  TEXT,
    new_status  TEXT NOT NULL,
    owner       TEXT,
    changed_at  TEXT NOT NULL,
    FOREIGN KEY (tender_id) REFERENCES tenders(id)
);

CREATE TABLE IF NOT EXISTS digest_state (
    key         TEXT PRIMARY KEY,
    value       TEXT
);
"""

# Kept for backward compatibility
SCHEMA = TABLES_SCHEMA


def init_db() -> None:
    """Create tables / indexes if they don't exist yet and run migrations."""
    import json
    from tender_mcp.dates import to_iso8601

    with get_conn() as conn:
        conn.executescript(TABLES_SCHEMA)

        # Ensure tenders columns exist
        cursor = conn.execute("PRAGMA table_info(tenders)")
        columns = [row["name"] for row in cursor.fetchall()]

        # Rename possible_latest_end_utc -> auto_extension_limit_utc if present
        if "possible_latest_end_utc" in columns and "auto_extension_limit_utc" not in columns:
            try:
                conn.execute("ALTER TABLE tenders RENAME COLUMN possible_latest_end_utc TO auto_extension_limit_utc")
                cursor = conn.execute("PRAGMA table_info(tenders)")
                columns = [row["name"] for row in cursor.fetchall()]
            except Exception:
                conn.execute("ALTER TABLE tenders ADD COLUMN auto_extension_limit_utc TEXT")
                conn.execute("UPDATE tenders SET auto_extension_limit_utc = possible_latest_end_utc")
                cursor = conn.execute("PRAGMA table_info(tenders)")
                columns = [row["name"] for row in cursor.fetchall()]
        for col_name, col_def in [
            ("relevance_score", "INTEGER DEFAULT 0"),
            ("matched_terms", "TEXT DEFAULT ''"),
            ("buyer_is_oil_gas", "INTEGER DEFAULT 0"),
            ("tier", "TEXT DEFAULT 'C'"),
            ("tender_kind", "TEXT DEFAULT 'unknown'"),
            ("is_consumable", "INTEGER DEFAULT 0"),
            ("match_basis", "TEXT DEFAULT 'item'"),
            ("first_seen_at", "TEXT DEFAULT ''"),
            ("last_seen_at", "TEXT DEFAULT ''"),
            ("bid_number", "TEXT"),
            ("bid_end_datetime", "TEXT"),
            ("bid_opening_datetime", "TEXT"),
            ("bid_offer_validity_days", "INTEGER"),
            ("ministry", "TEXT"),
            ("department", "TEXT"),
            ("office_name", "TEXT"),
            ("item_category", "TEXT"),
            ("total_quantity", "INTEGER"),
            ("estimated_bid_value_inr", "REAL"),
            ("emd_required", "INTEGER"),
            ("emd_amount_inr", "REAL"),
            ("epbg_required", "INTEGER"),
            ("epbg_percent", "REAL"),
            ("epbg_months", "INTEGER"),
            ("mse_reserved", "INTEGER"),
            ("mse_purchase_preference", "TEXT"),
            ("mse_experience_turnover_relaxation", "INTEGER"),
            ("startup_relaxation", "INTEGER"),
            ("mii_class_restriction", "TEXT"),
            ("mii_preference", "TEXT"),
            ("bis_required", "INTEGER"),
            ("min_turnover_lakh", "REAL"),
            ("years_experience_required", "REAL"),
            ("oem_authorization_required", "INTEGER"),
            ("bid_type", "TEXT"),
            ("reverse_auction", "INTEGER"),
            ("short_duration_bid", "INTEGER"),
            ("inspection_required", "INTEGER"),
            ("contract_period", "TEXT"),
            ("delivery_days", "INTEGER"),
            ("consignee_city", "TEXT"),
            ("bid_splitting", "TEXT"),
            ("pdf_fetched_at", "TEXT"),
            ("pdf_parse_status", "TEXT DEFAULT 'none'"),
            ("details_needed", "INTEGER DEFAULT 0"),
            ("eligibility", "TEXT DEFAULT 'unknown'"),
            ("eligibility_reasons", "TEXT DEFAULT '[]'"),
            ("auto_extend_min_bids", "INTEGER"),
            ("auto_extend_days", "INTEGER"),
            ("auto_extend_max_count", "INTEGER"),
            ("original_end_utc", "TEXT"),
            ("auto_extension_limit_utc", "TEXT"),
            ("possible_latest_end_utc", "TEXT"),
            ("was_extended", "INTEGER DEFAULT 0"),
            ("is_extended", "INTEGER DEFAULT 0"),
            ("extension_days_observed", "REAL"),
            ("date_check", "TEXT DEFAULT 'not_checked'"),
            ("extensions_seen", "INTEGER DEFAULT 0"),  # counts upward changes seen by refresh since tracking began, not total extensions
            ("past_performance_percent", "REAL"),
            ("oem_average_turnover_lakh", "REAL"),
            ("flags", "TEXT DEFAULT '[]'"),
            ("emd_amount_inr_min", "REAL"),
            ("emd_amount_inr_max", "REAL"),
            ("emd_schedules", "TEXT DEFAULT '[]'"),
            ("corrigendum_date", "TEXT"),
            ("owner", "TEXT DEFAULT ''"),
            ("notes", "TEXT DEFAULT ''"),
            ("status_updated_at", "TEXT"),
            ("buyer_overrides_gem_fields", "INTEGER DEFAULT 0"),
            ("floor_price_inr_min", "REAL"),
            ("floor_price_inr_max", "REAL"),
            ("floor_price_schedules", "TEXT DEFAULT '[]'"),
            ("pbg_months_per_lot", "TEXT DEFAULT '[]'"),
            ("similar_category", "TEXT"),
            ("category_noise_reason", "TEXT"),
            ("buyer_match", "INTEGER DEFAULT 0"),
        ]:
            if col_name not in columns:
                try:
                    conn.execute(f"ALTER TABLE tenders ADD COLUMN {col_name} {col_def}")
                except sqlite3.OperationalError:
                    pass

        # Migrate existing 'extended' date_check rows to 'extended_auto'
        conn.execute("UPDATE tenders SET date_check = 'extended_auto' WHERE date_check = 'extended'")
        # Migrate location_only tenders out of Tier A to Tier B
        conn.execute("UPDATE tenders SET tier = 'B' WHERE match_basis = 'location_only' AND tier = 'A'")
        # Migrate tenders with no PDF data (pdf_parse_status = 'none') and CPPP rows to 'not_checked'
        conn.execute("UPDATE tenders SET date_check = 'not_checked' WHERE pdf_parse_status = 'none' OR source = 'cppp'")
        # Migrate was_extended for existing tenders with extensions recorded
        conn.execute("""
            UPDATE tenders SET was_extended = 1
            WHERE (extensions_seen > 0 OR is_extended = 1 OR id IN (
                SELECT tender_id FROM closing_date_history WHERE new_value > old_value
            )) AND (was_extended IS NULL OR was_extended = 0)
        """)
        # Migrate legacy statuses: set details_needed flag and migrate active/details needed to 'new'
        conn.execute("UPDATE tenders SET details_needed = 1 WHERE title LIKE '[Items not listed]%' OR status = 'details needed'")
        conn.execute("UPDATE tenders SET status = 'new' WHERE status IN ('active', 'details needed')")

        # Ensure refresh_log columns exist
        cursor = conn.execute("PRAGMA table_info(refresh_log)")
        log_columns = [row["name"] for row in cursor.fetchall()]
        if "fetched_count" not in log_columns:
            try:
                conn.execute("ALTER TABLE refresh_log ADD COLUMN fetched_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass

        # Create indexes
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_tenders_source        ON tenders(source)",
            "CREATE INDEX IF NOT EXISTS idx_tenders_closing_date  ON tenders(closing_date)",
            "CREATE INDEX IF NOT EXISTS idx_tenders_status        ON tenders(status)",
            "CREATE INDEX IF NOT EXISTS idx_tenders_first_seen    ON tenders(first_seen_at)",
            "CREATE INDEX IF NOT EXISTS idx_tenders_relevance     ON tenders(relevance_score)",
        ]
        for idx in indexes:
            try:
                conn.execute(idx)
            except sqlite3.OperationalError:
                pass

        # Migration 3: migrate dates to ISO 8601 for all existing rows
        all_tenders = conn.execute(
            "SELECT id, published_date, closing_date, opening_date FROM tenders"
        ).fetchall()
        for row in all_tenders:
            pub = to_iso8601(row["published_date"]) if row["published_date"] else ""
            close = to_iso8601(row["closing_date"]) if row["closing_date"] else ""
            opn = to_iso8601(row["opening_date"]) if row["opening_date"] else ""
            if pub != row["published_date"] or close != row["closing_date"] or opn != row["opening_date"]:
                conn.execute(
                    "UPDATE tenders SET published_date = ?, closing_date = ?, opening_date = ? WHERE id = ?",
                    (pub, close, opn, row["id"]),
                )

        # Migration 4: migrate CPPP composite IDs from URL token to real tender ref/id
        cppp_rows = conn.execute(
            "SELECT id, title, raw_json FROM tenders WHERE source = 'cppp'"
        ).fetchall()
        for r in cppp_rows:
            old_id = r["id"]
            if "=" in old_id or "A13h1" in old_id:
                raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
                title_full = raw.get("title_full", "")
                anchor_text = raw.get("anchor_text", "") or r["title"] or ""
                remainder = (
                    title_full[len(anchor_text):].strip().lstrip("/")
                    if title_full.startswith(anchor_text)
                    else title_full
                )
                ref_no, _, tender_id = remainder.rpartition("/")
                real_id = (
                    tender_id.strip()
                    or ref_no.strip()
                    or str(raw.get("tender_id", "")).strip()
                    or str(raw.get("ref_no", "")).strip()
                )
                if real_id:
                    new_id = f"cppp:{real_id}"
                    if new_id != old_id:
                        # Check if new_id already exists
                        existing = conn.execute(
                            "SELECT id FROM tenders WHERE id = ?", (new_id,)
                        ).fetchone()
                        if existing:
                            conn.execute("DELETE FROM tenders WHERE id = ?", (old_id,))
                        else:
                            conn.execute(
                                "UPDATE tenders SET id = ? WHERE id = ?",
                                (new_id, old_id),
                            )

        # Migration 5: normalize titles that have synthesized department text or raw GEM IDs
        title_rows = conn.execute("SELECT id, title, category FROM tenders").fetchall()
        for tr in title_rows:
            tid = tr["id"]
            title = tr["title"] or ""
            cat = tr["category"] or ""
            bid_no = tid.replace("gem:", "").replace("_", "/") if tid.startswith("gem:") else ""
            if "Procurement of Technical Spares" in title or "Procurement (" in title:
                new_title = f"[Items not listed] {bid_no}" if bid_no else "[Items not listed]"
                conn.execute(
                    "UPDATE tenders SET title = ?, category = '' WHERE id = ?",
                    (new_title, tid),
                )
            elif title.startswith("GEM/") or title.startswith("GEM_"):
                new_title = f"[Items not listed] {title}"
                conn.execute("UPDATE tenders SET title = ? WHERE id = ?", (new_title, tid))

        # Migration 6: fix legacy naive IST dates that had Z appended without timezone shift
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY)")
        has_ist_migration = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = 'fix_ist_timezone'"
        ).fetchone()
        cursor = conn.execute("PRAGMA table_info(tenders)")
        curr_cols = [row["name"] for row in cursor.fetchall()]
        if not has_ist_migration and "first_seen_at" in curr_cols:
            from datetime import timedelta, datetime
            IST_OFFSET = timedelta(hours=5, minutes=30)
            legacy_rows = conn.execute(
                "SELECT id, published_date, closing_date, opening_date FROM tenders WHERE first_seen_at < '2026-10-14' AND first_seen_at != ''"
            ).fetchall()
            for r in legacy_rows:
                updates = {}
                for field in ["published_date", "closing_date", "opening_date"]:
                    val = r[field]
                    if val and val.endswith("Z"):
                        try:
                            dt = datetime.fromisoformat(val.rstrip("Z"))
                            shifted_dt = dt - IST_OFFSET
                            updates[field] = shifted_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                        except ValueError:
                            pass
                if updates:
                    set_clause = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(
                        f"UPDATE tenders SET {set_clause} WHERE id = ?",
                        (*updates.values(), r["id"]),
                    )
            conn.execute("INSERT INTO schema_migrations (version) VALUES ('fix_ist_timezone')")

        # Migration 7: rescore stored tenders, populate matched_terms, buyer_is_oil_gas, tier, tender_kind
        all_rows = conn.execute(
            "SELECT id, title, organisation, category, status, raw_json FROM tenders"
        ).fetchall()
        if all_rows:
            from tender_mcp.matcher import score_tender, is_oil_gas_buyer, compute_tier, classify_tender_kind
            for row in all_rows:
                r_dict = dict(row)
                res = score_tender(r_dict)
                score = res["score"]
                m_terms = res.get("matched_terms_str", "")
                st = r_dict.get("status") or "new"
                if res.get("suggested_status") == "details needed":
                    conn.execute("UPDATE tenders SET details_needed = 1 WHERE id = ?", (r_dict["id"],))
                if st in ("active", "details needed"):
                    st = "new"
                org = r_dict.get("organisation") or ""
                buyer_flag = 1 if is_oil_gas_buyer(org) else 0
                match_basis = res.get("match_basis") or "item"
                tier = compute_tier(score, org, match_basis=match_basis)
                kind = res.get("tender_kind") or classify_tender_kind(r_dict.get("title") or "")
                conn.execute(
                    "UPDATE tenders SET relevance_score = ?, matched_terms = ?, status = ?, buyer_is_oil_gas = ?, tier = ?, tender_kind = ?, is_consumable = ?, match_basis = ? WHERE id = ?",
                    (score, m_terms, st, buyer_flag, tier, kind, 1 if res.get("is_consumable") else 0, match_basis, r_dict["id"]),
                )

        # Migration 8: fix cppp:1 if present
        cppp1 = conn.execute("SELECT id, raw_json FROM tenders WHERE id = 'cppp:1'").fetchone()
        if cppp1 and cppp1["raw_json"]:
            raw = json.loads(cppp1["raw_json"])
            ref_no = raw.get("ref_no", "")
            tid_part = raw.get("tender_id", "")
            if ref_no:
                new_id = f"cppp:{ref_no}_{tid_part}" if tid_part else f"cppp:{ref_no}"
                conn.execute("UPDATE tenders SET id = ? WHERE id = 'cppp:1'", (new_id,))

        # Migration: Ensure location_only tenders are never Tier A
        conn.execute("UPDATE tenders SET tier = 'B' WHERE match_basis = 'location_only' AND tier = 'A'")
        conn.commit()


def cleanup_expired_tenders() -> int:
    """Delete expired rows from the database and return count of deleted rows."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM tenders WHERE closing_date != '' AND closing_date < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
        )
        conn.commit()
        return cur.rowcount


def rescore_all_tenders() -> dict[str, int]:
    """Recalculate relevance_score for all stored tenders in DB and return score distribution."""
    from tender_mcp.matcher import score_tender
    from collections import Counter

    distribution = Counter()
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, title, organisation, category, item_category, similar_category, status, raw_json FROM tenders"
        ).fetchall()
        for row in rows:
            r_dict = dict(row)
            res = score_tender(r_dict)
            new_score = res["score"]
            matched_terms = res.get("matched_terms_str", "")
            status = r_dict.get("status", "active")
            if res.get("suggested_status") == "details needed":
                status = "details needed"
            buyer_flag = res.get("buyer_is_oil_gas", 0)
            match_basis = res.get("match_basis") or "item"
            tier = res["tier"]
            kind = res.get("tender_kind") or "unknown"
            b_match = res.get("buyer_match", 0)
            cat_noise = res.get("category_noise_reason")
            conn.execute(
                "UPDATE tenders SET relevance_score = ?, matched_terms = ?, status = ?, buyer_is_oil_gas = ?, tier = ?, tender_kind = ?, is_consumable = ?, match_basis = ?, category_noise_reason = ?, buyer_match = ? WHERE id = ?",
                (new_score, matched_terms, status, buyer_flag, tier, kind, 1 if res.get("is_consumable") else 0, match_basis, cat_noise, b_match, r_dict["id"]),
            )
            if new_score == 0:
                distribution["0"] += 1
            elif new_score < 30:
                distribution["1-29"] += 1
            elif new_score < 60:
                distribution["30-59"] += 1
            elif new_score < 80:
                distribution["60-79"] += 1
            else:
                distribution["80-100"] += 1
        conn.commit()
    return dict(distribution)


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def upsert_tender(tender: dict[str, Any]) -> tuple[str, bool]:
    """
    Insert or update a tender record.
    Normalizes dates to ISO 8601.
    Returns (id, is_new).
    """
    tid = tender["id"]
    from tender_mcp.matcher import score_tender, is_oil_gas_buyer, compute_tier
    from tender_mcp.dates import to_iso8601

    scoring = score_tender(tender)

    # Ensure relevance score is calculated
    relevance_score = tender.get("relevance_score")
    if relevance_score is None:
        relevance_score = scoring["score"]
        tender["relevance_score"] = relevance_score

    matched_terms = tender.get("matched_terms")
    if matched_terms is None:
        matched_terms = scoring.get("matched_terms_str", "")
        tender["matched_terms"] = matched_terms

    raw_status = tender.get("status")
    if not raw_status or raw_status == "active":
        status = "new"
    else:
        status = raw_status
    tender["status"] = status
    owner = tender.get("owner") or ""
    notes = tender.get("notes") or ""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    org = str(tender.get("organisation") or "")
    buyer_flag = 1 if is_oil_gas_buyer(org) else 0
    consumable_flag = 1 if scoring.get("is_consumable") else 0
    match_basis = scoring.get("match_basis") or "item"
    buyer_match_flag = scoring.get("buyer_match", 0) if int(relevance_score) < 30 else 0
    tier = compute_tier(int(relevance_score), org, match_basis=match_basis, buyer_match=bool(buyer_match_flag))
    tender_kind = scoring.get("tender_kind") or ""
    if not tender_kind:
        from tender_mcp.matcher import classify_tender_kind
        tender_kind = classify_tender_kind(tender.get("title") or "")

    # Normalize dates to ISO 8601
    pub_date = to_iso8601(tender.get("published_date", ""))
    close_date = to_iso8601(tender.get("closing_date", ""))
    open_date = to_iso8601(tender.get("opening_date", ""))

    with get_conn() as conn:
        existing = conn.execute(
            """SELECT id, closing_date, extensions_seen, was_extended,
                      original_end_utc, auto_extend_days, auto_extend_max_count,
                      auto_extension_limit_utc, corrigendum_date, date_check,
                      is_extended, extension_days_observed, status, owner, notes,
                      similar_category, category_noise_reason, item_category, relevance_score, tier, buyer_match
               FROM tenders WHERE id = ?""", (tid,)
        ).fetchone()

        import json
        raw_json = json.dumps(tender.get("raw", {}), ensure_ascii=False)

        if existing is None:
            conn.execute(
                """
                INSERT INTO tenders
                    (id, source, title, organisation, category,
                     published_date, closing_date, opening_date,
                     emd_value, tender_value, link, status, owner, notes, status_updated_at, relevance_score,
                     matched_terms, buyer_is_oil_gas, tier, tender_kind, is_consumable, match_basis, raw_json,
                     date_check, similar_category, category_noise_reason, buyer_match)
                VALUES
                    (:id, :source, :title, :organisation, :category,
                     :published_date, :closing_date, :opening_date,
                     :emd_value, :tender_value, :link, :status, :owner, :notes, :status_updated_at, :relevance_score,
                     :matched_terms, :buyer_is_oil_gas, :tier, :tender_kind, :is_consumable, :match_basis, :raw_json,
                     :date_check, :similar_category, :category_noise_reason, :buyer_match)
                """,
                {
                    "id": tid,
                    "source": tender.get("source", ""),
                    "title": tender.get("title", ""),
                    "organisation": tender.get("organisation", ""),
                    "category": tender.get("category", ""),
                    "published_date": pub_date,
                    "closing_date": close_date,
                    "opening_date": open_date,
                    "emd_value": tender.get("emd_value", ""),
                    "tender_value": tender.get("tender_value", ""),
                    "link": tender.get("link", ""),
                    "status": status,
                    "owner": owner,
                    "notes": notes,
                    "status_updated_at": now_iso,
                    "relevance_score": int(relevance_score),
                    "matched_terms": matched_terms,
                    "buyer_is_oil_gas": buyer_flag,
                    "tier": tier,
                    "tender_kind": tender_kind,
                    "is_consumable": consumable_flag,
                    "match_basis": match_basis,
                    "raw_json": raw_json,
                    "date_check": tender.get("date_check") or "not_checked",
                    "similar_category": tender.get("similar_category"),
                    "category_noise_reason": scoring.get("category_noise_reason"),
                    "buyer_match": buyer_match_flag,
                },
            )
            conn.commit()
            return tid, True
        else:
            old_close = existing["closing_date"]
            ext_seen = existing["extensions_seen"] or 0
            was_ext = existing["was_extended"] if ("was_extended" in existing.keys() and existing["was_extended"] is not None) else 0
            is_ext = existing["is_extended"] if ("is_extended" in existing.keys() and existing["is_extended"] is not None) else 0
            date_chk = existing["date_check"] if ("date_check" in existing.keys() and existing["date_check"]) else "not_checked"
            ext_days_obs = existing["extension_days_observed"] if ("extension_days_observed" in existing.keys()) else None

            # Check if tender already has similar_category or category_noise_reason
            existing_sim_cat = existing["similar_category"] if "similar_category" in existing.keys() else None
            existing_noise = existing["category_noise_reason"] if "category_noise_reason" in existing.keys() else None
            existing_item_cat = existing["item_category"] if "item_category" in existing.keys() else None
            from tender_mcp.matcher import check_pdf_category_noise, is_product_supply_title, compute_tier
            sim_to_check = tender.get("similar_category") or existing_sim_cat
            item_to_check = tender.get("item_category") or existing_item_cat
            title_to_check = tender.get("title") or existing.get("title") or ""
            if is_product_supply_title(title_to_check):
                is_noise, noise_reason = False, None
            else:
                is_noise, noise_reason = check_pdf_category_noise(item_to_check, sim_to_check, title_to_check)
            if is_noise:
                relevance_score = min(int(relevance_score), 30)
                tier = compute_tier(int(relevance_score), org, match_basis=match_basis, category_noise_reason=noise_reason, buyer_match=bool(buyer_match_flag))
                cat_noise_reason = noise_reason
            else:
                cat_noise_reason = None if is_product_supply_title(title_to_check) else existing_noise

            if old_close and close_date and old_close != close_date:
                conn.execute(
                    "INSERT INTO closing_date_history (tender_id, old_value, new_value, seen_at) VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))",
                    (tid, old_close, close_date),
                )
                if close_date > old_close:
                    ext_seen += 1
                    was_ext = 1
                    is_ext = 1

                # Recalculate date_check whenever closing_date changes during a refresh:
                orig_end = existing["original_end_utc"] if "original_end_utc" in existing.keys() else None
                if orig_end:
                    from tender_mcp.gem_pdf import compute_extension_fields
                    auto_days = existing["auto_extend_days"] if "auto_extend_days" in existing.keys() else None
                    auto_count = existing["auto_extend_max_count"] if "auto_extend_max_count" in existing.keys() else None
                    corr_date = existing["corrigendum_date"] if "corrigendum_date" in existing.keys() else None
                    recalc = compute_extension_fields(
                        orig_end,
                        close_date,
                        auto_days,
                        auto_count,
                        corrigendum_date=corr_date,
                    )
                    date_chk = recalc["date_check"]
                    is_ext = recalc["is_extended"]
                    ext_days_obs = recalc["extension_days_observed"]

            conn.execute(
                """
                UPDATE tenders SET
                    title            = :title,
                    organisation     = :organisation,
                    category         = :category,
                    published_date   = :published_date,
                    closing_date     = :closing_date,
                    opening_date     = :opening_date,
                    emd_value        = :emd_value,
                    tender_value     = :tender_value,
                    link             = :link,
                    relevance_score  = :relevance_score,
                    matched_terms    = :matched_terms,
                    buyer_is_oil_gas = :buyer_is_oil_gas,
                    tier             = :tier,
                    tender_kind      = :tender_kind,
                    is_consumable    = :is_consumable,
                    match_basis      = :match_basis,
                    raw_json         = :raw_json,
                    extensions_seen  = :extensions_seen,
                    was_extended     = :was_extended,
                    is_extended      = :is_extended,
                    date_check       = :date_check,
                    extension_days_observed = :extension_days_observed,
                    category_noise_reason = :category_noise_reason,
                    buyer_match      = :buyer_match,
                    last_seen_at     = strftime('%Y-%m-%dT%H:%M:%SZ','now')
                WHERE id = :id
                """,
                {
                    "id": tid,
                    "title": tender.get("title", ""),
                    "organisation": tender.get("organisation", ""),
                    "category": tender.get("category", ""),
                    "published_date": pub_date,
                    "closing_date": close_date,
                    "opening_date": open_date,
                    "emd_value": tender.get("emd_value", ""),
                    "tender_value": tender.get("tender_value", ""),
                    "link": tender.get("link", ""),
                    "relevance_score": int(relevance_score),
                    "matched_terms": matched_terms,
                    "buyer_is_oil_gas": buyer_flag,
                    "tier": tier,
                    "tender_kind": tender_kind,
                    "is_consumable": consumable_flag,
                    "match_basis": match_basis,
                    "raw_json": raw_json,
                    "extensions_seen": ext_seen,
                    "was_extended": was_ext,
                    "is_extended": is_ext,
                    "date_check": date_chk,
                    "extension_days_observed": ext_days_obs,
                    "category_noise_reason": cat_noise_reason,
                    "buyer_match": buyer_match_flag,
                },
            )
            conn.commit()
            return tid, False



def query_tenders(
    source: str | None = None,
    status: str | None = None,
    closing_within_days: int | None = None,
    min_relevance: int | None = None,
    include_expired: bool = False,
) -> list[dict[str, Any]]:
    """Return tenders as a list of dicts, with optional filters."""
    clauses: list[str] = []
    params: list[Any] = []

    if source:
        clauses.append("source = ?")
        params.append(source)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if closing_within_days is not None:
        clauses.append(
            "closing_date <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ? || ' days')"
        )
        params.append(f"+{closing_within_days}")
    if min_relevance is not None:
        clauses.append("relevance_score >= ?")
        params.append(min_relevance)

    if not include_expired:
        # Hide tenders whose closing time has already passed
        clauses.append(
            "(closing_date IS NULL OR closing_date = '' OR closing_date >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
        )

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM tenders {where} ORDER BY closing_date ASC"

    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_tender_by_id(tender_id: str) -> dict[str, Any] | None:
    """Fetch a single tender by its composite id."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenders WHERE id = ?", (tender_id,)
        ).fetchone()
    return dict(row) if row else None


def log_refresh_start(source: str, started_at: str) -> int:
    """Insert a refresh_log row and return its rowid."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO refresh_log (started_at, source) VALUES (?, ?)",
            (started_at, source),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]


def log_refresh_end(
    rowid: int,
    finished_at: str,
    fetched_count: int,
    new_count: int,
    updated_count: int,
    error_msg: str | None = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE refresh_log SET
                finished_at   = ?,
                fetched_count = ?,
                new_count     = ?,
                updated_count = ?,
                error_msg     = ?
            WHERE id = ?
            """,
            (finished_at, fetched_count, new_count, updated_count, error_msg, rowid),
        )
        conn.commit()


def get_previous_refresh_stats(source: str, current_log_id: int) -> dict[str, Any] | None:
    """Get the previous completed refresh log for a given source."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT fetched_count, new_count, updated_count, error_msg, finished_at
            FROM refresh_log
            WHERE source = ? AND id < ? AND finished_at IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (source, current_log_id),
        ).fetchone()
        return dict(row) if row else None


def save_gem_pdf_enrichment(
    tender_id: str,
    fields: dict[str, Any],
    profile: dict[str, Any] | None = None,
) -> None:
    """Save extracted PDF fields and update eligibility, details_needed, and rescore if item_category was missing."""
    import json
    from datetime import datetime, timezone
    from tender_mcp.matcher import score_tender
    from tender_mcp.gem_pdf import evaluate_eligibility

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    status = fields.get("pdf_parse_status", "ok")

    with get_conn() as conn:
        tender = conn.execute("SELECT * FROM tenders WHERE id = ?", (tender_id,)).fetchone()
        if not tender:
            return

        tender_dict = dict(tender)

        if status == "mismatch":
            conn.execute(
                """
                UPDATE tenders SET
                    pdf_fetched_at = ?,
                    pdf_parse_status = 'mismatch'
                WHERE id = ?
                """,
                (now_iso, tender_id),
            )
            conn.commit()
            return

        if status not in ("ok", "partial"):
            conn.execute(
                """
                UPDATE tenders SET
                    pdf_fetched_at = ?,
                    pdf_parse_status = ?
                WHERE id = ?
                """,
                (now_iso, status, tender_id),
            )
            conn.commit()
            return

        # Status is ok or partial: evaluate eligibility and extension tracking
        from tender_mcp.gem_pdf import evaluate_eligibility, compute_extension_fields
        eligibility, reasons, flags = evaluate_eligibility(fields, profile)

        corrigendum = fields.get("corrigendum_date") or tender_dict.get("corrigendum_date")
        ext_data = compute_extension_fields(
            fields.get("bid_end_datetime"),
            tender_dict.get("closing_date"),
            fields.get("auto_extend_days"),
            fields.get("auto_extend_max_count"),
            corrigendum_date=corrigendum,
        )

        ext_seen_row = conn.execute(
            "SELECT count(*) FROM closing_date_history WHERE tender_id = ? AND new_value > old_value",
            (tender_id,),
        ).fetchone()
        extensions_seen = ext_seen_row[0] if ext_seen_row else 0
        was_ext = 1 if (extensions_seen > 0 or ext_data.get("is_extended", 0) == 1 or tender_dict.get("was_extended") == 1) else 0

        # Details needed handling:
        # If title is "[Items not listed] ..." or has no item text, fill item_category from PDF, rescore, details_needed=0
        title = tender_dict.get("title") or ""
        item_cat = fields.get("item_category")
        details_needed = 0

        # Merge fields into tender_dict for rescoring
        new_title = title
        similar_cat = fields.get("similar_category") or tender_dict.get("similar_category")
        if title.startswith("[Items not listed]") and item_cat:
            new_title = f"{item_cat} ({title})"

        org = fields.get("organisation") or tender_dict.get("organisation") or ""
        scoring = score_tender({
            "title": new_title,
            "category": item_cat or tender_dict.get("category"),
            "item_category": item_cat or tender_dict.get("item_category"),
            "similar_category": similar_cat,
            "organisation": org,
            "description": tender_dict.get("description"),
            "status": tender_dict.get("status"),
        })
        new_score = scoring["score"]
        new_tier = scoring["tier"]
        new_kind = scoring.get("tender_kind", "unknown")
        new_terms = scoring.get("matched_terms_str", "")
        match_basis = scoring.get("match_basis", "item")
        cat_noise_reason = scoring.get("category_noise_reason")

        conn.execute(
            """
            UPDATE tenders SET
                title = :title,
                category = COALESCE(:category, category),
                organisation = COALESCE(:organisation, organisation),
                relevance_score = :relevance_score,
                tier = :tier,
                tender_kind = :tender_kind,
                matched_terms = :matched_terms,
                match_basis = :match_basis,
                bid_number = :bid_number,
                bid_end_datetime = :bid_end_datetime,
                bid_opening_datetime = :bid_opening_datetime,
                bid_offer_validity_days = :bid_offer_validity_days,
                ministry = :ministry,
                department = :department,
                office_name = :office_name,
                item_category = :item_category,
                total_quantity = :total_quantity,
                estimated_bid_value_inr = :estimated_bid_value_inr,
                emd_required = :emd_required,
                emd_amount_inr = :emd_amount_inr,
                epbg_required = :epbg_required,
                epbg_percent = :epbg_percent,
                epbg_months = :epbg_months,
                mse_reserved = :mse_reserved,
                mse_purchase_preference = :mse_purchase_preference,
                mse_experience_turnover_relaxation = :mse_experience_turnover_relaxation,
                startup_relaxation = :startup_relaxation,
                mii_class_restriction = :mii_class_restriction,
                mii_preference = :mii_preference,
                bis_required = :bis_required,
                min_turnover_lakh = :min_turnover_lakh,
                years_experience_required = :years_experience_required,
                oem_authorization_required = :oem_authorization_required,
                bid_type = :bid_type,
                reverse_auction = :reverse_auction,
                short_duration_bid = :short_duration_bid,
                inspection_required = :inspection_required,
                contract_period = :contract_period,
                delivery_days = :delivery_days,
                consignee_city = :consignee_city,
                bid_splitting = :bid_splitting,
                pdf_fetched_at = :pdf_fetched_at,
                pdf_parse_status = :pdf_parse_status,
                details_needed = :details_needed,
                eligibility = :eligibility,
                eligibility_reasons = :eligibility_reasons,
                auto_extend_min_bids = :auto_extend_min_bids,
                auto_extend_days = :auto_extend_days,
                auto_extend_max_count = :auto_extend_max_count,
                original_end_utc = :original_end_utc,
                auto_extension_limit_utc = :auto_extension_limit_utc,
                was_extended = :was_extended,
                is_extended = :is_extended,
                extension_days_observed = :extension_days_observed,
                date_check = :date_check,
                extensions_seen = :extensions_seen,
                past_performance_percent = :past_performance_percent,
                oem_average_turnover_lakh = :oem_average_turnover_lakh,
                flags = :flags,
                emd_amount_inr_min = :emd_amount_inr_min,
                emd_amount_inr_max = :emd_amount_inr_max,
                emd_schedules = :emd_schedules,
                corrigendum_date = :corrigendum_date,
                buyer_overrides_gem_fields = :buyer_overrides_gem_fields,
                floor_price_inr_min = :floor_price_inr_min,
                floor_price_inr_max = :floor_price_inr_max,
                floor_price_schedules = :floor_price_schedules,
                pbg_months_per_lot = :pbg_months_per_lot,
                similar_category = :similar_category,
                category_noise_reason = :category_noise_reason,
                buyer_match = :buyer_match
            WHERE id = :id
            """,
            {
                "id": tender_id,
                "title": new_title,
                "category": item_cat or tender_dict.get("category"),
                "organisation": fields.get("organisation") or tender_dict.get("organisation"),
                "relevance_score": new_score,
                "tier": new_tier,
                "tender_kind": new_kind,
                "matched_terms": new_terms,
                "match_basis": match_basis,
                "bid_number": fields.get("bid_number"),
                "bid_end_datetime": fields.get("bid_end_datetime"),
                "bid_opening_datetime": fields.get("bid_opening_datetime"),
                "bid_offer_validity_days": fields.get("bid_offer_validity_days"),
                "ministry": fields.get("ministry"),
                "department": fields.get("department"),
                "office_name": fields.get("office_name"),
                "item_category": item_cat,
                "total_quantity": fields.get("total_quantity"),
                "estimated_bid_value_inr": fields.get("estimated_bid_value_inr"),
                "emd_required": 1 if fields.get("emd_required") else (0 if fields.get("emd_required") is False else None),
                "emd_amount_inr": fields.get("emd_amount_inr"),
                "epbg_required": 1 if fields.get("epbg_required") else (0 if fields.get("epbg_required") is False else None),
                "epbg_percent": fields.get("epbg_percent"),
                "epbg_months": fields.get("epbg_months"),
                "mse_reserved": 1 if fields.get("mse_reserved") else (0 if fields.get("mse_reserved") is False else None),
                "mse_purchase_preference": fields.get("mse_purchase_preference"),
                "mse_experience_turnover_relaxation": 1 if fields.get("mse_experience_turnover_relaxation") else (0 if fields.get("mse_experience_turnover_relaxation") is False else None),
                "startup_relaxation": 1 if fields.get("startup_relaxation") else (0 if fields.get("startup_relaxation") is False else None),
                "mii_class_restriction": fields.get("mii_class_restriction"),
                "mii_preference": fields.get("mii_preference"),
                "bis_required": 1 if (fields.get("bis_required") is True or fields.get("bis_required") == 1) else (0 if fields.get("bis_required") is False else ("not_stated" if fields.get("bis_required") == "not_stated" else None)),
                "min_turnover_lakh": fields.get("min_turnover_lakh"),
                "years_experience_required": fields.get("years_experience_required"),
                "oem_authorization_required": 1 if fields.get("oem_authorization_required") else (0 if fields.get("oem_authorization_required") is False else None),
                "bid_type": fields.get("bid_type"),
                "reverse_auction": 1 if fields.get("reverse_auction") else (0 if fields.get("reverse_auction") is False else None),
                "short_duration_bid": 1 if fields.get("short_duration_bid") else (0 if fields.get("short_duration_bid") is False else None),
                "inspection_required": 1 if fields.get("inspection_required") else (0 if fields.get("inspection_required") is False else None),
                "contract_period": fields.get("contract_period"),
                "delivery_days": fields.get("delivery_days"),
                "consignee_city": fields.get("consignee_city"),
                "bid_splitting": fields.get("bid_splitting"),
                "pdf_fetched_at": now_iso,
                "pdf_parse_status": status,
                "details_needed": details_needed,
                "eligibility": eligibility,
                "eligibility_reasons": json.dumps(reasons),
                "auto_extend_min_bids": fields.get("auto_extend_min_bids"),
                "auto_extend_days": fields.get("auto_extend_days"),
                "auto_extend_max_count": fields.get("auto_extend_max_count"),
                "original_end_utc": ext_data.get("original_end_utc"),
                "auto_extension_limit_utc": ext_data.get("auto_extension_limit_utc"),
                "is_extended": ext_data.get("is_extended", 0),
                "was_extended": was_ext,
                "extension_days_observed": ext_data.get("extension_days_observed"),
                "date_check": fields.get("date_check") or ext_data.get("date_check", "not_checked" if status == "none" else "check_needed"),
                "extensions_seen": extensions_seen,
                "past_performance_percent": fields.get("past_performance_percent"),
                "oem_average_turnover_lakh": fields.get("oem_average_turnover_lakh"),
                "flags": json.dumps(flags),
                "emd_amount_inr_min": fields.get("emd_amount_inr_min"),
                "emd_amount_inr_max": fields.get("emd_amount_inr_max"),
                "emd_schedules": json.dumps(fields.get("emd_schedules", [])),
                "corrigendum_date": fields.get("corrigendum_date"),
                "buyer_overrides_gem_fields": 1 if fields.get("buyer_overrides_gem_fields") else 0,
                "floor_price_inr_min": fields.get("floor_price_inr_min"),
                "floor_price_inr_max": fields.get("floor_price_inr_max"),
                "floor_price_schedules": json.dumps(fields.get("floor_price_schedules", [])),
                "pbg_months_per_lot": json.dumps(fields.get("pbg_months_per_lot", [])),
                "similar_category": similar_cat,
                "category_noise_reason": cat_noise_reason,
                "buyer_match": scoring.get("buyer_match", 0),
            },
        )
        conn.commit()


VALID_STATUSES = ("new", "interested", "bidding", "skipped", "submitted")


def update_tender_status(
    tender_id: str,
    new_status: str | None = None,
    owner: str | None = None,
    notes: str | None = None,
) -> bool:
    """
    Update status, owner, and/or notes on a tender.
    Records a row in status_history whenever status changes.
    Valid statuses: 'new', 'interested', 'bidding', 'skipped', 'submitted'.
    """
    from datetime import datetime, timezone

    if new_status and new_status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid status '{new_status}'. Valid statuses: {', '.join(VALID_STATUSES)}"
        )

    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with get_conn() as conn:
        tender = conn.execute(
            "SELECT status, owner, notes FROM tenders WHERE id = ?", (tender_id,)
        ).fetchone()
        if not tender:
            return False

        old_status = tender["status"] if "status" in tender.keys() and tender["status"] else "new"
        cur_owner = tender["owner"] if "owner" in tender.keys() and tender["owner"] is not None else ""
        cur_notes = tender["notes"] if "notes" in tender.keys() and tender["notes"] is not None else ""

        final_owner = owner if owner is not None else cur_owner
        final_notes = notes if notes is not None else cur_notes

        status_changed = (new_status is not None and new_status != old_status)
        final_status = new_status if new_status is not None else old_status

        if status_changed:
            conn.execute(
                """
                INSERT INTO status_history (tender_id, old_status, new_status, owner, changed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (tender_id, old_status, final_status, final_owner, now_iso),
            )
            conn.execute(
                """
                UPDATE tenders SET
                    status = ?,
                    owner = ?,
                    notes = ?,
                    status_updated_at = ?
                WHERE id = ?
                """,
                (final_status, final_owner, final_notes, now_iso, tender_id),
            )
        else:
            conn.execute(
                """
                UPDATE tenders SET
                    owner = ?,
                    notes = ?
                WHERE id = ?
                """,
                (final_owner, final_notes, tender_id),
            )
        conn.commit()
        return True


def get_tender_status_history(tender_id: str) -> list[dict[str, Any]]:
    """Return status history rows for a given tender in chronological order."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, tender_id, old_status, new_status, owner, changed_at
            FROM status_history
            WHERE tender_id = ?
            ORDER BY id ASC
            """,
            (tender_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_closing_date_history(tender_id: str) -> list[dict[str, Any]]:
    """Return closing date history rows for a given tender in chronological order."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT id, tender_id, old_value, new_value, seen_at
            FROM closing_date_history
            WHERE tender_id = ?
            ORDER BY id ASC
            """,
            (tender_id,),
        ).fetchall()
        return [dict(r) for r in rows]

