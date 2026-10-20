"""Best50 图片渲染：移植原版 maimaiDX 的 PIL 拼图实现。

与原版 ``core/image/best50.py`` + ``core/image/base.py`` 保持相同的画布、
素材、坐标与字体，输出 1400×1600 的 PNG，画面与原版一致。

与原版一样，出图依赖 ``static_path`` 指向的静态资源包（背景、字体、评级图、
曲绘等）；未配置资源包时无法出图。本模块只依赖 Pillow。
"""
from __future__ import annotations

from bisect import bisect_right
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------- 常量

#: 评级名到原版素材名的映射，例如 ``sssp`` → ``SSSp``。
RANK_SP = ["d", "c", "b", "bb", "bbb", "a", "aa", "aaa", "s", "sp", "ss", "ssp", "sss", "sssp"]
RANK_MAP = {
    key: (key[:-1].upper() + "p" if key.endswith("p") else key.upper())
    for key in RANK_SP
}

#: FC 类型到素材名的映射。
COMBO_SP = ["fc", "fcp", "ap", "app"]
COMBO_MAP = {
    key: (key.upper()[:-1] + "p" if len(key) > 2 and key.endswith("p") else key.upper())
    for key in COMBO_SP
}

#: SYNC 类型到素材名的映射。
SYNC_MAP = {
    "fs": "FS",
    "fsp": "FSp",
    "fsd": "FSD",
    "fdx": "FSD",
    "fsdp": "FSDp",
    "fdxp": "FSDp",
    "sync": "Sync",
}

#: 各难度的谱面底色素材，索引对应 level_index。
DIFF_BG_NAMES = (
    "b50_score_basic.png",
    "b50_score_advanced.png",
    "b50_score_expert.png",
    "b50_score_master.png",
    "b50_score_remaster.png",
)

#: 各难度的文字颜色，与原版 ``AssetsImage`` 一致。
DIFF_TEXT_COLOR = [
    (255, 255, 255, 255),
    (255, 255, 255, 255),
    (255, 255, 255, 255),
    (255, 255, 255, 255),
    (138, 0, 226, 255),
]
#: 各难度的曲目 ID 颜色。
ID_TEXT_COLOR = [
    (129, 217, 85, 255),
    (245, 189, 21, 255),
    (255, 129, 141, 255),
    (159, 81, 220, 255),
    (138, 0, 226, 255),
]

FONT_SIYUAN = "font/ResourceHanRoundedCN-Bold.ttf"
FONT_TORUS = "font/Torus SemiBold.otf"

#: 原版画布尺寸（背景图大小）。
CANVAS_SIZE = (1400, 1600)


class RenderUnavailable(RuntimeError):
    """缺少静态资源包或字体，无法出图。"""


# ----------------------------------------------------------------- 文本工具


def get_char_width(code_point: int) -> int:
    """按原版 ``get_char_width`` 估算字符占位宽度（半角 1、全角 2）。"""
    widths = [
        (126, 1), (159, 0), (687, 1), (710, 0), (711, 1), (727, 0), (733, 1),
        (879, 0), (1154, 1), (1161, 0), (4347, 1), (4447, 2), (7467, 1),
        (7521, 0), (8369, 1), (8426, 0), (9000, 1), (9002, 2), (11021, 1),
        (12350, 2), (12351, 1), (12438, 2), (12442, 0), (19893, 2), (19967, 1),
        (55203, 2), (63743, 1), (64106, 2), (65039, 1), (65059, 0), (65131, 2),
        (65279, 1), (65376, 2), (65500, 1), (65510, 2), (120831, 1),
        (262141, 2), (1114109, 1),
    ]
    if code_point in (0xE, 0xF):
        return 0
    for limit, width in widths:
        if code_point <= limit:
            return width
    return 1


def column_width(text: str) -> int:
    return sum(get_char_width(ord(char)) for char in text)


def truncate_column(text: str, limit: int) -> str:
    result = 0
    kept = []
    for char in text:
        result += get_char_width(ord(char))
        if result <= limit:
            kept.append(char)
    return "".join(kept)


def dx_star_count(percent: float) -> int:
    """DX 分数百分比对应的星数，与原版 ``utils.calc.dx_score`` 一致。"""
    if percent < 85:
        return 0
    if percent < 90:
        return 1
    if percent < 93:
        return 2
    if percent < 95:
        return 3
    if percent < 97:
        return 4
    return 5


class _DrawText:
    """与原版 ``tools.DrawText`` 相同的最小封装。"""

    def __init__(self, draw: ImageDraw.ImageDraw, font_path: Path) -> None:
        self._draw = draw
        self._font = str(font_path)

    def draw(
        self,
        pos_x: float,
        pos_y: float,
        size: int,
        text,
        color=(255, 255, 255, 255),
        anchor: str = "lt",
        stroke_width: int = 0,
        stroke_fill=(0, 0, 0, 0),
    ) -> None:
        font = ImageFont.truetype(self._font, size)
        self._draw.text(
            (pos_x, pos_y),
            str(text),
            color,
            font,
            anchor,
            stroke_width=stroke_width,
            stroke_fill=stroke_fill,
        )


# --------------------------------------------------------------- 静态资源包


class StaticAssets:
    """``static`` 资源包的只读视图，缺失的素材返回 ``None``。

    实例在插件生命周期内复用，打开过的图片缓存在内存里。
    """

    def __init__(self, static_path: str = "") -> None:
        root = Path(static_path).expanduser() if static_path else None
        self.root = root if root is not None and root.is_dir() else None
        self._cache: dict[Path, Image.Image | None] = {}

    @property
    def available(self) -> bool:
        return self.root is not None

    def _open(self, *parts: str) -> Image.Image | None:
        if self.root is None:
            return None
        path = self.root.joinpath(*parts)
        if path not in self._cache:
            try:
                with Image.open(path) as image:
                    self._cache[path] = image.convert("RGBA")
            except (OSError, ValueError):
                self._cache[path] = None
        return self._cache[path]

    def font_path(self, relative: str) -> Path | None:
        if self.root is None:
            return None
        path = self.root / relative
        return path if path.is_file() else None

    def pic(self, name: str) -> Image.Image | None:
        return self._open("mai", "pic", name)

    def themed(self, theme: str, name: str) -> Image.Image | None:
        return self._open("mai", "pic", theme, name)

    def shougou(self, name: str) -> Image.Image | None:
        return self._open("mai", "shougou", name)

    def plate_version(self, name: str) -> Image.Image | None:
        return self._open("mai", "plate_version", name)

    def cover(self, song_id) -> Image.Image | None:
        try:
            name = int(song_id) % 10000
        except (TypeError, ValueError):
            return None
        return self._open("mai", "cover", f"{name}.png") or self._open("mai", "cover", "0.png")

    def find_plate_by_suffix(self, plate: str) -> Image.Image | None:
        """水鱼只给牌子简称时，用后缀在 ``plate_version`` 里找一张。"""
        if self.root is None or not plate:
            return None
        folder = self.root / "mai" / "plate_version"
        if not folder.is_dir():
            return None
        try:
            names = sorted(path.stem for path in folder.glob("*.png"))
        except OSError:
            return None
        for name in names:
            if name.endswith(plate):
                return self.plate_version(f"{name}.png")
        return None


# ------------------------------------------------------------------- 渲染器


def _as_int(value, default: int = 0) -> int:
    if type(value) is bool:
        return default
    if type(value) is int:
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _type_abbr(value) -> str:
    text = str(value or "").strip()
    lowered = text.lower()
    if lowered == "standard":
        return "SD"
    if lowered == "utage":
        return "DX"
    return text.upper()


def _charts(data: dict) -> tuple[list, list]:
    charts = data.get("charts") if isinstance(data, dict) else None
    if not isinstance(charts, dict):
        return [], []
    sd = charts.get("sd") if isinstance(charts.get("sd"), list) else []
    dx = charts.get("dx") if isinstance(charts.get("dx"), list) else []
    return (
        [chart for chart in sd if isinstance(chart, dict)],
        [chart for chart in dx if isinstance(chart, dict)],
    )


class _PlayerBest50:
    """对应原版 ``PlayerBest50``，逐像素复刻其绘制流程。"""

    def __init__(self, data, assets: StaticAssets, theme: str, bot_name: str, song_db) -> None:
        self.data = data if isinstance(data, dict) else {}
        self.assets = assets
        self.theme = theme
        self.bot_name = bot_name
        self.song_db = song_db

        background = assets.themed(theme, "b50.png")
        if background is None:
            raise RenderUnavailable(f"静态资源包缺少 {theme}/b50.png")
        self._im = background.copy()
        self._draw = ImageDraw.Draw(self._im)

        siyuan = assets.font_path(FONT_SIYUAN)
        torus = assets.font_path(FONT_TORUS)
        if siyuan is None or torus is None:
            raise RenderUnavailable("静态资源包缺少 font/ 字体文件")
        self._sy = _DrawText(self._draw, siyuan)
        self._tb = _DrawText(self._draw, torus)

        self.color = (249, 62, 172, 255) if theme == "circle" else (124, 129, 255, 255)
        self.name = str(self.data.get("nickname") or self.data.get("username") or "未知")
        self.rating = _as_int(self.data.get("rating"))
        self.course_rank = max(_as_int(self.data.get("additional_rating")), 0)
        self.plate = str(self.data.get("plate") or "").strip()
        self.sd, self.dx = _charts(self.data)
        self.sd_total = sum(_as_int(chart.get("ra")) for chart in self.sd)
        self.dx_total = sum(_as_int(chart.get("ra")) for chart in self.dx)

    # ------------------------------------------------------------- 头部素材

    def _find_ra_pic(self) -> str:
        rating = self.rating
        thresholds = [
            (1000, "01"), (2000, "02"), (4000, "03"), (7000, "04"), (10000, "05"),
            (12000, "06"), (13000, "07"), (14000, "08"), (14500, "09"), (15000, "10"),
        ]
        for limit, num in thresholds:
            if rating < limit:
                return f"UI_CMN_DXRating_{num}.png"
        if self.theme == "circle":
            if rating < 16000:
                num = "11"
            elif rating < 17000:
                num = "12"
            else:
                num = "11"
        else:
            num = "11"
        return f"UI_CMN_DXRating_{num}.png"

    def _ra_pic_star(self) -> str:
        thresholds = [14000, 14250, 14500, 14750, 15000, 15250, 15500, 15750, 16000, 16250, 16500, 16750]
        num_map = [1, 2, 1, 2, 1, 2, 3, 4, 1, 2, 3, 4]
        index = bisect_right(thresholds, self.rating) - 1
        index = min(max(index, 0), len(num_map) - 1)
        return f"UI_CMN_DXRating_Star_0{num_map[index]}.png"

    def _plate_image(self) -> Image.Image | None:
        if not self.plate:
            return self.assets.pic("UI_Plate_550101.png")
        image = self.assets.plate_version(f"{self.plate}.png")
        if image is None:
            image = self.assets.find_plate_by_suffix(self.plate)
        return image or self.assets.pic("UI_Plate_550101.png")

    def _draw_header(self) -> None:
        im = self._im
        assets = self.assets

        logo = assets.themed(self.theme, "logo.png")
        if logo is not None:
            im.alpha_composite(logo.resize((249, 120)), (14, 60))

        plate = self._plate_image()
        if plate is not None:
            im.alpha_composite(plate.resize((800, 130)), (300, 60))

        icon = assets.pic("UI_Icon_509506.png")
        if icon is not None:
            im.alpha_composite(icon.resize((120, 120)), (305, 65))

        rank_num = self.course_rank if self.course_rank <= 10 else self.course_rank + 1
        matchlevel = assets.pic(f"UI_DNM_DaniPlate_{rank_num:02d}.png") or assets.pic("UI_DNM_DaniPlate_00.png")
        classlevel = assets.pic("UI_FBR_Class_00.png")

        dx_rating_size = (186, 35)
        dx_rating_star = None
        rating_num_x, rating_num_y, rating_num_gap, rating_num_size = 520, 80, 15, (17, 20)
        if self.theme == "circle" and self.rating >= 14000:
            dx_rating_size = (170, 35)
            star = assets.themed(self.theme, self._ra_pic_star())
            if star is not None:
                dx_rating_star = star.resize((21, 35))
            rating_num_x, rating_num_y, rating_num_gap, rating_num_size = 515, 82, 13, (14, 17)

        ra_pic = assets.themed(self.theme, self._find_ra_pic())
        if ra_pic is not None:
            im.alpha_composite(ra_pic.resize(dx_rating_size), (435, 72))
        if dx_rating_star is not None:
            im.alpha_composite(dx_rating_star, (590, 72))

        for index, digit in enumerate(f"{self.rating:05d}"):
            number = assets.pic(f"UI_NUM_Drating_{digit}.png")
            if number is not None:
                im.alpha_composite(number.resize(rating_num_size), (rating_num_x + rating_num_gap * index, rating_num_y))

        name_image = assets.pic("Name.png")
        if name_image is not None:
            im.alpha_composite(name_image, (435, 115))
        if matchlevel is not None:
            im.alpha_composite(matchlevel.resize((80, 32)), (625, 120))
        if classlevel is not None:
            im.alpha_composite(classlevel.resize((90, 54)), (620, 60))

        trophy_title = f"B35: {self.sd_total} + B15: {self.dx_total} = {self.rating}"
        shougou = assets.shougou("UI_CMN_Shougou_Rainbow.png")
        if shougou is not None:
            im.alpha_composite(shougou.resize((270, 27)), (435, 160))
        self._tb.draw(570, 172, 14, trophy_title, (0, 0, 0, 255), "mm")
        self._sy.draw(445, 135, 20, self.name, (0, 0, 0, 255), "lm")

        footer = (
            "Designed by Yuri-YuzuChaN & BlueDeer233. "
            f"Data from Diving-Fish. Generated by {self.bot_name} BOT"
        )
        self._sy.draw(700, 1570, 22, footer, self.color, "mm", 5, (255, 255, 255, 255))

    # ------------------------------------------------------------- 成绩列表

    def _draw_scores(self, charts: list, dx: bool) -> None:
        assets = self.assets
        theme = self.theme
        im = self._im

        gap = 114
        step_x = 276
        start_x = 16
        initial_y = 1085 if dx else 235

        for index, chart in enumerate(charts):
            row, column = divmod(index, 5)
            x = start_x + column * step_x
            y = initial_y + row * gap

            level_index = _as_int(chart.get("level_index"), 3)
            level_index = min(max(level_index, 0), len(DIFF_BG_NAMES) - 1)

            background = assets.pic(DIFF_BG_NAMES[level_index])
            if background is not None:
                im.alpha_composite(background, (x, y))

            cover = assets.cover(chart.get("song_id"))
            if cover is not None:
                im.alpha_composite(cover.resize((75, 75)), (x + 12, y + 12))

            type_image = assets.pic(f"{_type_abbr(chart.get('type'))}.png")
            if type_image is not None:
                im.alpha_composite(type_image.resize((37, 14)), (x + 51, y + 91))

            rate = str(chart.get("rate") or "")
            if rate:
                rate_name = RANK_MAP.get(rate, rate.upper()) if rate.islower() else rate
                rate_image = assets.themed(theme, f"UI_TTR_Rank_{rate_name}.png")
                if rate_image is not None:
                    im.alpha_composite(rate_image.resize((63, 28)), (x + 92, y + 78))

            fc = chart.get("fc")
            if fc:
                fc_image = assets.pic(f"UI_MSS_MBase_Icon_{COMBO_MAP.get(fc, str(fc).upper())}.png")
                if fc_image is not None:
                    im.alpha_composite(fc_image.resize((34, 34)), (x + 154, y + 77))

            fs = chart.get("fs")
            if fs:
                fs_image = assets.pic(f"UI_MSS_MBase_Icon_{SYNC_MAP.get(fs, str(fs).upper())}.png")
                if fs_image is not None:
                    im.alpha_composite(fs_image.resize((34, 34)), (x + 185, y + 77))

            user_dx = _as_int(chart.get("dxScore"))
            max_dx = self.song_db.max_dx(chart.get("song_id"), level_index) if self.song_db else 0
            if max_dx:
                star = dx_star_count(user_dx / max_dx * 100)
                if star:
                    star_image = assets.pic(f"UI_GAM_Gauge_DXScoreIcon_0{star}.png")
                    if star_image is not None:
                        im.alpha_composite(star_image.resize((47, 26)), (x + 217, y + 80))

            self._tb.draw(x + 26, y + 98, 13, _as_int(chart.get("song_id")), ID_TEXT_COLOR[level_index], "mm")

            title = str(chart.get("title") or "")
            if column_width(title) > 18:
                title = truncate_column(title, 17) + "..."
            self._sy.draw(x + 93, y + 14, 14, title, DIFF_TEXT_COLOR[level_index], "lm")

            achievements = chart.get("achievements")
            try:
                achievements = float(achievements)
            except (TypeError, ValueError):
                achievements = 0.0
            self._tb.draw(x + 93, y + 38, 30, f"{achievements:.4f}%", DIFF_TEXT_COLOR[level_index], "lm")

            if max_dx:
                self._tb.draw(x + 219, y + 65, 15, f"{user_dx}/{max_dx}", DIFF_TEXT_COLOR[level_index], "mm")

            ds = chart.get("ds")
            if type(ds) not in (int, float) and self.song_db is not None:
                ds = self.song_db.level_value(chart.get("song_id"), level_index)
            if type(ds) not in (int, float):
                ds = 0
            self._tb.draw(x + 93, y + 65, 15, f"{ds} -> {_as_int(chart.get('ra'))}", DIFF_TEXT_COLOR[level_index], "lm")

    # --------------------------------------------------------------- 入口

    def draw(self) -> bytes:
        self._draw_header()
        self._draw_scores(self.sd, False)
        self._draw_scores(self.dx, True)
        buffer = BytesIO()
        self._im.save(buffer, "PNG")
        return buffer.getvalue()


def render_best50_png(
    data: dict,
    *,
    assets: StaticAssets | None = None,
    theme: str = "prism_plus",
    bot_name: str = "helper",
    timeout: float = 30,
    song_db=None,
) -> bytes:
    """按原版 maimaiDX 的方式绘制 Best50，返回 PNG 字节。

    ``timeout`` 仅为兼容旧调用保留，PIL 绘制没有超时概念。
    """
    assets = assets or StaticAssets()
    if not assets.available:
        raise RenderUnavailable("未配置 static_path，无法按原版方式绘制 Best50")
    if theme not in ("prism_plus", "circle"):
        theme = "prism_plus"
    return _PlayerBest50(data, assets, theme, bot_name, song_db).draw()
