"""Retrieval over event and incident history (subphase E6).

Makes a resolved incident usable as context for a new one. This is the
layer ablation arm C needs, and the one 2.8's GraphRAG composes with
rather than replaces.

## Why lexical, not embeddings

plan.md asks for indexing and similarity search; it does not ask for a
vector store, and adding one would mean a new service (or a pgvector
extension) plus an embedding model, for a corpus that is currently tens
of incidents. CONTEXT.md is explicit that infrastructure arrives when a
concrete need forces it.

So similarity is IDF-weighted token overlap, computed in process. Three
properties earn it its place beyond "no new infrastructure":

1. It works identically on SQLite and Postgres, so the retrieval tests
   stay in the unit tier.
2. It is explainable. `matched_terms` on every result says *why* two
   incidents were judged similar, which is the difference between a
   citation and an assertion.
3. Swapping it is local: `score_documents` is the only thing that would
   change, and `RetrievalResult` would not.

The honest limit: this matches wording, not meaning. Two incidents
describing the same fault in different vocabulary will not retrieve each
other. At this corpus size that is an acceptable trade, and it is the
trade 2.8 revisits with the graph.

## Citations

Every result carries a `Citation` that resolves to real rows, and
`resolve_citation` raises rather than returning a placeholder when it
does not. A retrieval layer that can cite something which no longer
exists is worse than one that returns nothing, because it launders a
guess into a reference.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from state import models
from state.machine import IncidentStatus

#: Retrieval only offers incidents that actually finished. An incident
#: still in flight has no outcome to learn from, and one that escalated
#: has no fix to reuse - offering either as precedent would be offering a
#: guess as experience.
RETRIEVABLE_STATUSES = frozenset({IncidentStatus.RECOVERED})

#: Words carrying no signal for fault similarity. Deliberately short: an
#: aggressive list would strip domain terms, and IDF already discounts
#: anything that appears in most documents.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in into is it its of on or that the
    to was were will with this these those not no""".split()
)

_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]*")


def tokenize(text: str) -> list[str]:
    """Lowercase tokens, keeping dots, dashes and underscores.

    `CrashLoopBackOff`, `payment-api` and `spec.template.spec` are single
    terms in this domain; splitting them would destroy the most
    discriminating parts of a fault description.
    """
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS and len(t) > 1]


@dataclass(frozen=True)
class Citation:
    """A pointer back to the records a retrieval result came from.

    Structured rather than a rendered string, so it can be resolved and
    checked instead of merely displayed.
    """

    incident_id: str
    hypothesis_ids: tuple[int, ...] = ()
    candidate_ids: tuple[int, ...] = ()
    event_ids: tuple[str, ...] = ()

    def render(self) -> str:
        parts = [f"incident={self.incident_id}"]
        if self.hypothesis_ids:
            parts.append("hypotheses=" + ",".join(str(i) for i in self.hypothesis_ids))
        if self.candidate_ids:
            parts.append("candidates=" + ",".join(str(i) for i in self.candidate_ids))
        if self.event_ids:
            parts.append("events=" + ",".join(self.event_ids))
        return " ".join(parts)


@dataclass(frozen=True)
class IncidentDocument:
    """One resolved incident flattened into searchable text."""

    incident_id: str
    service: str
    text: str
    citation: Citation
    #: The fields a prompt would show, kept apart from `text` so context
    #: rendering does not have to re-parse the blob.
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def terms(self) -> list[str]:
        return tokenize(self.text)


@dataclass(frozen=True)
class RetrievalResult:
    document: IncidentDocument
    score: float
    #: Which terms drove the match. This is what makes a result auditable
    #: rather than an oracle.
    matched_terms: tuple[str, ...]

    @property
    def citation(self) -> Citation:
        return self.document.citation


def build_document(incident: models.Incident) -> IncidentDocument:
    """Flatten one incident's provenance into a searchable document.

    Covers what plan.md names: the incident summary, its hypotheses, the
    actions taken, and the outcome.
    """
    hypotheses = [h.claim for h in incident.hypotheses if h.claim]
    selected = [c for c in incident.candidates if c.selected]
    actions = [c.action for c in selected]
    verifications = [v.outcome.value for v in incident.verifications]
    signature_terms: list[str] = []
    for signal in incident.signals:
        for key, value in (signal.signature or {}).items():
            signature_terms.append(f"{key} {value}")

    text = " ".join(
        [
            incident.service,
            incident.environment,
            incident.severity,
            *signature_terms,
            *hypotheses,
            *actions,
            *verifications,
            str(incident.status.value if hasattr(incident.status, "value") else incident.status),
        ]
    )
    return IncidentDocument(
        incident_id=incident.incident_id,
        service=incident.service,
        text=text,
        citation=Citation(
            incident_id=incident.incident_id,
            hypothesis_ids=tuple(h.id for h in incident.hypotheses),
            candidate_ids=tuple(c.id for c in selected),
            event_ids=tuple(r.event_id for r in incident.event_records),
        ),
        summary={
            "incident_id": incident.incident_id,
            "service": incident.service,
            "hypothesis": hypotheses[-1] if hypotheses else None,
            "action": actions[-1] if actions else None,
            "outcome": str(
                incident.status.value if hasattr(incident.status, "value") else incident.status
            ),
        },
    )


def index_incidents(
    session: Session, *, service: str | None = None, limit: int = 500
) -> list[IncidentDocument]:
    """Build documents for the retrievable incidents.

    Bounded for the same reason `search_history` is: an unbounded scan
    works on a sandbox and quietly stops working later.
    """
    stmt = (
        select(models.Incident)
        .where(models.Incident.status.in_(list(RETRIEVABLE_STATUSES)))
        .order_by(models.Incident.id.desc())
        .limit(max(1, limit))
    )
    if service is not None:
        stmt = stmt.where(models.Incident.service == service)
    return [build_document(incident) for incident in session.scalars(stmt).all()]


def score_documents(
    query: str, documents: list[IncidentDocument]
) -> list[tuple[IncidentDocument, float, tuple[str, ...]]]:
    """IDF-weighted overlap between `query` and each document.

    A term appearing in every incident (the service name, on a
    single-service sandbox) carries almost no weight; one appearing in a
    handful carries most of it. That is the whole reason for weighting:
    without it, every incident for `payment-api` would look equally
    similar to every other.
    """
    query_terms = set(tokenize(query))
    if not query_terms or not documents:
        return []

    doc_terms = [set(doc.terms) for doc in documents]
    total = len(documents)
    frequency = Counter(term for terms in doc_terms for term in terms)

    scored = []
    for doc, terms in zip(documents, doc_terms, strict=True):
        shared = query_terms & terms
        if not shared:
            continue
        score = sum(math.log(1 + total / (1 + frequency[term])) for term in shared)
        # Normalized by the query, so a long document cannot outscore a
        # precise match simply by containing more words.
        scored.append((doc, score / len(query_terms), tuple(sorted(shared))))
    scored.sort(key=lambda row: (-row[1], row[0].incident_id))
    return scored


def similar_incidents(
    session: Session,
    query: str,
    *,
    service: str | None = None,
    exclude: str | None = None,
    limit: int = 3,
    min_score: float = 0.05,
) -> list[RetrievalResult]:
    """The resolved incidents most similar to `query`, best first.

    `exclude` drops the incident being diagnosed, which would otherwise
    retrieve itself the moment it resolves. `min_score` keeps a weak
    coincidence from being presented as precedent - returning nothing is
    a better answer than returning noise.
    """
    documents = [d for d in index_incidents(session, service=service) if d.incident_id != exclude]
    return [
        RetrievalResult(document=doc, score=score, matched_terms=terms)
        for doc, score, terms in score_documents(query, documents)
        if score >= min_score
    ][: max(1, limit)]


class CitationError(LookupError):
    """A citation did not resolve. Raised rather than returning a
    placeholder: a reference to a record that does not exist is worse than
    no reference, because it launders a guess into evidence."""


def resolve_citation(session: Session, citation: Citation) -> dict[str, Any]:
    """Fetch the records a citation points at, or raise.

    This is what makes the E6 exit criterion checkable: "a citation
    resolving to a real record" is only meaningful if something actually
    resolves it.
    """
    incident = session.scalar(
        select(models.Incident).where(models.Incident.incident_id == citation.incident_id)
    )
    if incident is None:
        raise CitationError(f"citation names unknown incident {citation.incident_id}")

    hypotheses = [h for h in incident.hypotheses if h.id in set(citation.hypothesis_ids)]
    candidates = [c for c in incident.candidates if c.id in set(citation.candidate_ids)]
    if len(hypotheses) != len(citation.hypothesis_ids):
        raise CitationError(f"citation names hypotheses absent from {citation.incident_id}")
    if len(candidates) != len(citation.candidate_ids):
        raise CitationError(f"citation names candidates absent from {citation.incident_id}")

    return {
        "incident": incident,
        "hypotheses": hypotheses,
        "candidates": candidates,
        "status": incident.status,
    }


def render_context(results: list[RetrievalResult]) -> str:
    """Prior incidents as prompt text.

    Every line carries its citation, so a model quoting one of these can
    be traced back to the record rather than taken on faith.
    """
    if not results:
        return ""
    lines = ["Prior resolved incidents on this service (for reference only):"]
    for result in results:
        summary = result.document.summary
        lines.append(
            f"- [{result.citation.render()}] score={result.score:.2f} "
            f"hypothesis={summary.get('hypothesis') or 'n/a'} "
            f"action={summary.get('action') or 'n/a'} "
            f"outcome={summary.get('outcome')}"
        )
    lines.append(
        "These are precedent, not evidence. Cite collected evidence ids for every "
        "claim; a prior incident alone does not support one."
    )
    return "\n".join(lines)
