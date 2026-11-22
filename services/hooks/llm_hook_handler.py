"""Background context refresh and Prompt snapshot publication."""

import asyncio
import time
from typing import Any, Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..monitoring.instrumentation import monitored
from .perf_tracker import PerfTracker
from .prompt_snapshot import publish_prompt_snapshot, scope_for_event


class LLMHookHandler:
    """Refresh context sources without blocking the active model request.

    Args:
        plugin_config: Plugin configuration object.
        diversity_manager: Diversity prompt builder service.
        social_context_injector: Social context injector service.
        v2_integration: V2 learning integration service.
        jargon_query_service: Jargon query service.
        temporary_persona_updater: Session-level persona updater.
        perf_tracker: ``PerfTracker`` for recording timing samples.
        group_id_to_unified_origin: Shared mapping from group_id to UMO.
        db_manager: Database manager for approved few-shot retrieval.
    """

    def __init__(
        self,
        plugin_config: Any,
        diversity_manager: Any,
        social_context_injector: Any,
        v2_integration: Any,
        jargon_query_service: Any,
        temporary_persona_updater: Any,
        perf_tracker: PerfTracker,
        group_id_to_unified_origin: Dict[str, str],
        db_manager: Any = None,
        context: Any = None,
    ) -> None:
        self._config = plugin_config
        self._diversity_manager = diversity_manager
        self._social_context_injector = social_context_injector
        self._v2_integration = v2_integration
        self._jargon_query_service = jargon_query_service
        self._temporary_persona_updater = temporary_persona_updater
        self._perf_tracker = perf_tracker
        self._group_id_to_unified_origin = group_id_to_unified_origin
        self._db_manager = db_manager
        self._context = context
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}

    # Public API

    @monitored
    async def handle(self, event: AstrMessageEvent, req: Any) -> None:
        """Schedule context refresh and leave the current request untouched."""
        del req
        if self._context is None or scope_for_event(event) is None:
            return
        group_id = event.get_group_id() or event.get_sender_id()
        if getattr(event, "unified_msg_origin", ""):
            self._group_id_to_unified_origin[group_id] = event.unified_msg_origin
        key = str(event.unified_msg_origin or event.get_session_id())
        task = self._refresh_tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self.refresh_snapshot(event),
                name=f"self-learning-prompt-snapshot:{key[:32]}",
            )
            self._refresh_tasks[key] = task
            task.add_done_callback(
                lambda done, cache=self._refresh_tasks, cache_key=key: self._finish_refresh_task(
                    done, cache, cache_key
                )
            )

    @staticmethod
    def _finish_refresh_task(
        task: asyncio.Task[None],
        cache: dict[str, asyncio.Task[None]],
        key: str,
    ) -> None:
        if cache.get(key) is task:
            cache.pop(key, None)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            logger.warning("[LLM Hook] background snapshot refresh failed: %s", exception)

    async def refresh_snapshot(self, event: AstrMessageEvent) -> None:
        """Refresh all dynamic context sources in the background."""
        started = time.perf_counter()
        group_id = event.get_group_id() or event.get_sender_id()
        user_id = event.get_sender_id()
        query = event.get_message_str()
        results: dict[str, Any] = {}
        timings: dict[str, float] = {}

        async def run(name: str, operation) -> None:
            source_started = time.perf_counter()
            try:
                results[name] = await asyncio.wait_for(
                    operation(),
                    timeout=max(0.1, float(getattr(self._config, "llm_hook_context_timeout", 3.0))),
                )
            except asyncio.TimeoutError:
                logger.warning("[LLM Hook] background context timed out source=%s", name)
                results[name] = None
            except Exception:
                logger.debug("[LLM Hook] background context failed source=%s", name, exc_info=True)
                results[name] = None
            timings[name] = round((time.perf_counter() - source_started) * 1000, 1)

        await asyncio.gather(
            run("social", lambda: self._fetch_social(group_id, user_id)),
            run("v2", lambda: self._fetch_v2(query, group_id)),
            run("diversity", lambda: self._fetch_diversity(group_id)),
            run("jargon", lambda: self._fetch_jargon(event, group_id)),
            run("few_shots", lambda: self._fetch_few_shots(group_id)),
        )

        sections: List[str] = []
        self._collect_social(results.get("social"), group_id, sections)
        self._collect_v2(results.get("v2"), timings.get("v2", 0.0), sections)
        self._collect_diversity(results.get("diversity"), sections)
        self._collect_jargon(results.get("jargon"), sections)
        self._collect_few_shots(results.get("few_shots"), sections)
        self._collect_session_updates(group_id, sections)
        if not sections:
            logger.debug(
                "[LLM Hook] background snapshot produced no sections, keeping previous value"
            )
            return
        publish_prompt_snapshot(
            self._context,
            event,
            sections=sections,
            generated_from=query,
            source_timings_ms=timings,
        )
        self._perf_tracker.record(
            {
                "ts": time.time(),
                "total_ms": round((time.perf_counter() - started) * 1000, 1),
                "group_id": group_id,
                **{f"{name}_ctx_ms": value for name, value in timings.items()},
            }
        )

    async def shutdown(self) -> None:
        """Cancel refreshes owned by this handler."""
        tasks = list(self._refresh_tasks.values())
        self._refresh_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # Context fetchers

    @monitored
    async def _fetch_social(
        self, group_id: str, user_id: str
    ) -> Optional[str]:
        if not self._social_context_injector:
            logger.debug("[LLM Hook] social_context_injector未初始化，跳过社交上下文注入")
            return None
        try:
            return await self._social_context_injector.format_complete_context(
                group_id=group_id,
                user_id=user_id,
                include_social_relations=self._config.include_social_relations,
                include_affection=self._config.include_affection_info,
                include_mood=False,
                include_expression_patterns=True,
                include_psychological=True,
                include_behavior_guidance=True,
                include_conversation_goal=self._config.enable_goal_driven_chat,
                enable_protection=True,
            )
        except Exception as e:
            logger.warning(f"[LLM Hook] 注入社交上下文失败: {e}")
            return None

    @monitored
    async def _fetch_v2(
        self, prompt: str, group_id: str
    ) -> Optional[Dict[str, Any]]:
        if not self._v2_integration:
            return None
        try:
            return await self._v2_integration.get_enhanced_context(
                prompt, group_id, top_k=self._config.rerank_top_k
            )
        except Exception as e:
            logger.debug(f"[LLM Hook] V2 context retrieval failed: {e}")
            return None

    @monitored
    async def _fetch_diversity(self, group_id: str) -> Optional[str]:
        try:
            content = await self._diversity_manager.build_diversity_prompt_injection(
                "",
                group_id=group_id,
                inject_style=True,
                inject_pattern=True,
                inject_variation=True,
                inject_history=True,
            )
            return content.strip() if content else None
        except Exception as e:
            logger.warning(f"[LLM Hook] 多样性增强失败: {e}")
            return None

    @monitored
    async def _fetch_jargon(
        self, event: AstrMessageEvent, group_id: str
    ) -> Optional[str]:
        if not self._jargon_query_service:
            logger.debug("[LLM Hook] jargon_query_service未初始化，跳过黑话注入")
            return None
        try:
            user_message = (
                event.message_str
                if hasattr(event, "message_str")
                else str(event.get_message())
            )
            return await self._jargon_query_service.check_and_explain_jargon(
                text=user_message, chat_id=group_id
            )
        except Exception as e:
            logger.warning(f"[LLM Hook] 注入黑话理解失败: {e}")
            return None

    @monitored
    async def _fetch_few_shots(self, group_id: str) -> Optional[str]:
        """Fetch approved few-shot dialogue content for the given group."""
        if not self._db_manager:
            return None
        try:
            contents = await self._db_manager.get_approved_few_shots(group_id, limit=3)
            if contents:
                return contents[0]
        except Exception as e:
            logger.warning(f"[LLM Hook] Failed to fetch approved few-shots: {e}")
        return None

    # Result collectors

    @staticmethod
    def _collect_social(
        result: Optional[str], group_id: str, out: List[str]
    ) -> None:
        if result:
            out.append(result)
            logger.debug(f"[LLM Hook] 已准备完整社交上下文 (长度: {len(result)})")
        else:
            logger.debug(f"[LLM Hook] 群组 {group_id} 暂无社交上下文")

    @staticmethod
    def _collect_v2(
        result: Optional[Dict[str, Any]], ms: float, out: List[str]
    ) -> None:
        if not result:
            return
        v2_parts: List[str] = []
        if result.get("knowledge_context"):
            v2_parts.append(f"[Related Knowledge]\n{result['knowledge_context']}")
        if result.get("related_memories"):
            memories_text = "\n".join(result["related_memories"][:5])
            v2_parts.append(f"[Related Memories]\n{memories_text}")
        if result.get("few_shot_examples"):
            examples_text = "\n".join(result["few_shot_examples"][:3])
            v2_parts.append(f"[Style Examples]\n{examples_text}")
        if v2_parts:
            out.append("\n\n".join(v2_parts))
            logger.debug(f"[LLM Hook] V2 context injected ({len(v2_parts)} sections, {ms:.0f}ms)")
        else:
            logger.debug(f"[LLM Hook] V2 context empty ({ms:.0f}ms)")

    @staticmethod
    def _collect_diversity(result: Optional[str], out: List[str]) -> None:
        if result:
            out.append(result)
            logger.debug(f"[LLM Hook] 已准备多样性增强内容 (长度: {len(result)})")

    @staticmethod
    def _collect_jargon(result: Optional[str], out: List[str]) -> None:
        if result:
            out.append(result)
            logger.debug(f"[LLM Hook] 已准备黑话理解内容 (长度: {len(result)})")
        else:
            logger.debug("[LLM Hook] 用户消息中未检测到已知黑话")

    @staticmethod
    def _collect_few_shots(result: Optional[str], out: List[str]) -> None:
        if result:
            out.append(f"[Few-Shot Dialogue Examples]\n{result}")
            logger.debug(f"[LLM Hook] Few-shot dialogue injected (len={len(result)})")
        else:
            logger.debug("[LLM Hook] No approved few-shot dialogues available")

    def _collect_session_updates(
        self, group_id: str, out: List[str]
    ) -> None:
        if not self._temporary_persona_updater:
            logger.debug("[LLM Hook] temporary_persona_updater未初始化，跳过会话级更新注入")
            return
        try:
            session_updates = self._temporary_persona_updater.session_updates.get(
                group_id, []
            )
            if session_updates:
                updates_text = "\n\n".join(session_updates)
                out.append(updates_text)
                logger.debug(
                    f"[LLM Hook] 已准备会话级更新 "
                    f"(会话: {group_id}, 更新数: {len(session_updates)}, "
                    f"长度: {len(updates_text)})"
                )
            else:
                logger.debug(f"[LLM Hook] 会话 {group_id} 暂无增量更新")
        except Exception as e:
            logger.warning(f"[LLM Hook] 注入会话级更新失败: {e}")
