"""Prompt snapshot publishing and read-only collection for Self-Learning."""

from __future__ import annotations

from time import time
from typing import Any

from astrbot.api import logger
from astrbot.core.prompt.extensions.types import PromptExtension
from astrbot.core.prompt.interfaces import PromptExtensionCollectorInterface
from astrbot.core.snapshots.types import SnapshotScope

PROMPT_SNAPSHOT_KIND = "prompt.self_learning"
PROMPT_SNAPSHOT_SOURCE = "astrbot_plugin_self_learning"
PROMPT_SNAPSHOT_TTL_SECONDS = 900.0
PROMPT_CONTEXT_VIEWS = frozenset(
    {"turn_plan", "olv_stream", "final_result", "proactive"}
)


def scope_for_event(event: Any) -> SnapshotScope | None:
    scope_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
    if not scope_id:
        getter = getattr(event, "get_session_id", None)
        if callable(getter):
            scope_id = str(getter() or "").strip()
    if not scope_id:
        return None
    return SnapshotScope(scope_type="conversation", scope_id=scope_id)


def publish_prompt_snapshot(
    context: Any,
    event: Any,
    *,
    sections: list[str],
    generated_from: str,
    source_timings_ms: dict[str, float],
) -> None:
    scope = scope_for_event(event)
    get_hub = getattr(context, "get_snapshot_hub", None)
    if scope is None or not callable(get_hub):
        return
    hub = get_hub()
    hub.publish_snapshot(
        kind=PROMPT_SNAPSHOT_KIND,
        source_id=PROMPT_SNAPSHOT_SOURCE,
        scope=scope,
        ttl=PROMPT_SNAPSHOT_TTL_SECONDS,
        payload={
            "sections": tuple(section for section in sections if section.strip()),
            "generated_from": generated_from,
            "source_timings_ms": dict(source_timings_ms),
        },
    )


class SelfLearningPromptSnapshotCollector(PromptExtensionCollectorInterface):
    """Read the last completed Self-Learning context snapshot."""

    prompt_extension_context_views = PROMPT_CONTEXT_VIEWS

    @property
    def plugin_id(self) -> str:
        return PROMPT_SNAPSHOT_SOURCE

    @property
    def priority(self) -> int:
        return 35

    @property
    def lifecycle(self) -> str:
        return "dynamic"

    async def collect(
        self,
        event: Any,
        plugin_context: Any,
        config: Any,
        provider_request: Any = None,
    ) -> list[PromptExtension]:
        del config, provider_request
        scope = scope_for_event(event)
        get_hub = getattr(plugin_context, "get_snapshot_hub", None)
        if scope is None or not callable(get_hub):
            return []
        snapshot = get_hub().get_snapshot(
            kind=PROMPT_SNAPSHOT_KIND,
            source_id=PROMPT_SNAPSHOT_SOURCE,
            scope=scope,
            include_expired=True,
        )
        if snapshot is None or not isinstance(snapshot.payload, dict):
            return []
        sections = snapshot.payload.get("sections", ())
        if not isinstance(sections, (list, tuple)):
            return []
        text = "\n\n".join(str(section).strip() for section in sections if str(section).strip())
        if not text:
            return []
        freshness = "stale" if snapshot.is_expired else "fresh"
        logger.debug(
            "[LLM Hook] Prompt snapshot read revision=%s freshness=%s age_ms=%.0f",
            snapshot.revision,
            freshness,
            max(0.0, (time() - snapshot.generated_at) * 1000),
        )
        return [
            PromptExtension(
                plugin_id=self.plugin_id,
                mount="context",
                title="Self-Learning context snapshot",
                value={"text": text},
                value_kind="mapping",
                order=self.priority,
                meta={
                    "targets": ["persona"],
                    "context_views": sorted(PROMPT_CONTEXT_VIEWS),
                    "source": PROMPT_SNAPSHOT_SOURCE,
                    "snapshot_revision": snapshot.revision,
                    "freshness": freshness,
                    "generated_from": snapshot.payload.get("generated_from", ""),
                },
            )
        ]


__all__ = [
    "PROMPT_SNAPSHOT_KIND",
    "PROMPT_SNAPSHOT_SOURCE",
    "SelfLearningPromptSnapshotCollector",
    "publish_prompt_snapshot",
    "scope_for_event",
]
