"""
ledger/agents/base_agent.py
===========================
BaseApexAgent — base for all 5 Apex agents.

LLM provider: Google Gemini via OpenRouter API.
Reads OPENROUTER_API_KEY, OPENROUTER_BASE_URL, LLM_MODEL from environment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx

from ledger.core.errors import OptimisticConcurrencyError
from ledger.core.models import BaseEvent

LANGGRAPH_VERSION = "1.0.0"
MAX_OCC_RETRIES = 5


class BaseApexAgent(ABC):
    """
    Base for all 5 Apex agents. Provides Gas Town session management,
    per-node event recording, tool call recording, OCC retry scaffolding,
    and OpenRouter LLM call wrapper.

    AGENT NODE SEQUENCE (all agents follow this):
        start_session → validate_inputs → load_context → [domain nodes] → write_output → end_session

    Each node must call self._record_node_execution() at its end.
    Each tool/registry call must call self._record_tool_call().
    The write_output node must call self._record_output_written() then
    self._record_node_execution().
    """

    def __init__(
        self,
        agent_id: str,
        agent_type: str,
        store: Any,
        registry: Any,
        model: str | None = None,
    ):
        self.agent_id = agent_id
        self.agent_type = agent_type
        self.store = store
        self.registry = registry
        # Read LLM config from environment — never hardcode
        self.model = model or os.environ.get("LLM_MODEL", "openrouter/free")
        self._base_url = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
        self._api_key = os.environ.get("OPENROUTER_API_KEY", "")

        self.session_id: str | None = None
        self.application_id: str | None = None
        self._session_stream: str | None = None
        self._t0: float | None = None
        self._seq = 0
        self._llm_calls = 0
        self._tokens = 0
        self._cost = 0.0
        self._graph: Any = None

    @abstractmethod
    def build_graph(self) -> Any: ...

    def _initial_state(self, application_id: str) -> dict[str, Any]:
        return {
            "application_id": application_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "errors": [],
            "output_events": [],
            "next_agent": None,
        }

    async def process_application(self, application_id: str) -> None:
        if not self._graph:
            self._graph = self.build_graph()
        self.application_id = application_id
        self.session_id = f"sess-{self.agent_type[:3]}-{uuid4().hex[:8]}"
        self._session_stream = f"agent-{self.agent_type}-{self.session_id}"
        self._t0 = time.time()
        self._seq = 0
        self._llm_calls = 0
        self._tokens = 0
        self._cost = 0.0
        await self._start_session(application_id)
        try:
            result = await self._graph.ainvoke(self._initial_state(application_id))
            await self._complete_session(result)
        except Exception as e:
            await self._fail_session(type(e).__name__, str(e))
            raise

    # ── Session lifecycle events ──────────────────────────────────────────────

    async def _start_session(self, app_id: str) -> None:
        await self._append_session_event(
            {
                "event_type": "AgentSessionStarted",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "agent_id": self.agent_id,
                    "application_id": app_id,
                    "model_version": self.model,
                    "langgraph_graph_version": LANGGRAPH_VERSION,
                    "context_source": "fresh",
                    "context_token_count": 1000,
                    "started_at": datetime.now().isoformat(),
                },
            }
        )

    async def _record_node_execution(
        self,
        name: str,
        in_keys: list[str],
        out_keys: list[str],
        ms: int,
        tok_in: int | None = None,
        tok_out: int | None = None,
        cost: float | None = None,
    ) -> None:
        self._seq += 1
        if tok_in is not None:
            self._tokens += tok_in + (tok_out or 0)
            self._llm_calls += 1
        if cost is not None:
            self._cost += cost
        await self._append_session_event(
            {
                "event_type": "AgentNodeExecuted",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "node_name": name,
                    "node_sequence": self._seq,
                    "input_keys": in_keys,
                    "output_keys": out_keys,
                    "llm_called": tok_in is not None,
                    "llm_tokens_input": tok_in,
                    "llm_tokens_output": tok_out,
                    "llm_cost_usd": cost,
                    "duration_ms": ms,
                    "executed_at": datetime.now().isoformat(),
                },
            }
        )

    async def _record_tool_call(self, tool: str, inp: str, out: str, ms: int) -> None:
        await self._append_session_event(
            {
                "event_type": "AgentToolCalled",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "tool_name": tool,
                    "tool_input_summary": inp,
                    "tool_output_summary": out,
                    "tool_duration_ms": ms,
                    "called_at": datetime.now().isoformat(),
                },
            }
        )

    async def _record_output_written(
        self, events_written: list[dict[str, Any]], summary: str
    ) -> None:
        await self._append_session_event(
            {
                "event_type": "AgentOutputWritten",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "application_id": self.application_id,
                    "events_written": events_written,
                    "output_summary": summary,
                    "written_at": datetime.now().isoformat(),
                },
            }
        )

    async def _record_input_validated(self, validated_keys: list[str], ms: int) -> None:
        await self._append_session_event(
            {
                "event_type": "AgentInputValidated",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "validated_keys": validated_keys,
                    "duration_ms": ms,
                    "validated_at": datetime.now().isoformat(),
                },
            }
        )

    async def _record_input_failed(self, validated_keys: list[str], errors: list[str]) -> None:
        await self._append_session_event(
            {
                "event_type": "AgentInputValidationFailed",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "validated_keys": validated_keys,
                    "errors": errors,
                    "failed_at": datetime.now().isoformat(),
                },
            }
        )

    async def _complete_session(self, result: dict[str, Any]) -> None:
        ms = int((time.time() - (self._t0 or time.time())) * 1000)
        await self._append_session_event(
            {
                "event_type": "AgentSessionCompleted",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "application_id": self.application_id,
                    "total_nodes_executed": self._seq,
                    "total_llm_calls": self._llm_calls,
                    "total_tokens_used": self._tokens,
                    "total_cost_usd": round(self._cost, 6),
                    "total_duration_ms": ms,
                    "next_agent_triggered": result.get("next_agent"),
                    "completed_at": datetime.now().isoformat(),
                },
            }
        )

    async def _fail_session(self, etype: str, emsg: str) -> None:
        await self._append_session_event(
            {
                "event_type": "AgentSessionFailed",
                "event_version": 1,
                "payload": {
                    "session_id": self.session_id,
                    "agent_type": self.agent_type,
                    "application_id": self.application_id,
                    "error_type": etype,
                    "error_message": emsg[:500],
                    "last_successful_node": f"node_{self._seq}",
                    "recoverable": etype in ("llm_timeout", "RateLimitError"),
                    "failed_at": datetime.now().isoformat(),
                },
            }
        )

    async def _append_session_event(self, event_dict: dict[str, Any]) -> None:
        """Append a session management event to the agent session stream."""
        if not self._session_stream:
            return
        event = BaseEvent(
            event_type=event_dict["event_type"],
            event_version=event_dict.get("event_version", 1),
            payload=event_dict["payload"],
        )
        try:
            ver = await self.store.stream_version(self._session_stream)
            await self.store.append(
                stream_id=self._session_stream,
                events=[event],
                expected_version=ver,
            )
        except Exception:
            # Session events are best-effort — don't let them crash the agent
            pass

    async def _append_with_retry(
        self,
        stream_id: str,
        events: list[BaseEvent],
        causation_id: str | None = None,
    ) -> list[int]:
        """Append to any aggregate stream with OCC retry."""
        for attempt in range(MAX_OCC_RETRIES):
            try:
                ver = await self.store.stream_version(stream_id)
                pos = await self.store.append(
                    stream_id=stream_id,
                    events=events,
                    expected_version=ver,
                    causation_id=causation_id,
                )
                return [pos]
            except OptimisticConcurrencyError:
                if attempt < MAX_OCC_RETRIES - 1:
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise
        return []

    # ── LLM call via OpenRouter ───────────────────────────────────────────────

    async def _call_llm(
        self, system: str, user: str, max_tokens: int = 1024
    ) -> tuple[str, int, int, float]:
        """
        Call LLM via OpenRouter API (OpenAI-compatible).
        Returns: (content, tokens_in, tokens_out, cost_usd)
        """
        url = f"{self._base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://apex-financial.internal",
            "X-Title": "Apex Agentic Ledger",
        }
        body = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        tok_in = int(usage.get("prompt_tokens", 0))
        tok_out = int(usage.get("completion_tokens", 0))
        # OpenRouter pricing approximation (varies by model)
        cost = round(tok_in / 1e6 * 3.0 + tok_out / 1e6 * 15.0, 6)
        return content, tok_in, tok_out, cost

    # ── Utilities ─────────────────────────────────────────────────────────────

    @staticmethod
    def _sha(d: object) -> str:
        return hashlib.sha256(json.dumps(str(d), sort_keys=True).encode()).hexdigest()[:16]

    @staticmethod
    def _parse_json(content: str) -> dict[str, Any]:
        """Extract first JSON object from LLM response."""
        import re

        m = re.search(r"\{.*\}", content, re.DOTALL)
        result: dict[str, Any] = json.loads(m.group() if m else content)
        return result
