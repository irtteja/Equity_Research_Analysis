"""
KB storage layer: read/write CompanyKB nodes from disk.

One JSON file per company: kb/companies/{SYMBOL}.json
The store loads only the nodes needed for a query — context window efficiency.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Optional

from .schema import CompanyKB


class KBStore:
    def __init__(self, kb_dir: Optional[str] = None):
        if kb_dir is None:
            # Default: kb/companies/ relative to this file's package root
            here = Path(__file__).parent
            kb_dir = here / "companies"
        self.kb_dir = Path(kb_dir)
        self.kb_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str) -> Path:
        return self.kb_dir / f"{symbol.upper()}.json"

    def exists(self, symbol: str) -> bool:
        return self._path(symbol).exists()

    def load(self, symbol: str) -> Optional[CompanyKB]:
        p = self._path(symbol)
        if not p.exists():
            return None
        with open(p) as f:
            return CompanyKB.from_dict(json.load(f))

    def save(self, kb: CompanyKB) -> None:
        p = self._path(kb.symbol)
        with open(p, "w") as f:
            json.dump(kb.to_dict(), f, indent=2, default=str)

    def list_symbols(self) -> list[str]:
        return [p.stem for p in sorted(self.kb_dir.glob("*.json"))]

    def load_or_create(self, symbol: str, name: str = "", sector: str = "", industry: str = "") -> CompanyKB:
        kb = self.load(symbol)
        if kb is None:
            kb = CompanyKB(symbol=symbol.upper(), name=name, sector=sector, industry=industry)
        return kb

    def get_all_summary(self) -> list[dict]:
        """Return lightweight summary (no facts/predictions) for all companies."""
        result = []
        for symbol in self.list_symbols():
            kb = self.load(symbol)
            if kb:
                result.append({
                    "symbol": kb.symbol,
                    "name": kb.name,
                    "sector": kb.sector,
                    "signal": kb.latest_signal,
                    "confidence": kb.latest_confidence,
                    "scored_at": kb.latest_scored_at,
                    "facts": len(kb.facts),
                    "open_questions": len(kb.open_questions_list()),
                    "unresolved_predictions": len(kb.unresolved_predictions()),
                })
        return result
