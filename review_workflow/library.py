"""Legacy-library migration and confirmed-knowledge aggregation.

This module deliberately keeps migration separate from semantic review.  Legacy
full-text flags are provenance only: they can never grant human confirmation or
citation permission.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence


PAPER_SCHEMA_VERSION = "review_workflow.paper.v1"
LEGACY_STATE = "legacy_unconfirmed"
ELIGIBLE_STATE = "human_confirmed"
ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
IMPORT_HEADING_RE = re.compile(
    r"^###\s+(?P<key>[A-Z0-9]{8})\s+(?:—|–|-)\s+(?P<title>.+?)\s*$",
    re.MULTILINE,
)


class LibraryDataError(ValueError):
    """Raised when a source file does not have the expected safe shape."""


def _load_json(path: Path | str) -> Any:
    source = Path(path)
    try:
        return json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LibraryDataError(f"Cannot read JSON source {source}: {exc}") from exc


def normalize_zotero_key(value: Any) -> str | None:
    """Return a canonical Zotero item key, or ``None`` for unsafe values."""

    if not isinstance(value, str):
        return None
    key = value.strip().upper()
    return key if ZOTERO_KEY_RE.fullmatch(key) else None


def load_screened_records(path: Path | str) -> list[dict[str, Any]]:
    """Load the ``records`` array from the historical screening JSON."""

    payload = _load_json(path)
    if not isinstance(payload, Mapping) or not isinstance(payload.get("records"), list):
        raise LibraryDataError(f"{path} must contain a top-level records array")
    records: list[dict[str, Any]] = []
    for record in payload["records"]:
        if not isinstance(record, Mapping):
            raise LibraryDataError(f"{path} contains a non-object screening record")
        records.append(dict(record))
    return records


def parse_import_batch(path: Path | str) -> dict[str, Any]:
    """Parse a Markdown import batch without treating its prose as confirmed data."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LibraryDataError(f"Cannot read import batch {source}: {exc}") from exc

    cards = [
        {
            "zotero_key": match.group("key"),
            "title": match.group("title").strip(),
            "source": str(source),
        }
        for match in IMPORT_HEADING_RE.finditer(text)
    ]
    batch_id_match = re.search(r"^batch_id:\s*(.+?)\s*$", text, re.MULTILINE)
    count_match = re.search(r"^paper_count:\s*(\d+)\s*$", text, re.MULTILINE)
    return {
        "path": str(source),
        "batch_id": batch_id_match.group(1).strip() if batch_id_match else source.stem,
        "declared_count": int(count_match.group(1)) if count_match else None,
        "cards": cards,
    }


def load_import_batches(imports_dir: Path | str) -> list[dict[str, Any]]:
    root = Path(imports_dir)
    if not root.exists():
        return []
    if not root.is_dir():
        raise LibraryDataError(f"Imports path is not a directory: {root}")
    return [parse_import_batch(path) for path in sorted(root.glob("*.md"))]


def _safe_legacy_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    """Copy useful old fields while excluding anything that could imply approval."""

    allowed = (
        "screening_stage",
        "full_text_state",
        "full_text_read_status",
        "evidence_tier",
        "tier_reason",
        "paper_role",
        "knowledge_products",
        "fulltext_priority",
        "source_import_note",
        "source_prior_screening",
    )
    return {name: deepcopy(record[name]) for name in allowed if name in record}


def make_legacy_skeleton(
    zotero_key: str,
    record: Mapping[str, Any] | None = None,
    import_sources: Sequence[str] = (),
    import_title: str | None = None,
    screening_source: str | None = None,
) -> dict[str, Any]:
    """Create a safe per-paper skeleton from unconfirmed historical data."""

    key = normalize_zotero_key(zotero_key)
    if key is None:
        raise LibraryDataError(f"Invalid Zotero key: {zotero_key!r}")
    record = record or {}
    identity = {
        name: deepcopy(record[name])
        for name in ("title", "doi", "url", "year", "item_type", "venue")
        if record.get(name) not in (None, "")
    }
    if "title" not in identity and import_title:
        identity["title"] = import_title
    provenance: dict[str, Any] = {"import_sources": sorted(set(import_sources))}
    if screening_source:
        provenance["screening_sources"] = [screening_source]

    return {
        "schema_version": PAPER_SCHEMA_VERSION,
        "zotero_key": key,
        "state": LEGACY_STATE,
        "identity": identity,
        "legacy": {
            **_safe_legacy_fields(record),
            "warning": (
                "Historical screening/full-text metadata is unconfirmed and cannot "
                "grant human confirmation or citation permission."
            ),
        },
        "human_review": {
            "status": "not_started",
            "citation_permission": {
                "allowed": False,
                "valid": False,
                "approved_claims": [],
                "status": "not_granted",
            },
        },
        "citation_permission": {
            "allowed": False,
            "valid": False,
            "approved_claims": [],
            "status": "not_granted",
        },
        "provenance": provenance,
    }


def plan_legacy_migration(
    screened_path: Path | str,
    imports_dir: Path | str,
    papers_dir: Path | str | None = None,
) -> dict[str, Any]:
    """Plan, but do not write, one safe skeleton per unique Zotero key."""

    screened_source = Path(screened_path)
    records = load_screened_records(screened_source)
    batches = load_import_batches(imports_dir)

    valid_records: dict[str, dict[str, Any]] = {}
    screened_occurrences: Counter[str] = Counter()
    invalid_screened_records = 0
    for record in records:
        key = normalize_zotero_key(record.get("zotero_key"))
        if key is None:
            invalid_screened_records += 1
            continue
        screened_occurrences[key] += 1
        valid_records.setdefault(key, record)

    import_sources: dict[str, list[str]] = {}
    import_titles: dict[str, str] = {}
    import_occurrences: Counter[str] = Counter()
    batch_count_mismatches: list[dict[str, Any]] = []
    for batch in batches:
        if batch["declared_count"] is not None and batch["declared_count"] != len(batch["cards"]):
            batch_count_mismatches.append(
                {
                    "path": batch["path"],
                    "declared": batch["declared_count"],
                    "parsed": len(batch["cards"]),
                }
            )
        for card in batch["cards"]:
            key = card["zotero_key"]
            import_occurrences[key] += 1
            import_sources.setdefault(key, []).append(batch["path"])
            import_titles.setdefault(key, card["title"])

    all_keys = sorted(set(valid_records) | set(import_sources))
    skeletons = [
        make_legacy_skeleton(
            key,
            valid_records.get(key),
            import_sources.get(key, ()),
            import_titles.get(key),
            str(screened_source) if key in valid_records else None,
        )
        for key in all_keys
    ]
    target = Path(papers_dir) if papers_dir is not None else None
    existing = 0
    if target is not None:
        existing = sum((target / item["zotero_key"] / "paper.json").exists() for item in skeletons)

    stats = {
        "screened_record_count": len(records),
        "screened_unique_key_count": len(valid_records),
        "invalid_screened_record_count": invalid_screened_records,
        "duplicate_screened_key_count": sum(count - 1 for count in screened_occurrences.values()),
        "import_batch_count": len(batches),
        "import_card_count": sum(import_occurrences.values()),
        "import_unique_key_count": len(import_sources),
        "duplicate_import_key_count": sum(count - 1 for count in import_occurrences.values()),
        "batch_count_mismatches": batch_count_mismatches,
        "screened_missing_from_imports": sorted(set(valid_records) - set(import_sources)),
        "imports_missing_from_screened": sorted(set(import_sources) - set(valid_records)),
        "planned_unique_skeleton_count": len(skeletons),
        "existing_paper_count": existing,
        "planned_new_paper_count": len(skeletons) - existing,
        "planned_state_counts": {LEGACY_STATE: len(skeletons)},
        "planned_human_confirmed_count": 0,
        "planned_valid_citation_permission_count": 0,
    }
    return {"skeletons": skeletons, "stats": stats}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    temporary.replace(path)


def write_legacy_skeletons(
    plan: Mapping[str, Any], papers_dir: Path | str, *, dry_run: bool = True
) -> dict[str, Any]:
    """Write planned skeletons without ever overwriting an existing paper card."""

    root = Path(papers_dir)
    planned: list[str] = []
    skipped_existing: list[str] = []
    written: list[str] = []
    for skeleton in plan.get("skeletons", []):
        key = normalize_zotero_key(skeleton.get("zotero_key"))
        if key is None:
            raise LibraryDataError("Migration plan contains an invalid Zotero key")
        destination = root / key / "paper.json"
        if destination.exists():
            skipped_existing.append(key)
            continue
        planned.append(key)
        if not dry_run:
            safe = deepcopy(skeleton)
            # Defense in depth against a hand-edited or stale plan.
            safe["state"] = LEGACY_STATE
            safe["human_review"] = {
                "status": "not_started",
                "citation_permission": {
                    "allowed": False,
                    "valid": False,
                    "approved_claims": [],
                    "status": "not_granted",
                },
            }
            safe["citation_permission"] = {
                "allowed": False,
                "valid": False,
                "approved_claims": [],
                "status": "not_granted",
            }
            _atomic_write_json(destination, safe)
            written.append(key)
    return {
        "dry_run": dry_run,
        "planned": planned,
        "written": written,
        "skipped_existing": skipped_existing,
        "planned_count": len(planned),
        "written_count": len(written),
        "skipped_existing_count": len(skipped_existing),
    }


def load_paper_cards(papers_dir: Path | str) -> list[dict[str, Any]]:
    """Load per-paper JSON cards from ``papers/{key}/paper.json``."""

    root = Path(papers_dir)
    if not root.exists():
        return []
    cards: list[dict[str, Any]] = []
    for path in sorted(root.glob("*/paper.json")):
        payload = _load_json(path)
        if not isinstance(payload, Mapping):
            raise LibraryDataError(f"Paper card must be an object: {path}")
        card = dict(payload)
        card.setdefault("_source_path", str(path))
        cards.append(card)
    return cards


def _hash_matches(card: Mapping[str, Any], permission: Mapping[str, Any], name: str) -> bool:
    aliases = {
        "pdf_hash": ("pdf_hash", "bound_pdf_sha256"),
        "note_hash": ("note_hash", "bound_ai_note_sha256"),
    }
    card_aliases = {
        "pdf_hash": ("pdf_hash", "pdf_sha256"),
        "note_hash": ("note_hash", "ai_note_sha256"),
    }
    expected = next(
        (permission.get(field) for field in aliases[name] if permission.get(field) not in (None, "")),
        None,
    )
    if expected in (None, ""):
        return True
    current = next(
        (card.get(field) for field in card_aliases[name] if card.get(field) not in (None, "")),
        None,
    )
    return isinstance(expected, str) and expected == current


def _approved_claims(card: Mapping[str, Any], permission: Any) -> list[Any]:
    claims: Any = card.get("approved_claims")
    human_review = card.get("human_review")
    if isinstance(human_review, Mapping) and not claims:
        claims = human_review.get("approved_claims")
    if isinstance(permission, Mapping) and not claims:
        claims = permission.get("approved_claims")
    return _as_items(claims)


def citation_permission_is_valid(card: Mapping[str, Any]) -> tuple[bool, str]:
    """Apply the fail-closed eligibility gate to a paper card.

    Both the compact boolean permission and the richer ``{"allowed": true}``
    representation are supported (``valid`` remains a compatibility alias).
    This is a paper-level synthesis gate. Specific claims may remain empty here
    because they are proposed by AI and approved later in a section argument
    package. If a permission pins ``pdf_hash`` or ``note_hash``, the current
    card must still match it.
    """

    if card.get("state") != ELIGIBLE_STATE:
        return False, "state_not_human_confirmed"
    if card.get("rescinded_at") or card.get("revoked_at"):
        return False, "card_rescinded"
    human_review = card.get("human_review")
    permission: Any = card.get("citation_permission")
    if permission is None and isinstance(human_review, Mapping):
        permission = human_review.get("citation_permission")
    if isinstance(permission, bool):
        return (True, "eligible") if permission else (False, "permission_not_granted")
    if not isinstance(permission, Mapping):
        return False, "permission_not_valid"
    if "allowed" in permission:
        if permission.get("allowed") is not True:
            return False, "permission_not_valid"
        if "valid" in permission and permission.get("valid") is not True:
            return False, "permission_not_valid"
    elif permission.get("valid") is not True:
        return False, "permission_not_valid"
    if permission.get("revoked") is True or permission.get("status") in {
        "revoked",
        "rescinded",
        "expired",
    }:
        return False, "permission_revoked_or_expired"
    if not _hash_matches(card, permission, "pdf_hash"):
        return False, "pdf_hash_mismatch"
    if not _hash_matches(card, permission, "note_hash"):
        return False, "note_hash_mismatch"
    return True, "eligible"


def _confirmed_payload(card: Mapping[str, Any]) -> Mapping[str, Any]:
    human_review = card.get("human_review")
    candidates = (
        card.get("confirmed_knowledge"),
        human_review.get("confirmed_knowledge") if isinstance(human_review, Mapping) else None,
        card.get("knowledge_card"),
        card,
    )
    return next((item for item in candidates if isinstance(item, Mapping)), {})


def _as_items(value: Any) -> list[Any]:
    if value in (None, "", []):
        return []
    return list(value) if isinstance(value, list) else [value]


def _with_source(value: Any, key: str, title: str | None = None) -> dict[str, Any]:
    item = deepcopy(dict(value)) if isinstance(value, Mapping) else {"value": deepcopy(value)}
    item.setdefault("zotero_key", key)
    if title:
        item.setdefault("title", title)
    return item


def build_confirmed_outputs(cards: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Build synthesis inputs exclusively from confirmed, citable paper cards."""

    literature_matrix: list[dict[str, Any]] = []
    knowledge_relations: list[dict[str, Any]] = []
    writing_candidates: list[dict[str, Any]] = []
    candidate_claims: list[dict[str, Any]] = []
    eligible_keys: list[str] = []
    rejected: list[dict[str, str]] = []
    seen: set[str] = set()
    input_card_count = 0

    for card in cards:
        input_card_count += 1
        key = normalize_zotero_key(card.get("zotero_key"))
        if key is None:
            rejected.append({"zotero_key": "", "reason": "invalid_zotero_key"})
            continue
        if key in seen:
            rejected.append({"zotero_key": key, "reason": "duplicate_card"})
            continue
        seen.add(key)
        allowed, reason = citation_permission_is_valid(card)
        if not allowed:
            rejected.append({"zotero_key": key, "reason": reason})
            continue

        eligible_keys.append(key)
        identity = card.get("identity")
        title = identity.get("title") if isinstance(identity, Mapping) else card.get("title")
        payload = _confirmed_payload(card)

        matrix_value = payload.get("literature_matrix")
        if matrix_value is None:
            fields = (
                "research_question",
                "method",
                "data",
                "claim",
                "evidence",
                "limitation",
                "relevance",
                "use_as",
            )
            row = {name: deepcopy(payload[name]) for name in fields if payload.get(name) not in (None, "")}
            matrix_value = row or None
        for item in _as_items(matrix_value):
            literature_matrix.append(_with_source(item, key, title))

        relations_value = payload.get("knowledge_relations", payload.get("relationships"))
        for item in _as_items(relations_value):
            relation = _with_source(item, key)
            supporters = relation.get("supporting_papers")
            if supporters is None:
                relation["supporting_papers"] = [key]
            elif isinstance(supporters, list) and key not in supporters:
                supporters.append(key)
            knowledge_relations.append(relation)

        for candidate in _as_items(
            payload.get("candidate_claims", card.get("candidate_claims"))
        ):
            proposed = _with_source(candidate, key, title)
            proposed.setdefault("status", "pending_argument_package_approval")
            candidate_claims.append(proposed)

        permission: Any = card.get("citation_permission")
        human_review = card.get("human_review")
        if permission is None and isinstance(human_review, Mapping):
            permission = human_review.get("citation_permission")
        # The approval list is canonical.  A broader AI-generated
        # ``writing_candidates`` list must never leak unapproved claims.
        detailed_claims = _as_items(payload.get("confirmed_claims"))
        for approved in _approved_claims(card, permission):
            detailed = next(
                (
                    candidate
                    for candidate in detailed_claims
                    if isinstance(candidate, Mapping)
                    and approved
                    in (candidate.get("claim_id"), candidate.get("text"), candidate.get("claim"))
                ),
                approved,
            )
            writing_candidates.append(_with_source(detailed, key, title))

    rejection_counts = dict(sorted(Counter(item["reason"] for item in rejected).items()))
    return {
        "schema_version": "review_workflow.confirmed_library.v1",
        "eligible_zotero_keys": eligible_keys,
        "literature_matrix": literature_matrix,
        "knowledge_relations": knowledge_relations,
        "writing_candidates": writing_candidates,
        "candidate_claims": candidate_claims,
        "rejected": rejected,
        "stats": {
            "card_count": input_card_count,
            "eligible_card_count": len(eligible_keys),
            "rejected_card_count": len(rejected),
            "rejection_reason_counts": rejection_counts,
            "literature_matrix_row_count": len(literature_matrix),
            "knowledge_relation_count": len(knowledge_relations),
            "writing_candidate_count": len(writing_candidates),
            "candidate_claim_count": len(candidate_claims),
        },
    }


def source_sha256(path: Path | str) -> str:
    """Return a provenance checksum without modifying the source."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
