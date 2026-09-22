"""The relationship-graph schema (subphase 2.7): CONTEXT.md section 12.

Every node and edge type Helix's graph ever holds is declared here, as
plain, JSON-serializable dataclasses - never a vendor driver record type
crossing an agent boundary (C8 applied to the graph the way it already
applies to evidence).

Node types: `developer`, `service`, `repository`, `database`, `pull_request`,
`deployment`, `incident`, `work_item`, `knowledge_page`.

Edge types (CONTEXT.md section 12's example graph, verbatim):

    Developer --OWNS--> Service
    Developer --REVIEWS--> Repository
    Service --DEPENDS_ON--> Database
    PR --MODIFIES--> Service
    PR --CAUSED--> Deployment
    Incident --INVOLVES--> Service
    Developer --RESOLVED--> Incident
    Incident --TRACKED_BY--> Jira        (WorkItem, provider-neutral)
    Incident --DOCUMENTED_IN--> Notion   (KnowledgePage, provider-neutral)
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class NodeType(StrEnum):
    DEVELOPER = "developer"
    SERVICE = "service"
    REPOSITORY = "repository"
    DATABASE = "database"
    PULL_REQUEST = "pull_request"
    DEPLOYMENT = "deployment"
    INCIDENT = "incident"
    WORK_ITEM = "work_item"
    KNOWLEDGE_PAGE = "knowledge_page"


class EdgeType(StrEnum):
    OWNS = "OWNS"
    REVIEWS = "REVIEWS"
    DEPENDS_ON = "DEPENDS_ON"
    MODIFIES = "MODIFIES"
    CAUSED = "CAUSED"
    INVOLVES = "INVOLVES"
    RESOLVED = "RESOLVED"
    TRACKED_BY = "TRACKED_BY"
    DOCUMENTED_IN = "DOCUMENTED_IN"


@dataclass(frozen=True)
class Node:
    """One graph node. `id` is a stable, human-readable key within its
    type (a developer handle, a service name, `INC-1042`, a PR's
    `owner/repo#123`) - never a vendor-internal database id, so the same
    node id is reachable whether the store is the in-memory fixture or
    live Neo4j."""

    type: NodeType
    id: str
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Edge:
    """One directed relationship between two node ids."""

    type: EdgeType
    source: str
    target: str
    properties: dict[str, Any] = field(default_factory=dict)
