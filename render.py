"""Best50 图片渲染：用 Playwright + Chromium 把 HTML 截图成 PNG。

原项目用 PIL 逐张素材拼图，需要下载整套 ``static`` 资源包。这里改成
HTML + Chromium 渲染，好处是版式用 CSS 描述、不依赖具体素材坐标：

- 配置了 ``static_path`` 且目录存在时，使用其中的字体、背景、曲绘和 logo；
- 未配置时使用内置的渐变样式，仍能出图。

``playwright`` 只在真正渲染时才导入，因此没安装浏览器也不会影响插件加载、
文本查询和单元测试。资源包缺失时自动降级，不影响出图。
"""
from __future__ import annotations

import base64
import html
import os
import tempfile
from pathlib import Path

#: 谱面难度索引，由 0 到 4 依次对应 Basic 到 Re:Master。
LEVEL_NAMES = ("Basic", "Advanced", "Expert", "Master", "Re:Master")

#: 画布逻辑宽度（CSS 像素），实际像素还要乘以 device_scale_factor。
PAGE_WIDTH = 1200
#: 渲染分辨率倍数，2 表示 Retina 级别。
DEFAULT_SCALE = 2
#: 资源包里候选字体的相对路径，按顺序取第一个存在的。
FONT_CANDIDATES = (
    "font/Torus SemiBold.otf",
    "font/ResourceHanRoundedCN-Bold.ttf",
    "font/ShangguMonoSC-Regular.otf",
)
#: 难度配色，索引对应 level_index（Basic 到 Re:Master）。
DIFFICULTY_COLORS = ("#69c46d", "#f0b23c", "#ef5f5f", "#a163f0", "#d07be0")
#: 成绩评级配色，键为水鱼返回的小写 rate。
RATE_COLORS = {
    "sssp": "#ffd166", "sss": "#ffd166", "ssp": "#ffd166",
    "ss": "#f4a261", "sp": "#f4a261",
    "s": "#7ec8e3", "a": "#7ec8e3", "aa": "#7ec8e3", "aaa": "#7ec8e3",
}
_FONT_MIME = {".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2"}


def playwright_available() -> bool:
    """探测 Playwright 是否可导入；不触发浏览器下载或启动。"""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        return False
    return True


class RenderUnavailable(RuntimeError):
    """Playwright 或 Chromium 不可用，本次及后续渲染应直接降级。"""


def _read_data_uri(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    mime = _FONT_MIME.get(path.suffix.lower())
    if mime is None:
        mime = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


class StaticAssets:
    """原项目 ``static`` 资源包的只读视图，缺失的素材返回 ``None``。

    实例在插件生命周期内复用，字体、背景、logo 与曲绘的 base64 只读一次。
    """

    def __init__(self, static_path: str = "") -> None:
        root = Path(static_path).expanduser() if static_path else None
        self.root = root if root is not None and root.is_dir() else None
        self._cache: dict[Path, str | None] = {}

    @property
    def available(self) -> bool:
        return self.root is not None

    def _uri(self, path: Path) -> str | None:
        if not path.is_file():
            return None
        if path not in self._cache:
            self._cache[path] = _read_data_uri(path)
        return self._cache[path]

    def font_uri(self) -> str | None:
        if self.root is None:
            return None
        for relative in FONT_CANDIDATES:
            uri = self._uri(self.root / relative)
            if uri:
                return uri
        return None

    def background_uri(self, theme: str) -> str | None:
        if self.root is None:
            return None
        return self._uri(self.root / "mai" / "pic" / theme / "b50.png")

    def logo_uri(self, theme: str) -> str | None:
        if self.root is None:
            return None
        return self._uri(self.root / "mai" / "pic" / theme / "logo.png")

    def cover_uri(self, song_id) -> str | None:
        if self.root is None:
            return None
        try:
            name = int(song_id) % 10000
        except (TypeError, ValueError):
            return None
        cover = self.root / "mai" / "cover"
        uri = self._uri(cover / f"{name}.png")
        return uri or self._uri(cover / "0.png")


def _difficulty_name(level_index) -> str:
    if type(level_index) is int and 0 <= level_index < len(LEVEL_NAMES):
        return LEVEL_NAMES[level_index].upper()
    return "UNKNOWN"


def _difficulty_color(level_index) -> str:
    if type(level_index) is int and 0 <= level_index < len(DIFFICULTY_COLORS):
        return DIFFICULTY_COLORS[level_index]
    return "#8a8f98"


def _score_text(achievements) -> str:
    try:
        return f"{float(achievements):.4f}%"
    except (TypeError, ValueError):
        return "-"


def _total(charts) -> int:
    total = 0
    for chart in charts:
        rating = chart.get("ra") if isinstance(chart, dict) else None
        if type(rating) in (int, float):
            total += int(rating)
    return total


def _charts(data) -> tuple[list, list]:
    charts = data.get("charts") if isinstance(data, dict) else None
    if not isinstance(charts, dict):
        return [], []
    sd = charts.get("sd")
    dx = charts.get("dx")
    return (sd if isinstance(sd, list) else [], dx if isinstance(dx, list) else [])


def _chart_row(position: int, chart: dict, assets: StaticAssets) -> str:
    level_index = chart.get("level_index")
    color = _difficulty_color(level_index)
    title = html.escape(str(chart.get("title") or "?"))
    level = html.escape(str(chart.get("level") or ""))
    difficulty = _difficulty_name(level_index)
    rate = str(chart.get("rate") or "").lower()
    marks = " · ".join(
        html.escape(str(chart.get(key) or "").upper()) for key in ("fc", "fs")
    ).strip(" ·")
    rate_text = html.escape(rate.upper())
    rate_color = RATE_COLORS.get(rate, "#c8ccd4")
    cover = assets.cover_uri(chart.get("song_id"))
    if cover:
        cover_html = f'<img class="cover" src="{cover}" alt="">'
    else:
        cover_html = f'<span class="cover empty" style="--diff:{color}"></span>'
    mark_html = f'<span class="mark">{marks}</span>' if marks else ""
    return f"""      <li class="song">
        <span class="idx">{position}</span>
        {cover_html}
        <span class="body">
          <span class="title">{title}</span>
          <span class="sub">
            <span class="diff" style="--diff:{color}">{difficulty} {level}</span>
            <span class="rate" style="color:{rate_color}">{rate_text}</span>
            {mark_html}
          </span>
        </span>
        <span class="score">
          <span class="achv">{_score_text(chart.get("achievements"))}</span>
          <span class="ra">Ra {html.escape(str(chart.get("ra", 0)))}</span>
        </span>
      </li>"""


def _section(title: str, subtitle: str, charts: list, assets: StaticAssets) -> str:
    rows = "\n".join(_chart_row(index, chart, assets) for index, chart in enumerate(charts, 1))
    if not rows:
        rows = '      <li class="empty-tip">（无成绩）</li>'
    return f"""    <section class="col">
      <h2>{html.escape(title)}<span class="subtitle">{html.escape(subtitle)}</span></h2>
      <ol class="songs">
{rows}
      </ol>
    </section>"""


def build_best50_html(data: dict, *, assets: StaticAssets | None = None,
                      theme: str = "prism_plus", bot_name: str = "helper") -> str:
    """把 Best50 数据排版成一张自包含的 HTML 页面。"""
    assets = assets or StaticAssets()
    sd, dx = _charts(data)
    nickname = html.escape(str(data.get("nickname") or "未知"))
    rating = html.escape(str(data.get("rating", "-")))
    course_rank = html.escape(str(data.get("additional_rating", "-")))
    plate = html.escape(str(data.get("plate") or "-"))

    font_face = ""
    font_uri = assets.font_uri()
    if font_uri:
        font_face = ("@font-face{font-family:'MaiFont';"
                     f"src:url('{font_uri}');font-display:swap;}}")

    background_uri = assets.background_uri(theme)
    background = (f"background-image:linear-gradient(rgba(12,14,24,.72),rgba(12,14,24,.86)),"
                  f"url('{background_uri}');background-size:cover;background-position:center;"
                  if background_uri else
                  "background-image:linear-gradient(160deg,#1b2033,#0c0e18 60%);")

    logo_uri = assets.logo_uri(theme)
    logo_html = f'<img class="logo" src="{logo_uri}" alt="">' if logo_uri else '<span class="logo empty"></span>'
    footer = (f"Designed by Yuri-YuzuChaN &amp; BlueDeer233. "
              f"Data from DIVING-FISH. Generated by {html.escape(bot_name)} BOT")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Best50</title>
<style>
{font_face}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{width:{PAGE_WIDTH}px;{background}color:#eef1f7;
  font-family:'MaiFont','Microsoft YaHei','PingFang SC',system-ui,sans-serif;}}
.page{{padding:22px 24px 18px}}
.header{{display:flex;align-items:center;gap:18px;
  background:rgba(10,12,20,.55);border:1px solid rgba(255,255,255,.12);
  border-radius:16px;padding:16px 20px;margin-bottom:16px}}
.logo{{width:180px;height:86px;object-fit:contain}}
.logo.empty{{display:inline-block;width:6px}}
.who{{flex:1}}
.name{{font-size:30px;font-weight:700;letter-spacing:1px}}
.rating{{font-size:22px;color:#9db4ff;margin-top:4px}}
.meta{{text-align:right;font-size:16px;line-height:1.7;color:#c8ccd4}}
.meta b{{color:#ffd166;font-weight:600}}
.cols{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.col{{background:rgba(10,12,20,.5);border:1px solid rgba(255,255,255,.1);
  border-radius:14px;padding:12px 14px}}
.col h2{{font-size:18px;display:flex;justify-content:space-between;align-items:baseline;
  padding-bottom:8px;margin-bottom:6px;border-bottom:1px solid rgba(255,255,255,.12)}}
.subtitle{{font-size:13px;font-weight:400;color:#9aa3b2}}
.songs{{list-style:none}}
.song{{display:grid;grid-template-columns:26px 40px 1fr auto;gap:8px;align-items:center;
  padding:4px 0;border-bottom:1px solid rgba(255,255,255,.05)}}
.idx{{font-size:13px;color:#7d8698;text-align:right}}
.cover{{width:40px;height:40px;border-radius:8px;object-fit:cover;background:#2a2f45}}
.cover.empty{{display:block;border:2px solid var(--diff);
  background:linear-gradient(135deg,color-mix(in srgb,var(--diff) 30%,transparent),transparent)}}
.title{{font-size:15px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;display:block}}
.sub{{display:flex;align-items:center;gap:6px;margin-top:3px;font-size:12px}}
.diff{{color:#0c0e18;background:var(--diff);border-radius:5px;padding:1px 6px;font-weight:700}}
.rate{{font-weight:700}}
.mark{{color:#aab3c5}}
.score{{text-align:right;line-height:1.35}}
.achv{{display:block;font-size:15px;font-weight:600}}
.ra{{display:block;font-size:12px;color:#9db4ff}}
.empty-tip{{padding:14px 0;color:#7d8698;font-size:14px}}
.footer{{margin-top:14px;text-align:center;font-size:12px;color:#7d8698}}
</style>
</head>
<body>
<div class="page">
  <div class="header">
    {logo_html}
    <div class="who">
      <div class="name">{nickname}</div>
      <div class="rating">Rating {rating}</div>
    </div>
    <div class="meta">段位 <b>{course_rank}</b><br>牌子 <b>{plate}</b></div>
  </div>
  <div class="cols">
{_section("B35", f"旧曲 · 合计 RA {_total(sd)}", sd, assets)}
{_section("B15", f"新曲 · 合计 RA {_total(dx)}", dx, assets)}
  </div>
  <div class="footer">{footer}</div>
</div>
</body>
</html>"""


def render_best50_png(data: dict, *, assets: StaticAssets | None = None,
                      theme: str = "prism_plus", bot_name: str = "helper",
                      timeout: float = 30, scale: float = DEFAULT_SCALE) -> bytes:
    """渲染并返回 PNG 字节。需要已安装 Playwright 与 Chromium。"""
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except Exception as error:  # pragma: no cover - 取决于本机是否安装
        raise RenderUnavailable("未安装 Playwright") from error

    document = build_best50_html(data, assets=assets, theme=theme, bot_name=bot_name)
    handle, name = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(document)
        page_url = Path(name).as_uri()
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.launch(
                    headless=True, args=["--no-sandbox", "--allow-file-access-from-files"]
                )
            except PlaywrightError as error:
                raise RenderUnavailable(str(error)) from error
            try:
                page = browser.new_page(
                    viewport={"width": PAGE_WIDTH, "height": 900},
                    device_scale_factor=scale,
                )
                page.goto(page_url, wait_until="load", timeout=timeout * 1000)
                page.wait_for_timeout(150)
                return page.screenshot(full_page=True)
            finally:
                browser.close()
    finally:
        Path(name).unlink(missing_ok=True)
