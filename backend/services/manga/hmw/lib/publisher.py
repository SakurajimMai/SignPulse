from __future__ import annotations

import asyncio
import inspect
import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from .config import UploaderConfig
from .image_pipeline import convert_image
from .models import SourceDiscovery, TaskState, UploadedObject
from .state import TaskStateStore


class PublishWorkflow:
    def __init__(
        self,
        config: UploaderConfig,
        api,
        storage,
        state_store: TaskStateStore,
        *,
        temp_root: str | Path,
        image_converter=convert_image,
        progress: Callable | None = None,
    ):
        self.config = config
        self.api = api
        self.storage = storage
        self.state_store = state_store
        self.temp_root = Path(temp_root)
        self.image_converter = image_converter
        self.progress = progress

    async def _progress(self, stage: str, current: int, total: int, message: str) -> None:
        if self.progress is None:
            return
        result = self.progress(stage, current, total, message)
        if inspect.isawaitable(result):
            await result

    async def _confirm(self, callback, plan: dict) -> bool:
        result = callback(plan)
        return bool(await result) if inspect.isawaitable(result) else bool(result)

    def _load_or_create(self, task_id: str, source: SourceDiscovery, slug: str) -> TaskState:
        path = self.state_store.path_for(task_id)
        if path.exists():
            return self.state_store.load(task_id)
        state = TaskState(
            task_id=task_id,
            source_path=str(source.root),
            manga_slug=slug,
            manga_payload={},
            chapter_mappings=[
                {
                    "directory": str(chapter.directory),
                    "number": chapter.number,
                    "title": chapter.title,
                    "action": chapter.action,
                }
                for chapter in source.chapters
            ],
        )
        self.state_store.save(state)
        return state

    async def execute(
        self,
        source: SourceDiscovery,
        manga: dict,
        *,
        confirm,
        task_id: str | None = None,
    ) -> TaskState:
        task_id = task_id or uuid4().hex
        slug = str(manga["slug"])
        state = self._load_or_create(task_id, source, slug)
        if not state.manga_payload:
            state.manga_payload = dict(manga)
            self.state_store.save(state)
        if state.stage == "cleaned":
            return state

        cover_key = self.storage.cover_key(slug)
        manga_payload = {**manga, "cover_url": self.storage.public_url(cover_key)}
        active_chapters = [chapter for chapter in source.chapters if chapter.action != "skip"]

        if state.stage not in {"uploaded", "committed"}:
            preflight_payload = {
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
            state.preflight = await self.api.preflight(preflight_payload)
            state.expected_version = state.preflight.get("expected_version")
            state.stage = "preflighted"
            state.error = ""
            self.state_store.save(state)
            await self._progress("preflighted", 1, 1, "预检查完成")
            if not await self._confirm(confirm, state.preflight):
                state.stage = "cancelled"
                self.state_store.save(state)
                return state
            state.stage = "confirmed"
            self.state_store.save(state)

            try:
                converted = await self._convert_all(source, active_chapters, task_id)
                state.stage = "converted"
                self.state_store.save(state)
                await self._upload_all(state, converted)
                state.stage = "uploaded"
                self.state_store.save(state)
            except Exception as exc:
                state.error = str(exc)
                self.state_store.save(state)
                raise

        await self._verify_urls(state)
        publish_payload = self._publish_payload(manga_payload, active_chapters, state)
        await self._progress("committing", 0, 1, "正在提交主站数据")
        try:
            state.publish_result = await self.api.publish(publish_payload)
        except Exception as exc:
            state.stage = "uploaded"
            state.error = str(exc)
            self.state_store.save(state)
            raise

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

    async def _convert_all(self, source, chapters, task_id):
        task_dir = self.temp_root / task_id
        total = 1 + sum(len(chapter.images) for chapter in chapters)
        current = 0
        converted = []
        await self._progress("converting", 0, total, f"正在转换图片：0/{total}")
        cover = await asyncio.to_thread(
            self.image_converter,
            source.cover,
            task_dir / "cover.avif",
            quality=self.config.avif_quality,
            background=self.config.avif_background,
        )
        converted.append(("cover", None, None, self.storage.cover_key(source.root.name), cover))
        current += 1
        await self._progress("converting", current, total, "封面转换完成")

        for chapter in chapters:
            for index, image in enumerate(chapter.images, start=1):
                filename = f"{index:04d}.avif"
                result = await asyncio.to_thread(
                    self.image_converter,
                    image,
                    task_dir / f"chapter-{chapter.number:g}" / filename,
                    quality=self.config.avif_quality,
                    background=self.config.avif_background,
                )
                converted.append(("page", chapter.number, index, filename, result))
                current += 1
                await self._progress("converting", current, total, f"转换 {chapter.title} {filename}")
        return converted

    async def _upload_all(self, state: TaskState, converted) -> None:
        completed = {item.key: item for item in state.uploaded_objects if item.completed}
        slug = state.manga_slug
        total = len(converted)
        completed_count = sum(
            1
            for kind, chapter_number, _page_index, name, _result in converted
            if (
                self.storage.cover_key(slug)
                if kind == "cover"
                else self.storage.chapter_key(slug, chapter_number, name)
            )
            in completed
        )
        await self._progress(
            "uploading",
            completed_count,
            total,
            f"正在上传对象：{completed_count}/{total}",
        )
        current = completed_count
        for kind, chapter_number, page_index, name, result in converted:
            key = (
                self.storage.cover_key(slug)
                if kind == "cover"
                else self.storage.chapter_key(slug, chapter_number, name)
            )
            if key in completed:
                continue
            url = await asyncio.to_thread(self.storage.upload_file, result.destination, key)
            state.uploaded_objects.append(
                UploadedObject(
                    source=str(result.source),
                    local_path=str(result.destination),
                    key=key,
                    url=url,
                    width=result.width,
                    height=result.height,
                    kind=kind,
                    chapter_number=chapter_number,
                    page_index=page_index,
                )
            )
            self.state_store.save(state)
            current += 1
            await self._progress("uploading", current, total, f"上传 {name}")

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

        async def worker() -> None:
            nonlocal completed
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    valid = await asyncio.to_thread(self.storage.verify_public_url, item.url)
                    if not valid:
                        raise RuntimeError(f"公开 URL 无法访问：{item.url}")
                    completed += 1
                    await self._progress(
                        "verifying",
                        completed,
                        total,
                        f"正在校验公开 URL：{completed}/{total}",
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

    def _publish_payload(self, manga: dict, chapters, state: TaskState) -> dict:
        cover = next((item for item in state.uploaded_objects if item.kind == "cover"), None)
        if cover is None:
            raise RuntimeError("任务缺少已上传封面")
        manga_payload = {**manga, "cover_url": cover.url}
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
