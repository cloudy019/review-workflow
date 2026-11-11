"""Deterministic, single-record candidate screening and batch selection.

Scoring follows the installed screening skills' 0--10 structure:
keywords (0--3), evidence/data value (0--4), and specificity (0--3).
The score is an abstract-level prioritisation aid, never a human inclusion or
citation decision.  ``score_paper`` intentionally refuses paper collections;
cross-paper logic starts only after independent results exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .core import BATCH_TERMINAL_STATES


RUBRIC_VERSION = "xai-scientific-knowledge-1.0.0"
LEGACY_STATE = "legacy_unconfirmed"
READING_THRESHOLD = 7
VALID_AXES = ("method", "materials", "battery")


class ScreeningError(ValueError):
    """A candidate or a precomputed screening result is not safe to use."""


@dataclass(frozen=True)
class ScreeningRubric:
    """Versioned immutable screening criteria for the current review thesis."""

    version: str = RUBRIC_VERSION
    central_thesis: str = (
        "由项目配置提供的研究问题；缺省留空，"
        "避免把具体课题写入通用代码。"
    )
    reading_threshold: int = READING_THRESHOLD
    keyword_max: int = 3
    evidence_max: int = 4
    specificity_max: int = 3


DEFAULT_RUBRIC = ScreeningRubric()


_METHOD_TERMS = (
    "accumulated local effects",
    "counterfactual explanation",
    "explainable artificial intelligence",
    "explainable machine learning",
    "feature attribution",
    "feature importance",
    "interpretable machine learning",
    "partial dependence",
    "rule extraction",
    "shapley",
    "shap",
    "lime",
    "saliency",
    "symbolic regression",
    "xai",
    "explainability",
    "interpretability",
    "interpretable",
    "可解释人工智能",
    "可解释机器学习",
    "可解释性",
    "解释方法",
    "特征归因",
    "特征重要性",
    "符号回归",
    "规则提取",
)

_MATERIALS_TERMS = (
    "materials informatics",
    "materials science",
    "materials discovery",
    "materials design",
    "composition-property",
    "structure-property",
    "crystal structure",
    "semiconductor",
    "catalyst",
    "alloy",
    "polymer",
    "perovskite",
    "材料信息学",
    "材料科学",
    "材料发现",
    "材料设计",
    "构效关系",
    "晶体结构",
    "半导体",
    "催化剂",
    "合金",
    "聚合物",
    "钙钛矿",
)

_BATTERY_TERMS = (
    "lithium-ion battery",
    "lithium ion battery",
    "solid-state battery",
    "battery health",
    "state of health",
    "state of charge",
    "remaining useful life",
    "electrolyte",
    "electrode",
    "cathode",
    "anode",
    "battery degradation",
    "battery lifetime",
    "battery cycle life",
    "锂离子电池",
    "固态电池",
    "电池健康",
    "健康状态",
    "荷电状态",
    "剩余寿命",
    "电解液",
    "电极",
    "正极",
    "负极",
    "电池退化",
    "电池寿命",
)

_KNOWLEDGE_TERMS = (
    "scientific knowledge",
    "design rule",
    "empirical rule",
    "decision rule",
    "physical law",
    "analytical equation",
    "closed-form equation",
    "quantitative relationship",
    "scientific descriptor",
    "mechanistic insight",
    "mechanism",
    "descriptor",
    "equation",
    "hypothesis",
    "theory",
    "科学知识",
    "设计规则",
    "经验规则",
    "决策规则",
    "科学概念",
    "描述符",
    "解析方程",
    "定量规律",
    "机制",
    "理论结构",
    "科学假设",
)

_VALIDATION_TERMS = (
    "independent validation",
    "external validation",
    "experimental validation",
    "prospective validation",
    "out-of-distribution",
    "cross-domain",
    "cross-system",
    "transferability",
    "transferable",
    "generalizability",
    "generalization",
    "falsifiable",
    "reproducible",
    "intervention",
    "独立验证",
    "外部验证",
    "实验验证",
    "前瞻验证",
    "域外验证",
    "跨体系",
    "可迁移",
    "迁移性",
    "泛化",
    "可证伪",
    "可复现",
    "干预实验",
)

_ARTIFACT_TERMS = (
    "benchmark",
    "dataset",
    "open source",
    "source code",
    "github",
    "quantitative",
    "equation",
    "rule",
    "descriptor",
    "workflow",
    "pipeline",
    "基准",
    "数据集",
    "开源代码",
    "定量",
    "方程",
    "规则",
    "描述符",
    "工作流",
)

_PRIMARY_TERMS = (
    "we propose",
    "we develop",
    "we developed",
    "we demonstrate",
    "we report",
    "we validate",
    "we experimentally",
    "本研究提出",
    "本研究开发",
    "我们提出",
    "我们开发",
    "我们验证",
    "实验表明",
)

_REVIEW_TERMS = (
    "systematic review",
    "literature review",
    "review article",
    "a review of",
    "perspective",
    "survey",
    "系统综述",
    "文献综述",
    "综述文章",
    "研究进展",
)


def _scalar_text(value: Any, field: str, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ScreeningError(f"{field}必须是单篇论文的字符串字段")
    cleaned = value.strip()
    if required and not cleaned:
        raise ScreeningError(f"{field}不能为空")
    return cleaned


def _paper_identity(paper: Mapping[str, Any]) -> str:
    identities = []
    for field in ("paper_id", "zotero_key", "doi"):
        value = paper.get(field)
        if value in (None, ""):
            continue
        identities.append(_scalar_text(value, field, required=True))
    if not identities:
        raise ScreeningError("单篇候选必须包含paper_id、zotero_key或doi")
    return identities[0]


def _metadata_text(paper: Mapping[str, Any]) -> str:
    pieces = [
        _scalar_text(paper.get("title"), "title"),
        _scalar_text(paper.get("abstract"), "abstract"),
        _scalar_text(paper.get("paper_type"), "paper_type"),
        _scalar_text(paper.get("direction"), "direction"),
    ]
    for field in ("keywords", "tags", "topics"):
        values = paper.get(field)
        if values is None:
            continue
        if isinstance(values, str):
            pieces.append(values)
            continue
        if not isinstance(values, Sequence) or isinstance(values, (bytes, bytearray)):
            raise ScreeningError(f"{field}必须是字符串或单篇论文的字符串数组")
        if any(not isinstance(value, str) for value in values):
            raise ScreeningError(f"{field}只能包含字符串")
        pieces.extend(values)
    return "\n".join(piece for piece in pieces if piece).casefold()


def _matches(text: str, terms: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({term for term in terms if term.casefold() in text}))


def _classify_axes(text: str) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    signals = {
        "method": _matches(text, _METHOD_TERMS),
        "materials": _matches(text, _MATERIALS_TERMS),
        "battery": _matches(text, _BATTERY_TERMS),
    }
    axes = tuple(axis for axis in VALID_AXES if signals[axis])
    return axes, signals


def _legacy_screening_signals(
    paper: Mapping[str, Any],
) -> tuple[set[str], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Read old screening hints without treating them as verified evidence."""

    axes: set[str] = set()
    role = _scalar_text(paper.get("paper_role"), "paper_role").casefold()
    if role == "computer_science_method" or "method" in role:
        axes.add("method")
    if role == "materials_application" or "material" in role:
        axes.add("materials")
    if role == "battery_application" or "battery" in role:
        axes.add("battery")

    signals = paper.get("signals")
    if signals is None:
        signals = {}
    if not isinstance(signals, Mapping):
        raise ScreeningError("signals必须是单篇旧筛选记录的对象")
    if signals.get("explanation") is True:
        axes.add("method")
    if signals.get("materials") is True:
        axes.add("materials")
    if signals.get("battery") is True:
        axes.add("battery")

    knowledge_signal_names = ("rule", "concept", "equation", "mechanism")
    knowledge = tuple(
        f"legacy:{name}" for name in knowledge_signal_names if signals.get(name) is True
    )
    validation = ("legacy:validation",) if signals.get("validation") is True else ()
    artifacts = ("legacy:auxiliary",) if signals.get("auxiliary") is True else ()

    products = paper.get("knowledge_products")
    if products is not None:
        if isinstance(products, str):
            product_values = [products]
        elif isinstance(products, Sequence) and not isinstance(products, (bytes, bytearray)):
            if any(not isinstance(value, str) for value in products):
                raise ScreeningError("knowledge_products只能包含字符串")
            product_values = list(products)
        else:
            raise ScreeningError("knowledge_products必须是字符串或字符串数组")
        meaningful = sorted(
            {value.strip() for value in product_values if value.strip() not in {"", "—", "-"}}
        )
        if meaningful:
            knowledge = tuple(sorted(set(knowledge) | {f"legacy:product:{v}" for v in meaningful}))
    return axes, knowledge, validation, artifacts


def _route_axes(paper: Mapping[str, Any], detected_axes: Iterable[str]) -> tuple[str, ...]:
    """Use the frozen screening role to prevent incidental domain words from
    masquerading as portfolio coverage.

    A methods paper may mention a semiconductor dataset or remaining useful
    life without becoming a materials/battery application.  The old role is
    still provisional evidence, but it is the most stable routing label in the
    migrated pool and is therefore used only for batch composition.
    """

    axes = set(detected_axes)
    role = _scalar_text(paper.get("paper_role"), "paper_role").casefold()
    if role == "computer_science_method":
        axes = {"method"}
    elif role == "materials_application":
        axes.discard("battery")
        axes.add("materials")
    elif role == "battery_application":
        axes.discard("materials")
        axes.add("battery")
    return tuple(axis for axis in VALID_AXES if axis in axes)


def _source_provenance(paper: Mapping[str, Any]) -> dict[str, Any]:
    attachment_keys: list[str] = []
    direct_keys = paper.get("attachment_keys")
    if direct_keys is not None:
        if isinstance(direct_keys, str):
            direct_keys = [direct_keys]
        if not isinstance(direct_keys, Sequence) or isinstance(direct_keys, (bytes, bytearray)):
            raise ScreeningError("attachment_keys必须是字符串数组")
        if any(not isinstance(key, str) or not key.strip() for key in direct_keys):
            raise ScreeningError("attachment_keys只能包含非空字符串")
        attachment_keys.extend(key.strip() for key in direct_keys)

    attachment_details = paper.get("attachment_details")
    if attachment_details is not None:
        if not isinstance(attachment_details, Mapping):
            raise ScreeningError("attachment_details必须是对象")
        attachments = attachment_details.get("attachments") or []
        if not isinstance(attachments, Sequence) or isinstance(attachments, (str, bytes)):
            raise ScreeningError("attachment_details.attachments必须是数组")
        for attachment in attachments:
            if not isinstance(attachment, Mapping):
                raise ScreeningError("每个attachment必须是对象")
            key = attachment.get("key")
            if isinstance(key, str) and key.strip():
                attachment_keys.append(key.strip())
    attachment_keys = sorted(set(attachment_keys))

    explicit_hint = paper.get("source_hint")
    if explicit_hint is not None:
        source_hint = _scalar_text(explicit_hint, "source_hint", required=True)
    elif attachment_keys:
        source_hint = "zotero_attachment:" + ",".join(attachment_keys)
    else:
        doi = _scalar_text(paper.get("doi"), "doi")
        source_hint = f"doi:{doi}" if doi else "metadata_only"
    return {
        "attachment_keys": attachment_keys,
        "source_hint": source_hint,
        # Historical attachment/index fields are discovery hints only.  A
        # current Zotero/plugin or local-file check must explicitly flip this.
        "source_verified": False,
        "legacy_screening": "provisional",
        "legacy_full_text_state": paper.get("full_text_state"),
    }


def _paper_type_point(paper: Mapping[str, Any], text: str) -> tuple[int, str]:
    explicit = _scalar_text(paper.get("paper_type"), "paper_type").casefold()
    if explicit in {"review", "survey", "perspective", "meta-analysis", "综述"}:
        return 0, "二次研究不增加具体性分"
    if explicit in {
        "primary",
        "primary_research",
        "research_article",
        "article",
        "experimental",
        "methods",
        "method",
    }:
        return 1, "原始研究或方法论文"
    if _matches(text, _REVIEW_TERMS):
        return 0, "题名或摘要呈现综述属性"
    if _matches(text, _PRIMARY_TERMS):
        return 1, "摘要呈现原始研究行为"
    return 0, "文献类型尚不明确"


def score_paper(
    paper: Mapping[str, Any], rubric: ScreeningRubric = DEFAULT_RUBRIC
) -> dict[str, Any]:
    """Score exactly one paper record and return a reproducible result.

    A list, iterator, or mapping containing ``papers``/``records`` is rejected.
    The function never fetches full text and never decides citation permission.
    """

    if not isinstance(paper, Mapping):
        raise ScreeningError("score_paper一次只接受一个paper mapping")
    if "papers" in paper or "records" in paper:
        raise ScreeningError("检测到候选集合；必须逐篇调用score_paper")
    if not isinstance(rubric, ScreeningRubric):
        raise ScreeningError("rubric必须是版本化ScreeningRubric")

    paper_id = _paper_identity(paper)
    title = _scalar_text(paper.get("title"), "title") or paper_id
    text = _metadata_text(paper)
    axes, axis_signals = _classify_axes(text)
    legacy_axes, legacy_knowledge, legacy_validation, legacy_artifacts = (
        _legacy_screening_signals(paper)
    )
    axes = _route_axes(paper, set(axes) | legacy_axes)
    knowledge = tuple(sorted(set(_matches(text, _KNOWLEDGE_TERMS)) | set(legacy_knowledge)))
    validation = tuple(
        sorted(set(_matches(text, _VALIDATION_TERMS)) | set(legacy_validation))
    )
    artifacts = tuple(sorted(set(_matches(text, _ARTIFACT_TERMS)) | set(legacy_artifacts)))
    provenance = _source_provenance(paper)

    keyword_points = 0
    keyword_reasons: list[str] = []
    if "method" in axes:
        keyword_points += 1
        keyword_reasons.append("命中可解释性/方法术语")
    if "materials" in axes or "battery" in axes:
        keyword_points += 1
        keyword_reasons.append("命中材料或电池科学场景")
    if knowledge or validation:
        keyword_points += 1
        keyword_reasons.append("命中知识产物或验证术语")

    evidence_points = 0
    evidence_reasons: list[str] = []
    if knowledge:
        evidence_points += 2
        evidence_reasons.append("出现规则、描述符、方程或机制等知识产物")
    if validation:
        evidence_points += 1
        evidence_reasons.append("出现独立、实验、迁移或可证伪验证")
    if artifacts:
        evidence_points += 1
        evidence_reasons.append("出现可复用的定量、数据、代码或流程产物")

    specificity_points = 0
    specificity_reasons: list[str] = []
    if "method" in axes and ({"materials", "battery"} & set(axes)):
        specificity_points += 2
        specificity_reasons.append("可解释方法与科学应用形成直接交集")
    elif "method" in axes and knowledge:
        specificity_points += 2
        specificity_reasons.append("方法论文直接产生可导出的知识对象")
    elif ({"materials", "battery"} & set(axes)) and knowledge and validation:
        specificity_points += 2
        specificity_reasons.append("科学知识产物与独立验证形成直接交集")
    type_point, type_reason = _paper_type_point(paper, text)
    specificity_points += type_point
    specificity_reasons.append(type_reason)

    # Defensive caps make custom rubric objects safe and preserve 0--10.
    keyword_points = min(keyword_points, rubric.keyword_max)
    evidence_points = min(evidence_points, rubric.evidence_max)
    specificity_points = min(specificity_points, rubric.specificity_max)
    score = min(10, keyword_points + evidence_points + specificity_points)

    if score >= 9:
        status = "highly_relevant"
    elif score >= rubric.reading_threshold:
        status = "relevant"
    elif score >= 5:
        status = "possibly_relevant"
    else:
        status = "not_relevant"

    reasons = keyword_reasons + evidence_reasons + specificity_reasons
    if not reasons:
        reasons = ["题名与摘要未提供研究问题所需的相关信号"]
    return {
        "paper_id": paper_id,
        "zotero_key": _scalar_text(paper.get("zotero_key"), "zotero_key") or None,
        "doi": _scalar_text(paper.get("doi"), "doi") or None,
        "year": paper.get("year"),
        "paper_role": _scalar_text(paper.get("paper_role"), "paper_role") or None,
        "evidence_tier": _scalar_text(paper.get("evidence_tier"), "evidence_tier") or None,
        "fulltext_priority": paper.get("fulltext_priority"),
        "title": title,
        "score": score,
        "status": status,
        "eligible_for_reading": score >= rubric.reading_threshold,
        "rubric_version": rubric.version,
        "central_thesis": rubric.central_thesis,
        "axes": list(axes),
        "breakdown": {
            "keywords": keyword_points,
            "evidence_value": evidence_points,
            "specificity": specificity_points,
        },
        "matched_signals": {
            "method": list(axis_signals["method"]),
            "materials": list(axis_signals["materials"]),
            "battery": list(axis_signals["battery"]),
            "knowledge_product": list(knowledge),
            "validation": list(validation),
            "artifact": list(artifacts),
        },
        "reasons": reasons,
        **provenance,
        "citation_allowed": False,
    }


def _identity_tokens(record: Mapping[str, Any]) -> frozenset[str]:
    tokens: set[str] = set()
    for field in ("paper_id", "zotero_key", "doi"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            tokens.add(value.strip().casefold())
    return frozenset(tokens)


def _dominant_route(record: Mapping[str, Any]) -> str | None:
    role = str(record.get("paper_role") or "").casefold()
    if role == "computer_science_method":
        return "method"
    if role == "materials_application":
        return "materials"
    if role == "battery_application":
        return "battery"
    axes = record.get("axes") or []
    return axes[0] if len(axes) == 1 else None


def _existing_records(existing_tasks: Any) -> list[Mapping[str, Any]]:
    if existing_tasks is None:
        return []
    if isinstance(existing_tasks, Mapping):
        if _identity_tokens(existing_tasks):
            return [existing_tasks]
        values = list(existing_tasks.values())
        if not all(isinstance(value, Mapping) for value in values):
            raise ScreeningError("existing_tasks映射值必须是任务对象")
        return values
    if isinstance(existing_tasks, (str, bytes)) or not isinstance(
        existing_tasks, Iterable
    ):
        raise ScreeningError("existing_tasks必须是任务对象集合")
    records = list(existing_tasks)
    if not all(isinstance(record, Mapping) for record in records):
        raise ScreeningError("existing_tasks只能包含任务对象")
    return records


def _validate_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise ScreeningError("批次选择只能读取逐篇screening result")
    required = {"paper_id", "score", "rubric_version", "axes"}
    missing = required - set(result)
    if missing:
        raise ScreeningError(f"screening result缺少字段：{', '.join(sorted(missing))}")
    paper_id = _scalar_text(result["paper_id"], "paper_id", required=True)
    score = result["score"]
    if isinstance(score, bool) or not isinstance(score, (int, float)) or not 0 <= score <= 10:
        raise ScreeningError(f"{paper_id}的score必须在0到10之间")
    axes = result["axes"]
    if isinstance(axes, str) or not isinstance(axes, Sequence):
        raise ScreeningError(f"{paper_id}的axes必须是数组")
    if any(axis not in VALID_AXES for axis in axes) or len(set(axes)) != len(axes):
        raise ScreeningError(f"{paper_id}包含未知或重复的方向轴")
    version = _scalar_text(result["rubric_version"], "rubric_version", required=True)
    if "source_verified" in result and not isinstance(result["source_verified"], bool):
        raise ScreeningError(f"{paper_id}的source_verified必须是布尔值")
    normalized = dict(result)
    normalized["paper_id"] = paper_id
    normalized["score"] = score
    normalized["axes"] = list(axes)
    normalized["rubric_version"] = version
    return normalized


def select_next_batch(
    screening_results: Sequence[Mapping[str, Any]],
    *,
    existing_tasks: Any = (),
    max_papers: int = 3,
    reading_threshold: int = READING_THRESHOLD,
    require_verified_source: bool = False,
) -> dict[str, Any]:
    """Select at most three independently scored papers for the next batch.

    The highest score is selected first.  Remaining slots prefer candidates
    that cover missing method/materials/battery axes, then higher scores.
    Existing non-legacy jobs and terminal workflow records are excluded.
    """

    if isinstance(screening_results, (str, bytes)) or not isinstance(
        screening_results, Sequence
    ):
        raise ScreeningError("screening_results必须是逐篇结果数组")
    if isinstance(max_papers, bool) or not isinstance(max_papers, int) or not 1 <= max_papers <= 3:
        raise ScreeningError("max_papers必须为1至3")
    if not isinstance(reading_threshold, int) or not 0 <= reading_threshold <= 10:
        raise ScreeningError("reading_threshold必须为0至10的整数")
    if not isinstance(require_verified_source, bool):
        raise ScreeningError("require_verified_source必须是布尔值")

    results = [_validate_result(result) for result in screening_results]
    seen_identities: set[str] = set()
    for result in results:
        identities = set(_identity_tokens(result))
        if identities & seen_identities:
            raise ScreeningError("screening_results包含重复论文")
        seen_identities.update(identities)
    versions = {result["rubric_version"] for result in results}
    if len(versions) > 1:
        raise ScreeningError("同一批次不能混用不同rubric_version的结果")

    excluded_identities: set[str] = set()
    for task in _existing_records(existing_tasks):
        state = task.get("state")
        if state != LEGACY_STATE:
            excluded_identities.update(_identity_tokens(task))

    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    terminal_states = set(BATCH_TERMINAL_STATES)
    for result in results:
        identities = _identity_tokens(result)
        workflow_state = result.get("workflow_state")
        if workflow_state is None and result.get("state") in terminal_states:
            workflow_state = result.get("state")
        if identities & excluded_identities:
            excluded.append(
                {"paper_id": result["paper_id"], "reason": "已有非legacy单篇任务"}
            )
        elif str(result.get("evidence_tier") or "").casefold() in {"exclude", "excluded"}:
            excluded.append(
                {"paper_id": result["paper_id"], "reason": "旧筛选提示为排除，留待规则校准"}
            )
        elif workflow_state in terminal_states:
            excluded.append(
                {"paper_id": result["paper_id"], "reason": f"已处于终态{workflow_state}"}
            )
        elif require_verified_source and result.get("source_verified") is not True:
            excluded.append(
                {"paper_id": result["paper_id"], "reason": "来源尚未经当前流程核验"}
            )
        elif result["score"] < reading_threshold:
            excluded.append(
                {"paper_id": result["paper_id"], "reason": "未达到全文阅读阈值"}
            )
        else:
            eligible.append(result)

    selected: list[dict[str, Any]] = []
    covered: set[str] = set()
    covered_routes: set[str] = set()
    remaining = list(eligible)
    while remaining and len(selected) < max_papers:
        def priority(candidate: Mapping[str, Any]) -> tuple[Any, ...]:
            axes = set(candidate["axes"])
            new_axes = axes - covered
            route = _dominant_route(candidate)
            new_route = route is not None and route not in covered_routes
            if not selected:
                return (-candidate["score"], -len(new_axes), candidate["paper_id"].casefold())
            return (
                -int(new_route),
                -len(new_axes),
                -candidate["score"],
                candidate["paper_id"].casefold(),
            )

        chosen = min(remaining, key=priority)
        remaining.remove(chosen)
        new_axes = set(chosen["axes"]) - covered
        if not selected:
            reason = f"当前候选最高优先级：{chosen['score']}/10"
        elif new_axes:
            reason = (
                f"{chosen['score']}/10，并补足"
                + "、".join(axis for axis in VALID_AXES if axis in new_axes)
                + "方向"
            )
        else:
            reason = f"方向覆盖完成后剩余候选中得分最高：{chosen['score']}/10"
        selected.append({**chosen, "selection_reason": reason})
        covered.update(chosen["axes"])
        route = _dominant_route(chosen)
        if route is not None:
            covered_routes.add(route)

    return {
        "rubric_version": next(iter(versions), RUBRIC_VERSION),
        "reading_threshold": reading_threshold,
        "max_papers": max_papers,
        "require_verified_source": require_verified_source,
        "selected": selected,
        "selected_count": len(selected),
        "covered_axes": [axis for axis in VALID_AXES if axis in covered],
        "covered_routes": [axis for axis in VALID_AXES if axis in covered_routes],
        "excluded": sorted(excluded, key=lambda item: item["paper_id"].casefold()),
    }


# Descriptive aliases for callers that use candidate/batch terminology.
score_candidate = score_paper
select_reading_batch = select_next_batch


__all__ = [
    "DEFAULT_RUBRIC",
    "LEGACY_STATE",
    "READING_THRESHOLD",
    "RUBRIC_VERSION",
    "ScreeningError",
    "ScreeningRubric",
    "VALID_AXES",
    "score_candidate",
    "score_paper",
    "select_next_batch",
    "select_reading_batch",
]
