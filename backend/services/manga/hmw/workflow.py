from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .lib.api_client import APIError
from .lib.publisher import PublishWorkflow
from .lib.state import TaskState


class PanelPublishWorkflow(PublishWorkflow):
    """面板用发布流：限并发转换、CDN 重试、version 过期自动再预检查。"""

    def __init__(self, *args, convert_workers: int = 1, cancel_event: asyncio.Event | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.convert_workers = max(1, min(int(convert_workers or 1), 4))
        self.cancel_event = cancel_event

    def _raise_if_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise asyncio.CancelledError()

    async def _convert_all(self, source, chapters, task_id, *, include_cover: bool = True):
        task_dir = self.temp_root / task_id
        jobs = []
        if include_cover:
            jobs.append(("cover", None, None, "cover.avif", source.cover, task_dir / "cover.avif"))
        for chapter in chapters:
            for index, image in enumerate(chapter.images, start=1):
                filename = f"{index:04d}.avif"
                jobs.append(
                    (
                        "page",
                        chapter.number,
                        index,
                        filename,
                        image,
                        task_dir / f"chapter-{chapter.number:g}" / filename,
                    )
                )
        total = len(jobs)
        converted: list = [None] * total
        await self._progress("converting", 0, total, f"正在转换图片：0/{total}")
        completed = 0
        lock = asyncio.Lock()

        def run_one(job):
            kind, chapter_number, page_index, filename, image, dest = job
            result = self.image_converter(
                image,
                dest,
                quality=self.config.avif_quality,
                background=self.config.avif_background,
            )
            return kind, chapter_number, page_index, filename, result

        loop = asyncio.get_running_loop()
        executor = ThreadPoolExecutor(max_workers=self.convert_workers)
        sem = asyncio.Semaphore(self.convert_workers)

        async def bound(index: int, job) -> None:
            nonlocal completed
            self._raise_if_cancelled()
            async with sem:
                self._raise_if_cancelled()
                item = await loop.run_in_executor(executor, run_one, job)
            converted[index] = item
            async with lock:
                completed += 1
                current = completed
            if current == total or current % 20 == 0 or job[0] == "cover":
                title = "封面" if job[0] == "cover" else f"ch{job[1]:g}"
                await self._progress(
                    "converting",
                    current,
                    total,
                    f"转换 {title} {job[3]} ({current}/{total})",
                )

        try:
            await asyncio.gather(*(bound(i, job) for i, job in enumerate(jobs)))
        finally:
            executor.shutdown(wait=True)
        return converted

    async def _verify_urls(self, state: TaskState) -> None:
        items = list(state.uploaded_objects)
        total = len(items)
        await self._progress("verifying", 0, total, f"正在校验公开 URL：0/{total}")
        if not items:
            return
        queue: asyncio.Queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)
        completed = 0
        lock = asyncio.Lock()

        async def check(url: str) -> bool:
            for attempt in range(5):
                self._raise_if_cancelled()
                valid = await asyncio.to_thread(self.storage.verify_public_url, url)
                if valid:
                    return True
                await asyncio.sleep(1.5 * (attempt + 1))
            return False

        async def worker() -> None:
            nonlocal completed
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    if not await check(item.url):
                        raise RuntimeError(f"公开 URL 无法访问：{item.url}")
                    async with lock:
                        completed += 1
                        current = completed
                    if current == total or current % 50 == 0:
                        await self._progress(
                            "verifying",
                            current,
                            total,
                            f"正在校验公开 URL：{current}/{total}",
                        )
                finally:
                    queue.task_done()

        tasks = [
            asyncio.create_task(worker())
            for _ in range(min(self.config.upload_workers, total))
        ]
        try:
            await asyncio.gather(*tasks)
        except Exception as exc:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            state.stage = "uploaded"
            state.error = str(exc)
            self.state_store.save(state)
            raise

    async def execute(self, source, manga, *, confirm, task_id=None, include_cover: bool = True):
        from uuid import uuid4

        task_id = task_id or uuid4().hex
        slug = str(manga["slug"])
        state = self._load_or_create(task_id, source, slug)
        if not state.manga_payload:
            state.manga_payload = dict(manga)
            self.state_store.save(state)
        if state.stage == "cleaned":
            return state

        cover_key = self.storage.cover_key(slug)
        manga_payload = {**manga, "cover_url": manga.get("cover_url") or self.storage.public_url(cover_key)}
        active_chapters = [chapter for chapter in source.chapters if chapter.action != "skip"]

        if state.stage not in {"uploaded", "committed"}:
            await self._run_preflight(state, manga_payload, source)
            if not await self._confirm(confirm, state.preflight):
                state.stage = "cancelled"
                self.state_store.save(state)
                return state
            state.stage = "confirmed"
            self.state_store.save(state)
            try:
                self._raise_if_cancelled()
                converted = await self._convert_all(
                    source, active_chapters, task_id, include_cover=include_cover
                )
                state.stage = "converted"
                self.state_store.save(state)
                await self._upload_all(state, converted)
                state.stage = "uploaded"
                self.state_store.save(state)
            except asyncio.CancelledError:
                state.error = "任务已取消"
                self.state_store.save(state)
                raise
            except Exception as exc:
                state.error = str(exc)
                self.state_store.save(state)
                raise
        elif not state.expected_version:
            await self._run_preflight(state, manga_payload, source)

        await self._verify_urls(state)
        publish_payload = self._publish_payload(manga_payload, active_chapters, state)
        await self._progress("committing", 0, 1, "正在提交主站数据")
        try:
            state.publish_result = await self._publish_with_retry(
                state, manga_payload, source, active_chapters, publish_payload
            )
        except asyncio.CancelledError:
            state.stage = "uploaded"
            state.error = "任务已取消"
            self.state_store.save(state)
            raise
        except Exception as exc:
            state.stage = "uploaded"
            state.error = str(exc)
            self.state_store.save(state)
            raise

        import shutil

        state.database_committed = True
        state.stage = "committed"
        state.error = ""
        self.state_store.save(state)
        await self._progress("committing", 1, 1, "主站数据已提交")

        old_keys = [
            key
            for url in state.publish_result.get("old_urls", [])
            if (key := self.storage.key_from_public_url(url)) is not None
        ]
        errors = await asyncio.to_thread(self.storage.delete_keys, old_keys) if old_keys else []
        if errors:
            state.orphaned_keys = old_keys
            state.error = "；".join(errors)
        else:
            state.orphaned_keys = []
            state.stage = "cleaned"
            shutil.rmtree(self.temp_root / task_id, ignore_errors=True)
        self.state_store.save(state)
        await self._progress(state.stage, 1, 1, "发布完成")
        return state

    async def _run_preflight(self, state: TaskState, manga_payload: dict, source) -> None:
        payload = {
            "manga": manga_payload,
            "chapters": [
                {
                    "number": chapter.number,
                    "title": chapter.title,
                    "action": chapter.action,
                }
                for chapter in source.chapters
            ],
        }
        state.preflight = await self.api.preflight(payload)
        state.expected_version = state.preflight.get("expected_version")
        state.stage = "preflighted"
        state.error = ""
        self.state_store.save(state)
        await self._progress("preflighted", 1, 1, "预检查完成")

    async def _publish_with_retry(
        self,
        state: TaskState,
        manga_payload: dict,
        source,
        active_chapters,
        publish_payload: dict,
    ) -> dict[str, Any]:
        try:
            return await self.api.publish(publish_payload)
        except APIError as exc:
            if "漫画内容已变化" not in str(exc):
                raise
            await self._run_preflight(state, manga_payload, source)
            retry_payload = self._publish_payload(manga_payload, active_chapters, state)
            return await self.api.publish(retry_payload)

    def _publish_payload(self, manga: dict, chapters, state: TaskState) -> dict:
        cover = next((item for item in state.uploaded_objects if item.kind == "cover"), None)
        cover_url = cover.url if cover is not None else manga.get("cover_url")
        if not cover_url:
            raise RuntimeError("任务缺少已上传封面")
        manga_payload = {**manga, "cover_url": cover_url}
        chapter_payloads = []
        for chapter in chapters:
            pages = sorted(
                (
                    item
                    for item in state.uploaded_objects
                    if item.kind == "page" and item.chapter_number == chapter.number
                ),
                key=lambda item: item.page_index or 0,
            )
            if not pages:
                raise RuntimeError(f"章节 {chapter.number:g} 缺少已上传页面")
            chapter_payloads.append(
                {
                    "number": chapter.number,
                    "title": chapter.title,
                    "pages": [
                        {"url": item.url, "width": item.width, "height": item.height}
                        for item in pages
                    ],
                }
            )
        return {
            "manga": manga_payload,
            "chapters": chapter_payloads,
            "expected_version": state.expected_version,
        }
