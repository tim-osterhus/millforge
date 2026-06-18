"""Deterministic offline connector broker for runtime-boundary tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from millforge.connectors.broker import (
    ConnectorBrokerOutcome,
    ConnectorInvocationRequest,
    ConnectorProviderToolEvidence,
)

FakeConnectorHandler = Callable[[ConnectorInvocationRequest], ConnectorBrokerOutcome]


class DeterministicFakeConnectorBroker:
    """In-memory connector broker keyed by connector ID and provider tool name."""

    def __init__(
        self,
        handlers: Mapping[
            tuple[str, str], ConnectorBrokerOutcome | FakeConnectorHandler
        ]
        | None = None,
        *,
        provider_evidence: Mapping[tuple[str, str], ConnectorProviderToolEvidence]
        | None = None,
    ) -> None:
        self._handlers = dict(handlers or {})
        self._provider_evidence = dict(provider_evidence or {})
        self._requests: list[ConnectorInvocationRequest] = []

    @property
    def requests(self) -> tuple[ConnectorInvocationRequest, ...]:
        """Return captured broker requests in invocation order."""
        return tuple(self._requests)

    def register(
        self,
        connector_id: str,
        provider_tool_name: str,
        outcome: ConnectorBrokerOutcome | FakeConnectorHandler,
    ) -> None:
        """Register a deterministic outcome or handler for a scoped provider tool."""
        key = (connector_id, provider_tool_name)
        if key in self._handlers:
            raise ValueError("duplicate fake connector handler")
        self._handlers[key] = outcome

    def has_provider_tool(self, connector_id: str, provider_tool_name: str) -> bool:
        return (connector_id, provider_tool_name) in self._handlers

    def provider_tool_evidence(
        self,
        connector_id: str,
        provider_tool_name: str,
    ) -> ConnectorProviderToolEvidence | None:
        return self._provider_evidence.get((connector_id, provider_tool_name))

    def invoke(self, request: ConnectorInvocationRequest) -> ConnectorBrokerOutcome:
        key = (request.connector_id, request.provider_tool_name)
        handler = self._handlers[key]
        self._requests.append(request)
        if isinstance(handler, ConnectorBrokerOutcome):
            return handler
        return handler(request)
