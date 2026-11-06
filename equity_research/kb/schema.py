"""
KB schema: one thesis file per company.

Design principles (from brief):
- Graph-linked: facts reference entities (competitors, suppliers, mgmt)
- Facts have lifecycle: recency + usage_frequency + thesis_linkage + impact_score → relevance
- Predictions are stored and later compared to outcomes
- Open questions drive future research
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional
import uuid


def _now() -> str:
    return datetime.utcnow().isoformat()


def _today() -> str:
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Atomic building blocks
# ---------------------------------------------------------------------------

@dataclass
class Fact:
    """A single piece of evidence in the KB."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str = ""
    source: str = ""          # "concall Q2FY27", "BSE filing 2026-10-15", "news", etc.
    date: str = field(default_factory=_today)
    tags: list[str] = field(default_factory=list)   # ["revenue", "guidance", "capex"]
    impact: float = 0.5       # 0–1: how important this fact is
    # lifecycle
    created_at: str = field(default_factory=_now)
    last_referenced: str = field(default_factory=_now)
    reference_count: int = 0
    contradicted_by: Optional[str] = None  # fact_id that supersedes this

    @property
    def is_active(self) -> bool:
        return self.contradicted_by is None

    def relevance(self, today: Optional[date] = None) -> float:
        """relevance = f(recency, usage_frequency, impact)."""
        if today is None:
            today = date.today()
        fact_date = date.fromisoformat(self.date)
        days_old = (today - fact_date).days
        recency = max(0.0, 1.0 - days_old / 365.0)   # decays over 1 year
        usage = min(1.0, self.reference_count / 10.0)  # saturates at 10 refs
        return 0.5 * recency + 0.3 * self.impact + 0.2 * usage

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Fact":
        return cls(**d)


@dataclass
class Prediction:
    """A forward-looking call stored for later comparison."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    made_at: str = field(default_factory=_now)
    horizon: str = ""          # "3M", "6M", "12M"
    thesis: str = ""           # What was predicted
    signal: str = ""           # "BUY" | "SELL" | "HOLD" | "WATCH"
    confidence: float = 0.5    # 0–1
    score_snapshot: dict = field(default_factory=dict)  # scores at prediction time
    price_at_prediction: Optional[float] = None
    target_price: Optional[float] = None
    # outcome (filled later)
    outcome_date: Optional[str] = None
    outcome_price: Optional[float] = None
    outcome_signal: Optional[str] = None  # actual outcome
    prediction_error: Optional[float] = None  # signed: positive = overestimated
    factor_attributions: dict = field(default_factory=dict)  # which factor contributed most to error

    @property
    def is_resolved(self) -> bool:
        return self.outcome_date is not None

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Prediction":
        return cls(**d)


@dataclass
class Entity:
    """A related entity (competitor, supplier, customer, mgmt person, sector)."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    entity_type: str = ""    # "competitor" | "supplier" | "customer" | "mgmt" | "sector" | "macro"
    symbol: Optional[str] = None   # NSE symbol if listed
    relationship: str = ""   # "primary competitor in power electronics segment"
    signal_direction: str = "neutral"  # "positive" | "negative" | "neutral"
    notes: str = ""
    last_updated: str = field(default_factory=_today)

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Entity":
        return cls(**d)


@dataclass
class OpenQuestion:
    """Something the system is actively looking for."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    question: str = ""
    priority: str = "medium"   # "high" | "medium" | "low"
    raised_at: str = field(default_factory=_today)
    resolved_at: Optional[str] = None
    answer: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.resolved_at is None

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "OpenQuestion":
        return cls(**d)


# ---------------------------------------------------------------------------
# Top-level thesis
# ---------------------------------------------------------------------------

@dataclass
class Thesis:
    bull_case: str = ""
    bear_case: str = ""
    moat: str = ""              # competitive advantage
    key_risks: list[str] = field(default_factory=list)
    investment_horizon: str = "12M"
    last_reviewed: str = field(default_factory=_today)

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, d: dict) -> "Thesis":
        return cls(**d)


# ---------------------------------------------------------------------------
# Company KB entry (the main node)
# ---------------------------------------------------------------------------

@dataclass
class CompanyKB:
    """
    One node in the knowledge graph = one company's complete research file.

    Stored as a single JSON file at kb/companies/{symbol}.json
    """
    symbol: str = ""
    name: str = ""
    sector: str = ""
    industry: str = ""
    isin: str = ""

    thesis: Thesis = field(default_factory=Thesis)
    facts: list[Fact] = field(default_factory=list)
    predictions: list[Prediction] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    open_questions: list[OpenQuestion] = field(default_factory=list)

    # Scores (latest run)
    latest_scores: dict = field(default_factory=dict)
    latest_signal: str = ""
    latest_confidence: float = 0.0
    latest_scored_at: str = ""

    # Metadata
    created_at: str = field(default_factory=_now)
    last_updated: str = field(default_factory=_now)
    version: int = 1

    # Raw financial snapshot (last fetch)
    financial_snapshot: dict = field(default_factory=dict)

    def active_facts(self) -> list[Fact]:
        return [f for f in self.facts if f.is_active]

    def open_questions_list(self) -> list[OpenQuestion]:
        return [q for q in self.open_questions if q.is_open]

    def unresolved_predictions(self) -> list[Prediction]:
        return [p for p in self.predictions if not p.is_resolved]

    def add_fact(self, **kwargs) -> Fact:
        f = Fact(**kwargs)
        self.facts.append(f)
        self.last_updated = _now()
        return f

    def add_prediction(self, **kwargs) -> Prediction:
        p = Prediction(**kwargs)
        self.predictions.append(p)
        self.last_updated = _now()
        return p

    def add_entity(self, **kwargs) -> Entity:
        e = Entity(**kwargs)
        self.entities.append(e)
        self.last_updated = _now()
        return e

    def add_question(self, **kwargs) -> OpenQuestion:
        q = OpenQuestion(**kwargs)
        self.open_questions.append(q)
        self.last_updated = _now()
        return q

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "name": self.name,
            "sector": self.sector,
            "industry": self.industry,
            "isin": self.isin,
            "thesis": self.thesis.to_dict(),
            "facts": [f.to_dict() for f in self.facts],
            "predictions": [p.to_dict() for p in self.predictions],
            "entities": [e.to_dict() for e in self.entities],
            "open_questions": [q.to_dict() for q in self.open_questions],
            "latest_scores": self.latest_scores,
            "latest_signal": self.latest_signal,
            "latest_confidence": self.latest_confidence,
            "latest_scored_at": self.latest_scored_at,
            "created_at": self.created_at,
            "last_updated": self.last_updated,
            "version": self.version,
            "financial_snapshot": self.financial_snapshot,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CompanyKB":
        kb = cls(
            symbol=d.get("symbol", ""),
            name=d.get("name", ""),
            sector=d.get("sector", ""),
            industry=d.get("industry", ""),
            isin=d.get("isin", ""),
            latest_scores=d.get("latest_scores", {}),
            latest_signal=d.get("latest_signal", ""),
            latest_confidence=d.get("latest_confidence", 0.0),
            latest_scored_at=d.get("latest_scored_at", ""),
            created_at=d.get("created_at", _now()),
            last_updated=d.get("last_updated", _now()),
            version=d.get("version", 1),
            financial_snapshot=d.get("financial_snapshot", {}),
        )
        if "thesis" in d:
            kb.thesis = Thesis.from_dict(d["thesis"])
        kb.facts = [Fact.from_dict(f) for f in d.get("facts", [])]
        kb.predictions = [Prediction.from_dict(p) for p in d.get("predictions", [])]
        kb.entities = [Entity.from_dict(e) for e in d.get("entities", [])]
        kb.open_questions = [OpenQuestion.from_dict(q) for q in d.get("open_questions", [])]
        return kb
