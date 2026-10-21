"""
tender_mcp/matcher.py  –  Keyword matching & relevance scoring
==============================================================
Loads keywords from keywords.json at the project root.

Scoring rules (as of v4):
  - negative_phrases are masked out before matching (words in wrong sense).
  - context_exclude: if ANY phrase matches, score is capped at 20 regardless of keywords.
  - exclude: non-target categories (furniture, stationery, …) cap score at 15.
  - strong terms: first match +70, each additional +10, total capped at 100.
  - weak terms: +5 each; a tender with ONLY weak matches is capped at 30.
  - staffing_noise (keywords.json) caps the final score at staffing_noise_cap (default 20).
  - logistics_noise (keywords.json) caps the final score at logistics_noise_cap (default 20).
      Only real services belong in these groups – NOT equipment words like battery or pump type.
  - is_consumable comes from the keywords.json 'consumables' list; it never changes the score.
  - Itemless bids (no real item text):
      - Petroleum-ministry organisations: score=20, status="details needed".
      - Others: score=0.
  - buyer_is_oil_gas is determined from the oil_gas_buyers list in keywords.json;
      it is NEVER added to the relevance score.
  - tier:  A = score>=30 AND buyer_is_oil_gas
           B = score>=30 AND NOT buyer_is_oil_gas
           C = score<30
"""
from __future__ import annotations

import functools
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "keywords.json"

_CACHED_CONFIG: dict[str, Any] | None = None


def load_keywords(config_path: Path | str | None = None) -> dict[str, Any]:
    """Load and cache keywords configuration from JSON."""
    global _CACHED_CONFIG
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.warning("keywords.json not found at %s; using built-in defaults", path)
        return {
            "min_score": 30,
            "strong": [
                "petroleum", "crude", "crude oil", "lpg", "lng", "cng",
                "refinery", "pipeline", "hydrocarbon", "bitumen", "lubricant"
            ],
            "weak": ["valve", "valves", "gas", "epc", "drilling"],
            "exclude": ["office furniture", "stationery", "catering", "vehicle hiring"],
            "context_exclude": [],
            "staffing_noise": [],
            "logistics_noise": [],
            "pdf_category_noise": [],
            "oil_gas_buyers": [],
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            _CACHED_CONFIG = json.load(f)
            return _CACHED_CONFIG
    except Exception as exc:
        logger.error("Failed to load keywords from %s: %s", path, exc)
        return {
            "min_score": 30,
            "strong": ["petroleum", "crude", "lpg", "refinery", "pipeline", "hydrocarbon"],
            "weak": ["valve", "gas", "epc", "drilling"],
            "exclude": ["office furniture", "stationery"],
            "context_exclude": [],
            "staffing_noise": [],
            "logistics_noise": [],
            "pdf_category_noise": [],
            "oil_gas_buyers": [],
        }


def get_min_score() -> int:
    """Return the configured minimum relevance score threshold."""
    config = _CACHED_CONFIG or load_keywords()
    return int(config.get("min_score", 30))


@functools.lru_cache(maxsize=1024)
def _get_term_pattern(term: str) -> re.Pattern:
    """
    Build a regex pattern matching singular and plural forms with strict word boundaries.
    Ensures 'gas' matches 'gas' and 'gases', but NEVER 'gasket'.
    Ensures 'pipeline' matches 'pipeline' and 'pipelines'.
    """
    clean = term.strip()
    words = clean.split()
    if not words:
        return re.compile(r"$^")

    last_word = words[-1]
    prefix_words = words[:-1]
    lw_lower = last_word.lower()

    if lw_lower == "gas":
        lw_pat = r"gas(?:es|ses)?"
    elif lw_lower in ("pipes", "valves", "tubes", "tanks", "pumps", "filters", "seals", "gaskets", "tools", "spares", "fans", "gauges"):
        stem = re.escape(last_word[:-1])
        lw_pat = rf"{stem}(?:s)?"
    elif lw_lower.endswith("ies") and len(lw_lower) > 3:
        stem = re.escape(last_word[:-3])
        lw_pat = rf"{stem}(?:y|ies)"
    elif lw_lower.endswith("y") and len(lw_lower) > 2 and lw_lower[-2] not in "aeiou":
        stem = re.escape(last_word[:-1])
        lw_pat = rf"{stem}(?:y|ies)"
    elif lw_lower.endswith("s") and not lw_lower.endswith("ss") and len(lw_lower) > 2:
        stem = re.escape(last_word[:-1])
        lw_pat = rf"{stem}(?:s|es)?"
    elif any(lw_lower.endswith(sfx) for sfx in ("sh", "ch", "x", "z", "ss")):
        stem = re.escape(last_word)
        lw_pat = rf"{stem}(?:es)?"
    else:
        stem = re.escape(last_word)
        lw_pat = rf"{stem}(?:s|es)?"

    if prefix_words:
        prefix_pat = r"\s+".join(re.escape(w) for w in prefix_words)
        full_pat = rf"\b{prefix_pat}\s+{lw_pat}\b"
    else:
        full_pat = rf"\b{lw_pat}\b"

    return re.compile(full_pat, re.IGNORECASE)


def _match_term(term: str, text: str) -> bool:
    """Check if term exists with singular/plural word boundaries in text (case-insensitive)."""
    clean_term = term.strip()
    if not clean_term:
        return False
    return bool(_get_term_pattern(clean_term).search(text))


def _mask_negative_phrases(text: str, negative_phrases: list[str]) -> str:
    """Mask negative phrases with whitespace so terms in wrong sense don't match."""
    cleaned = text
    for phrase in sorted(negative_phrases, key=len, reverse=True):
        p = phrase.strip()
        if p:
            pattern = rf"\b{re.escape(p)}\b"
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return cleaned


def is_petroleum_org(organisation: str) -> bool:
    """Check if an organisation is a Petroleum ministry or oil/gas PSU entity."""
    if not organisation:
        return False
    org_lower = organisation.lower()
    petro_markers = [
        "petroleum",
        "petro chemical",
        "petrochemical",
        "oil india",
        "ongc",
        "gail",
        "bpcl",
        "hpcl",
        "iocl",
        "indian oil",
        "bharat petroleum",
        "hindustan petroleum",
        "mopng",
        "refinery",
        "refineries",
    ]
    return any(marker in org_lower for marker in petro_markers)


def is_oil_gas_buyer(organisation: str, config: dict[str, Any] | None = None) -> bool:
    """
    Check if the organisation matches any entry in the oil_gas_buyers list.
    This is separate from relevance scoring and is used only for tier assignment.
    """
    if not organisation:
        return False
    cfg = config or _CACHED_CONFIG or load_keywords()
    buyers = cfg.get("oil_gas_buyers", [])
    org_lower = organisation.lower()
    return any(b.lower() in org_lower for b in buyers)


def is_product_supply_title(title: str, config: dict[str, Any] | None = None) -> bool:
    """
    Check if title starts with 'Supply of', 'Supply &', 'Procurement of' or 'Purchase of'
    (after stripping an optional 'Custom Bid for Services -' prefix).
    """
    if not title:
        return False
    t = title.strip()
    # Strip optional "Custom Bid for Services -" prefix (case-insensitive)
    t = re.sub(r"^Custom\s+Bid\s+for\s+Services\s*-\s*", "", t, flags=re.IGNORECASE).strip()
    cfg = config or _CACHED_CONFIG or load_keywords()
    prefixes = cfg.get("product_title_prefixes", ["Supply of", "Supply &", "Procurement of", "Purchase of"])
    norm = " ".join(t.split()).lower()
    return any(norm.startswith(" ".join(p.split()).lower()) for p in prefixes if p.strip())


def compute_tier(
    score: int,
    organisation: str,
    config: dict[str, Any] | None = None,
    match_basis: str | None = None,
    category_noise_reason: str | None = None,
    has_noise: bool = False,
    buyer_match: bool = False,
) -> str:
    """
    Return tier string:
      A = score >= 30 AND organisation matches oil_gas_buyers AND match_basis != 'location_only' AND not category_noise
      B = (score >= 30 AND (organisation does NOT match oil_gas_buyers OR match_basis == 'location_only' OR category_noise))
          OR buyer_match
      C = score < 30 AND NOT buyer_match
    """
    if buyer_match:
        return "B"
    cfg = config or _CACHED_CONFIG or load_keywords()
    min_score = int(cfg.get("min_score", 30))
    if score < min_score:
        return "C"
    if category_noise_reason or has_noise:
        return "B"
    if match_basis == "location_only":
        return "B"
    if is_oil_gas_buyer(organisation, cfg):
        return "A"
    return "B"


def _starts_with_any(title: str, prefixes: list[str]) -> bool:
    """Case-insensitive, whitespace-normalised prefix check."""
    norm = " ".join(title.split()).lower()
    return any(norm.startswith(" ".join(p.split()).lower()) for p in prefixes if p.strip())


def classify_tender_kind(title: str, config: dict[str, Any] | None = None) -> str:
    """
    Classify a tender title as 'service', 'product', 'mixed' or 'unknown'.
    All word lists come from keywords.json (nothing hardcoded here).

    Precedence:
      a. title starts with any service_title_prefixes  -> service
      b. title starts with any product_title_prefixes  -> product
      c. count service_kind_words and product_kind_words found in the title:
           service only -> service, product only -> product,
           both -> mixed, neither -> unknown
    """
    cfg = config or _CACHED_CONFIG or load_keywords()
    title = (title or "").strip()
    if not title:
        return "unknown"

    if _starts_with_any(title, cfg.get("service_title_prefixes", [])):
        return "service"
    if _starts_with_any(title, cfg.get("product_title_prefixes", [])):
        return "product"

    service_hits = [w for w in cfg.get("service_kind_words", []) if _match_term(w, title)]
    product_hits = [w for w in cfg.get("product_kind_words", []) if _match_term(w, title)]
    if service_hits and product_hits:
        return "mixed"
    if service_hits:
        return "service"
    if product_hits:
        return "product"
    return "unknown"


LOCATION_PATTERN = re.compile(
    r"\b(?:at|for|in|near)\s+(?:(?:the|a|an)\s+)?(?:[A-Za-z0-9_.-]+(?:\s+&)?\s+){0,6}(?:refinery|refineries|lpg\s+plant|lpg\s+terminal|lpg\s+bottling\s+plant|bottling\s+plant|filling\s+facilities|filling\s+facility|facilities|facility|complex|terminal|station|lpg)\b",
    re.IGNORECASE,
)


def _match_item_terms(text: str, item_terms: list[str]) -> list[str]:
    """Match items from keywords.json item_terms allowing singular/plural forms with word boundary."""
    return [term for term in item_terms if _match_term(term, text)]


def _strong_only_in_location(text: str, strong_terms: list[str]) -> bool:
    """
    Check if all matched strong keywords appear ONLY as part of a plant or location name
    (e.g., 'at <X> LPG Plant', 'for <X> Refinery', 'at <X> LPG Terminal').
    """
    text_no_loc = LOCATION_PATTERN.sub(" ", text)
    has_strong_outside = any(_match_term(t, text_no_loc) for t in strong_terms)
    return not has_strong_outside


def is_consumable(text: str, config: dict[str, Any] | None = None) -> bool:
    """True if any word from the keywords.json 'consumables' list is in the text. No score effect."""
    cfg = config or _CACHED_CONFIG or load_keywords()
    return any(_match_term(w, text or "") for w in cfg.get("consumables", []))


def _apply_noise_caps(
    score: int, matched_staffing: list[str], matched_logistics: list[str], cfg: dict[str, Any]
) -> tuple[int, list[str]]:
    """
    Apply staffing_noise / logistics_noise caps (each group has its own cap in keywords.json).
    Returns (new_score, cap_notes).
    """
    notes: list[str] = []
    if matched_staffing:
        cap = int(cfg.get("staffing_noise_cap", 20))
        if score > cap:
            score = cap
            notes.append(f"staffing_noise cap {cap}: {', '.join(matched_staffing)}")
    if matched_logistics:
        cap = int(cfg.get("logistics_noise_cap", 20))
        if score > cap:
            score = cap
            notes.append(f"logistics_noise cap {cap}: {', '.join(matched_logistics)}")
    return score, notes


def compute_bid_window(hours_to_close: float | None) -> str:
    """
    Return a bid_window label:
      urgent  – closing in < 48 h
      soon    – closing in 2-7 days (48-168 h)
      normal  – closing in > 7 days
      unknown – no closing date
    """
    if hours_to_close is None:
        return "unknown"
    if hours_to_close < 48:
        return "urgent"
    if hours_to_close <= 168:
        return "soon"
    return "normal"

def score_text(haystack: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Score arbitrary text against the keywords rules.
    Used mainly in unit tests.
    """
    cfg = config or _CACHED_CONFIG or load_keywords()
    min_score = int(cfg.get("min_score", 30))

    strong_terms: list[str] = cfg.get("strong", [])
    weak_terms: list[str] = cfg.get("weak", [])
    exclude_terms: list[str] = cfg.get("exclude", [])
    negative_phrases: list[str] = cfg.get("negative_phrases", [])
    context_exclude_phrases: list[str] = cfg.get("context_exclude", [])

    masked_haystack = _mask_negative_phrases(haystack, negative_phrases)

    matched_exclude = [t for t in exclude_terms if _match_term(t, haystack)]
    matched_staffing = [t for t in cfg.get("staffing_noise", []) if _match_term(t, haystack)]
    matched_logistics = [t for t in cfg.get("logistics_noise", []) if _match_term(t, haystack)]
    if re.search(r"\bhiring\b.*?\b(?:vehicle|vehicles|truck|trucks|lorry|lorries|car|cars|cab|cabs)\b", haystack, re.I):
        if "vehicle hiring" not in matched_staffing and "vehicle hiring" not in matched_logistics:
            matched_staffing.append("vehicle hiring")
    if re.search(r"\bgoods\s+vehicle(?:s)?\b", haystack, re.I):
        if "goods vehicle" not in matched_logistics:
            matched_logistics.append("goods vehicle")
    matched_service_noise = matched_staffing + matched_logistics
    # context_exclude is checked on the MASKED text so negative_phrases don't create false context hits
    matched_context_exclude = [p for p in context_exclude_phrases if _match_term(p, masked_haystack)]
    matched_strong = [t for t in strong_terms if _match_term(t, masked_haystack)]
    matched_weak = [t for t in weak_terms if _match_term(t, masked_haystack)]
    matched_items = _match_item_terms(haystack, cfg.get("item_terms", []))

    matched_terms_detailed: list[str] = []
    assessment = ""
    match_basis = "item"

    if matched_context_exclude:
        # context_exclude: cap at 20, no further scoring needed
        score = 20
        assessment = f"CONTEXT_EXCLUDED – capped at 20: {', '.join(matched_context_exclude)}"
        matched_terms_detailed = [f"context_exclude: {p}" for p in matched_context_exclude]
    elif matched_exclude:
        score = min(15, 5 * len(matched_strong))
        assessment = f"EXCLUDED – matched non-target categories: {', '.join(matched_exclude)}"
        matched_terms_detailed = [f"EXCLUDE: {t}" for t in matched_exclude]
    elif matched_strong:
        base = 70 + (len(matched_strong) - 1) * 10
        bonus = len(matched_weak) * 5
        score = min(100, base + bonus)
        assessment = "HIGH – strong petroleum/gas keywords matched"
        for i, t in enumerate(matched_strong):
            pts = 70 if i == 0 else 10
            matched_terms_detailed.append(f"{t} (+{pts})")
        for t in matched_weak:
            matched_terms_detailed.append(f"{t} (+5)")
        all_matched = matched_strong + matched_weak

        if _strong_only_in_location(masked_haystack, matched_strong) and not matched_items:
            score = min(score, 40)
            match_basis = "location_only"
            assessment = f"LOCATION_ONLY – strong keyword only in plant/location name, capped at 40: {', '.join(matched_strong)}"
            matched_terms_detailed.append("[location mention without target item, capped at 40]")
    elif matched_weak:
        base = 20 + (len(matched_weak) - 1) * 5
        score = min(30, base)
        assessment = "LOW – weak keywords only (no core petroleum terms)"
        for i, t in enumerate(matched_weak):
            pts = 20 if i == 0 else 5
            matched_terms_detailed.append(f"{t} (+{pts})")
    else:
        score = 0
        assessment = "NONE – no configured keywords matched"

    is_prod_supply = is_product_supply_title(haystack, cfg)
    if not is_prod_supply and not matched_context_exclude and not matched_exclude:
        score, notes = _apply_noise_caps(score, matched_staffing, matched_logistics, cfg)
        if notes:
            assessment = "SERVICE_NOISE – " + "; ".join(notes)
            matched_terms_detailed.extend(f"[{n}]" for n in notes)

    all_matched = matched_strong + matched_weak

    return {
        "score": score,
        "matched_terms": all_matched,
        "matched_terms_detailed": matched_terms_detailed,
        "matched_terms_str": ", ".join(matched_terms_detailed),
        "matched_strong": matched_strong,
        "matched_weak": matched_weak,
        "matched_items": matched_items,
        "item_term": matched_items[0] if matched_items else "",
        "match_basis": match_basis,
        "matched_exclude": matched_exclude,
        "matched_service_noise": matched_service_noise,
        "matched_staffing_noise": matched_staffing,
        "matched_logistics_noise": matched_logistics,
        "matched_context_exclude": matched_context_exclude,
        "min_score": min_score,
        "is_relevant": score >= min_score,
        "assessment": assessment,
    }


def score_tender(
    tender: dict[str, Any],
    config: dict[str, Any] | None = None,
    org_bonus: int = 0,
) -> dict[str, Any]:
    """
    Score a tender dictionary using ONLY real item text:
      - title
      - category
      - description / items / item_description
    NEVER score source_url, search keyword, or raw_json metadata.
    NEVER rely on department/organisation names for relevance score.

    Scoring rules:
      - context_exclude phrases: cap score at 20.
      - exclude terms: cap score at 15.
      - strong terms: first +70, each additional +10, max 100.
      - weak terms only: +5 each, capped at 30.
      - staffing_noise / logistics_noise: cap final score at their own caps (default 20).
      - itemless (no real item text):
          petroleum org → score=20, status="details needed"
          other org → score=0
    """
    cfg = config or _CACHED_CONFIG or load_keywords()
    min_score = int(cfg.get("min_score", 30))

    strong_terms: list[str] = cfg.get("strong", [])
    weak_terms: list[str] = cfg.get("weak", [])
    exclude_terms: list[str] = cfg.get("exclude", [])
    negative_phrases: list[str] = cfg.get("negative_phrases", [])
    context_exclude_phrases: list[str] = cfg.get("context_exclude", [])

    # ONLY tender's own title, item/category, and description
    title = str(tender.get("title") or "").strip()
    category = str(tender.get("category") or "").strip()
    description = str(
        tender.get("description")
        or tender.get("item_description")
        or tender.get("items")
        or ""
    ).strip()

    # Check if this bid has no real item text
    item_text = "" if title.startswith("[Items not listed]") else title
    for part in [category, description]:
        tokens = [tk for tk in re.split(r"[\s,;/]+", part or "") if tk and not re.match(r"^\d{4,}$", tk)]
        item_text += " " + " ".join(tokens)
    is_itemless = not bool(re.search(r"[a-zA-Z]{3,}", item_text.strip()))

    content_parts = [title, category, description]
    content_haystack = " ".join(part for part in content_parts if part)
    masked_haystack = _mask_negative_phrases(content_haystack, negative_phrases)

    matched_exclude = [t for t in exclude_terms if _match_term(t, content_haystack)]
    matched_staffing = [t for t in cfg.get("staffing_noise", []) if _match_term(t, content_haystack)]
    matched_logistics = [t for t in cfg.get("logistics_noise", []) if _match_term(t, content_haystack)]
    matched_service_noise = matched_staffing + matched_logistics
    # context_exclude is checked on the MASKED text so negative_phrases don't create false context hits
    matched_context_exclude = [p for p in context_exclude_phrases if _match_term(p, masked_haystack)]
    matched_strong = [t for t in strong_terms if _match_term(t, masked_haystack)]
    matched_weak = [t for t in weak_terms if _match_term(t, masked_haystack)]
    matched_items = _match_item_terms(content_haystack, cfg.get("item_terms", []))

    is_details_needed = False
    suggested_status = tender.get("status", "active")
    matched_terms_detailed: list[str] = []
    all_matched: list[str] = []
    assessment = ""
    match_basis = "item"

    if is_itemless:
        org = str(tender.get("organisation") or "")
        if is_petroleum_org(org):
            score = 20
            assessment = "DETAILS_NEEDED – Petroleum-ministry bid with items not listed"
            is_details_needed = True
            suggested_status = "details needed"
            matched_terms_detailed = ["Petroleum-ministry organisation (details needed: 20)"]
            all_matched = ["details needed"]
        else:
            score = 0
            assessment = "NONE – no item text"
            matched_terms_detailed = []
            all_matched = []
    elif matched_context_exclude:
        # context_exclude: cap at 20, regardless of strong matches
        score = 20
        assessment = f"CONTEXT_EXCLUDED – capped at 20: {', '.join(matched_context_exclude)}"
        matched_terms_detailed = [f"context_exclude: {p}" for p in matched_context_exclude]
        all_matched = matched_strong + matched_weak
    elif matched_exclude:
        score = min(15, 5 * len(matched_strong))
        assessment = f"EXCLUDED – matched non-target categories: {', '.join(matched_exclude)}"
        matched_terms_detailed = [f"EXCLUDE: {t}" for t in matched_exclude]
        all_matched = matched_strong + matched_weak
    elif matched_strong:
        base = 70 + (len(matched_strong) - 1) * 10
        bonus = len(matched_weak) * 5
        score = min(100, base + bonus)
        assessment = "HIGH – strong petroleum/gas keywords matched"
        for i, t in enumerate(matched_strong):
            pts = 70 if i == 0 else 10
            matched_terms_detailed.append(f"{t} (+{pts})")
        for t in matched_weak:
            matched_terms_detailed.append(f"{t} (+5)")
        all_matched = matched_strong + matched_weak

        if _strong_only_in_location(masked_haystack, matched_strong) and not matched_items:
            score = min(score, 40)
            match_basis = "location_only"
            assessment = f"LOCATION_ONLY – strong keyword only in plant/location name, capped at 40: {', '.join(matched_strong)}"
            matched_terms_detailed.append("[location mention without target item, capped at 40]")
    elif matched_weak:
        base = 20 + (len(matched_weak) - 1) * 5
        score = min(30, base)
        assessment = "LOW – weak keywords only (no core petroleum terms)"
        for i, t in enumerate(matched_weak):
            pts = 20 if i == 0 else 5
            matched_terms_detailed.append(f"{t} (+{pts})")
        all_matched = matched_strong + matched_weak
    else:
        score = 0
        assessment = "NONE – no configured keywords matched"
        all_matched = []

    is_prod_supply = is_product_supply_title(title, cfg)

    # staffing_noise / logistics_noise caps (only when context_exclude and exclude did not already apply)
    # product-supply titles are protected from noise caps
    if not is_prod_supply and not is_itemless and not matched_context_exclude and not matched_exclude:
        staffing_with_fields = []
        for term in matched_staffing:
            field = "title" if _match_term(term, title) else ("item_category" if _match_term(term, category) else "description")
            staffing_with_fields.append(f"{term} in {field}")
        logistics_with_fields = []
        for term in matched_logistics:
            field = "title" if _match_term(term, title) else ("item_category" if _match_term(term, category) else "description")
            logistics_with_fields.append(f"{term} in {field}")

        score, notes = _apply_noise_caps(score, staffing_with_fields, logistics_with_fields, cfg)
        if notes:
            assessment = "SERVICE_NOISE – " + "; ".join(notes)
            matched_terms_detailed.extend(f"[{n}]" for n in notes)

    # pdf_category_noise check (similar_category, item_category, or title)
    item_cat = tender.get("item_category") or category
    sim_cat = tender.get("similar_category")
    if not is_prod_supply:
        is_cat_noise, cat_noise_reason = check_pdf_category_noise(item_cat, sim_cat, title, cfg)
    else:
        is_cat_noise, cat_noise_reason = False, None

    if is_cat_noise:
        if score > 30:
            score = 30
        matched_terms_detailed.append(f"[{cat_noise_reason}, capped at 30]")
        assessment = f"CATEGORY_NOISE – {cat_noise_reason}, capped at 30"

    matched_terms_str = ", ".join(matched_terms_detailed)

    tender_kind = classify_tender_kind(title, cfg)
    consumable = is_consumable(content_haystack, cfg)
    org = str(tender.get("organisation") or "")

    # Buyer-match lane check:
    # 1. buyer_is_oil_gas = 1
    # 2. title starts with Supply, Procurement, Purchase (after stripping optional Custom Bid for Services -)
    # 3. no noise phrase (staffing, logistics, category) matched
    # 4. relevance_score below 30
    buyer_is_og = 1 if is_oil_gas_buyer(org, cfg) else 0
    clean_title = re.sub(r"^Custom\s+Bid\s+for\s+Services\s*-\s*", "", title, flags=re.IGNORECASE).strip()
    is_supply_tender = clean_title.lower().startswith(("supply", "procurement", "purchase"))
    has_noise = bool(matched_staffing or matched_logistics or is_cat_noise or cat_noise_reason)

    buyer_match = 1 if (buyer_is_og and is_supply_tender and not has_noise and score < 30) else 0
    buyer_match_label = "Oil/gas buyer, supply tender" if buyer_match else None

    tier = compute_tier(
        score,
        org,
        cfg,
        match_basis=match_basis,
        category_noise_reason=cat_noise_reason,
        has_noise=has_noise,
        buyer_match=bool(buyer_match),
    )

    return {
        "score": score,
        "tier": tier,
        "buyer_match": buyer_match,
        "buyer_match_label": buyer_match_label,
        "buyer_is_oil_gas": buyer_is_og,
        "tender_kind": tender_kind,
        "is_consumable": consumable,
        "match_basis": match_basis,
        "matched_items": matched_items,
        "item_term": matched_items[0] if matched_items else "",
        "matched_staffing_noise": matched_staffing,
        "matched_logistics_noise": matched_logistics,
        "matched_terms": all_matched,
        "matched_terms_detailed": matched_terms_detailed,
        "matched_terms_str": matched_terms_str,
        "matched_strong": matched_strong,
        "matched_weak": matched_weak,
        "matched_exclude": matched_exclude,
        "matched_service_noise": matched_service_noise,
        "matched_context_exclude": matched_context_exclude,
        "matched_org": [],
        "org_bonus_applied": 0,
        "is_details_needed": is_details_needed,
        "suggested_status": suggested_status,
        "min_score": min_score,
        "is_relevant": score >= min_score,
        "assessment": assessment,
        "category_noise_reason": cat_noise_reason,
    }


def check_pdf_category_noise(
    item_category: str | None = None,
    similar_category: str | None = None,
    title: str | None = None,
    config: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """
    Check if similar_category, item_category, or title matches any term in pdf_category_noise
    (case-insensitive, whole-word matching only).
    Returns (True, f"Buyer category: {matched_term} in {field}") if matched, else (False, None).
    """
    cfg = config or _CACHED_CONFIG or load_keywords()
    noise_list = cfg.get("pdf_category_noise", [])
    candidates = [
        ("similar_category", similar_category),
        ("item_category", item_category),
        ("title", title),
    ]

    for field_name, text in candidates:
        if not text:
            continue
        for term in noise_list:
            if _match_term(term, text):
                return True, f"Buyer category: {term} in {field_name}"
    return False, None
