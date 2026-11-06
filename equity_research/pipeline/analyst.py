"""
Analyst — Claude reasoning layer.

Takes raw fetched data + existing KB for a company and:
1. Extracts structured facts (0–1 scores with reasoning) for the thesis
2. Identifies open questions
3. Builds / updates the thesis text
4. Populates thesis_check dict for ThesisAlignmentScorer

This is what Claude does that Python can't: read qualitative signals
from filings and concalls and return structured scores.
"""
from __future__ import annotations
import json
import subprocess
import os
import sys
from typing import Optional

from ..kb.schema import CompanyKB, Fact, OpenQuestion


_SYSTEM_PROMPT = """You are a senior equity research analyst specialising in Indian listed companies.
You reason carefully, stay factual, and give structured output exactly as specified.
You focus on durable competitive advantages, management quality, and earnings quality.
Your scores are 0.0–1.0 floats where 1.0 is best/most positive."""


def _call_claude(prompt: str, system: str = _SYSTEM_PROMPT) -> str:
    """Call Claude via the claude CLI (works inside Claude Code sessions without API key)."""
    import shutil
    if not shutil.which("claude"):
        return ""
    try:
        result = subprocess.run(
            ["claude", "-p", "--system-prompt", system],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def _parse_json_from_response(text: str) -> Optional[dict]:
    """Extract first JSON object from Claude's response."""
    import re
    # Try to find ```json ... ``` block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # Try to find bare JSON object
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


class Analyst:
    """
    Uses Claude to perform qualitative analysis and populate the
    thesis_check and management dicts needed by the scorers.
    """

    def analyse(
        self,
        symbol: str,
        name: str,
        sector: str,
        fetched_data: dict,
        existing_kb: Optional[CompanyKB] = None,
    ) -> dict:
        """
        Returns enriched data dict with:
          - thesis_check: {bull_case_score, bear_case_materialised,
                           guidance_momentum, catalyst_progress}
          - management.mgmt_tone_score
          - suggested_thesis: {bull_case, bear_case, moat, key_risks}
          - suggested_facts: list of Fact-ready dicts
          - suggested_questions: list of question strings
        """
        fins = fetched_data.get("financials", [])
        actions = fetched_data.get("corporate_actions", [])
        sh = fetched_data.get("shareholding", {})
        val = fetched_data.get("valuation", {})

        # Build a compact summary for Claude
        fin_summary = []
        for q in fins[:4]:
            fin_summary.append({
                "period": q.get("period"),
                "revenue_cr": self._fmt(q.get("revenue")),
                "pat_cr": self._fmt(q.get("pat")),
                "cfo_cr": self._fmt(q.get("cfo")),
            })

        existing_thesis = {}
        existing_facts_summary = ""
        if existing_kb:
            existing_thesis = existing_kb.thesis.to_dict()
            facts = existing_kb.active_facts()[:5]
            existing_facts_summary = "; ".join(f.text for f in facts)

        prompt = f"""Analyse {name} ({symbol}), sector: {sector}.

## Financial summary (₹ Crores, quarterly, most recent first)
{json.dumps(fin_summary, indent=2)}

## Valuation
PE={val.get('pe_ratio')}, PB={val.get('pb_ratio')}, D/E={val.get('debt_equity')}
Promoter holding={val.get('promoter_holding')}%, Pledge={val.get('promoter_pledge')}%
3Y PAT growth (approx)={val.get('pat_growth_3y', 'n/a')}

## Existing thesis
{json.dumps(existing_thesis) if existing_thesis else "None — first time"}

## Recently known facts
{existing_facts_summary or "None"}

## Corporate actions (last 5)
{json.dumps(actions[:5])}

## Task
Return a JSON object with exactly these keys:
{{
  "bull_case": "2-3 sentence bull case",
  "bear_case": "2-3 sentence bear case",
  "moat": "one sentence on competitive advantage or lack thereof",
  "key_risks": ["risk1", "risk2", "risk3"],
  "bull_case_score": 0.0,
  "bear_case_materialised": 0.0,
  "guidance_momentum": 0.5,
  "catalyst_progress": 0.5,
  "mgmt_tone_score": 0.6,
  "new_facts": [
    {{"text": "...", "source": "financial data", "tags": ["revenue"], "impact": 0.7}}
  ],
  "open_questions": ["question1", "question2"],
  "one_line_summary": "..."
}}

Score rules:
- bull_case_score: 0=thesis completely broken, 1=fully intact
- bear_case_materialised: 0=no bear risks showing, 1=fully playing out
- guidance_momentum: 0=guidance being cut, 1=upgrades
- catalyst_progress: 0=key catalysts delayed/failed, 1=on track/achieved
- mgmt_tone_score: 0=defensive/evasive, 1=confident/specific/honest
Base scores on the ACTUAL data provided, not assumptions."""

        response = _call_claude(prompt)
        parsed = _parse_json_from_response(response) if response else None

        if not parsed:
            # Fallback: neutral scores if Claude unavailable
            parsed = {
                "bull_case": f"{name} in {sector} sector — thesis to be developed",
                "bear_case": "Insufficient data for bear case analysis",
                "moat": "Moat assessment pending concall and filing review",
                "key_risks": ["data unavailable — manual review required"],
                "bull_case_score": 0.5,
                "bear_case_materialised": 0.3,
                "guidance_momentum": 0.5,
                "catalyst_progress": 0.5,
                "mgmt_tone_score": 0.6,
                "new_facts": [],
                "open_questions": [
                    "What is the order book size and visibility?",
                    "How does management guide for next 2 quarters?",
                    "What is the capex cycle plan?",
                ],
                "one_line_summary": f"Initial analysis of {name} — data-driven scores populated",
            }

        # Build thesis_check for ThesisAlignmentScorer
        thesis_check = {
            "bull_case_score": parsed.get("bull_case_score", 0.5),
            "bear_case_materialised": parsed.get("bear_case_materialised", 0.3),
            "guidance_momentum": parsed.get("guidance_momentum", 0.5),
            "catalyst_progress": parsed.get("catalyst_progress", 0.5),
        }

        return {
            "thesis_check": thesis_check,
            "management": {
                "mgmt_tone_score": parsed.get("mgmt_tone_score", 0.6),
            },
            "suggested_thesis": {
                "bull_case": parsed.get("bull_case", ""),
                "bear_case": parsed.get("bear_case", ""),
                "moat": parsed.get("moat", ""),
                "key_risks": parsed.get("key_risks", []),
            },
            "suggested_facts": parsed.get("new_facts", []),
            "suggested_questions": parsed.get("open_questions", []),
            "one_line_summary": parsed.get("one_line_summary", ""),
            "claude_raw_response": response,
        }

    @staticmethod
    def _fmt(v) -> str:
        if v is None:
            return "n/a"
        try:
            return f"{float(v):.1f}"
        except Exception:
            return str(v)
