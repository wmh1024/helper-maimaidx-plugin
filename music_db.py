"""曲目数据：为原版 Best50 出图提供最大 DX 分与谱面定数。

原版出图时用 ``song.difficulties[level_index].dx_score`` 算 DX 星，用
``total_level_value_map`` 取定数。这些数据来自水鱼公开的 ``/music_data``，
插件按需获取并缓存到 ``.runtime/music_data.json``；获取失败时退回旧缓存，
仍拿不到就跳过 DX 星与定数，不影响其余绘制。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

CACHE_TTL_SECONDS = 7 * 24 * 3600


def _build_map(raw) -> dict[tuple[int, int], tuple[int, float | None]]:
    """把 ``/music_data`` 原始列表转成 ``(song_id, level_index) -> (max_dx, ds)``。"""
    result: dict[tuple[int, int], tuple[int, float | None]] = {}
    if not isinstance(raw, list):
        return result
    for song in raw:
        if not isinstance(song, dict):
            continue
        try:
            song_id = int(song.get("id"))
        except (TypeError, ValueError):
            continue
        ds_list = song.get("ds") if isinstance(song.get("ds"), list) else []
        charts = song.get("charts") if isinstance(song.get("charts"), list) else []
        for level_index, chart in enumerate(charts):
            if not isinstance(chart, dict):
                continue
            notes = chart.get("notes")
            total = 0
            if isinstance(notes, (list, tuple)):
                for value in notes:
                    if type(value) in (int, float):
                        total += int(value)
            max_dx = total * 3 if total else 0
            ds = ds_list[level_index] if level_index < len(ds_list) else None
            if type(ds) not in (int, float):
                ds = None
            result[(song_id, level_index)] = (max_dx, ds)
    return result


class SongDatabase:
    """曲目数据的惰性加载与缓存。``fetch`` 是一个返回列表的可调用对象。"""

    def __init__(self, fetch, cache_path=None) -> None:
        self._fetch = fetch
        self.cache_path = Path(cache_path) if cache_path else None
        self._map: dict[tuple[int, int], tuple[int, float | None]] | None = None

    def _ensure(self) -> None:
        if self._map is not None:
            return
        raw = self._read_cache(fresh_only=True)
        if raw is None:
            try:
                raw = self._fetch()
            except Exception:
                raw = self._read_cache(fresh_only=False)
            else:
                self._write_cache(raw)
        self._map = _build_map(raw) if raw else {}

    def _read_cache(self, *, fresh_only: bool):
        if self.cache_path is None or not self.cache_path.is_file():
            return None
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if fresh_only:
            fetched_at = payload.get("fetched_at")
            if type(fetched_at) not in (int, float) or time.time() - fetched_at > CACHE_TTL_SECONDS:
                return None
        data = payload.get("data")
        return data if isinstance(data, list) else None

    def _write_cache(self, raw) -> None:
        if self.cache_path is None:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"fetched_at": time.time(), "data": raw}
            self.cache_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def max_dx(self, song_id, level_index) -> int:
        self._ensure()
        if not self._map:
            return 0
        try:
            key = (int(song_id), int(level_index))
        except (TypeError, ValueError):
            return 0
        entry = self._map.get(key)
        return entry[0] if entry else 0

    def level_value(self, song_id, level_index):
        self._ensure()
        if not self._map:
            return None
        try:
            key = (int(song_id), int(level_index))
        except (TypeError, ValueError):
            return None
        entry = self._map.get(key)
        return entry[1] if entry else None
