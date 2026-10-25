import time

import numpy as np

from module.base.button import Button
from module.exception import GameClickNoResponseError
from module.logger import logger


def locked_banner_appear(image) -> bool:
    """
    识别点击未解锁功能后弹出的提示横幅：屏幕中部一条贯穿全宽的半透明黑带，中间是白字，
    持续约 2 秒。文字随功能变化，所以不做模板/OCR 匹配，只看外形：

    - 上沿：同一列往下跨过边界后亮度骤降（半透明黑色压暗背景），多数列成立且贯穿全宽
    - 下沿：同样贯穿全宽的骤亮
    - 两条边的间距约为屏幕高度的 13%，带内居中有白字

    背景在上下沿处本身很暗时边界不可见，此时识别不到，属于可接受的漏判。
    """
    if image is None or image.ndim < 2:
        return False
    height, width = image.shape[:2]
    gray = image[:, ::4].astype(np.float32)
    if gray.ndim == 3:
        gray = gray.mean(axis=2)
    gap = 3
    start, end = int(height * 0.3), int(height * 0.7)
    if start < gap or end + gap >= height:
        return False

    rows = np.arange(start, end)
    ratio = (gray[rows + gap] + 1) / (gray[rows - gap] + 1)
    tops = rows[_edge_rows(ratio < 0.7)]
    bottoms = rows[_edge_rows(ratio > 1.4)]
    if not len(tops) or not len(bottoms):
        return False

    min_height, max_height = height * 0.10, height * 0.16
    center = slice(gray.shape[1] // 6, gray.shape[1] * 5 // 6)
    for top in tops:
        for bottom in bottoms:
            if not min_height <= bottom - top <= max_height:
                continue
            band = gray[top + gap + 2:bottom - gap - 2]
            if not band.size:
                continue
            if np.count_nonzero(band[:, center] > 200) < band[:, center].size * 0.005:
                continue
            return True
    return False


def _edge_rows(hit):
    """
    hit: 每行每列是否越过边界。整行多数列成立，且横向切成 6 段每段都有一定比例成立，
    才算一条贯穿全宽的边；两端背景偏暗时边界不明显，所以分段门槛放得比整体低，
    主要用来排除只占中间一部分宽度的面板。
    """
    segments = np.array_split(np.arange(hit.shape[1]), 6)
    weakest = np.min([hit[:, part].mean(axis=1) for part in segments], axis=0)
    return (hit.mean(axis=1) >= 0.6) & (weakest >= 0.2)


class ClickResponse:
    """
    点击无响应检测：点击按钮后短时间内出现未解锁提示横幅，记为该按钮一次命中，
    同一按钮在本任务内命中达到上限即抛 GameClickNoResponseError，由调度器跳过当前任务。

    小号未解锁的功能点了不会跳转，任务循环会一直点同一个入口。原有的点击记录在 PC 端
    不生效，而且它的处理是重启游戏、重跑同一个任务，对未解锁功能没有意义，所以单独检测并跳过。
    """
    # 同一按钮触发横幅的次数上限。不取 1：同样外观的横幅可能也用于其他普通提示，正常流程
    # 点一次出一次后就会离开；未解锁入口会被循环反复点击，第二次命中即可确认。
    click_response_limit = 2
    # 点击后多久内出现的横幅算作这次点击触发的
    click_response_window = 3

    _click_response_name = None
    _click_response_time = 0.
    _click_response_hits = None

    def click_response_record(self, button: Button):
        """在点击完成后调用"""
        self._click_response_name = str(button)
        self._click_response_time = time.time()

    def click_response_check(self):
        """
        在每次截图后调用，此时 self.image 是最新画面

        Raises:
            GameClickNoResponseError:
        """
        name = self._click_response_name
        if name is None:
            return
        if time.time() - self._click_response_time > self.click_response_window:
            self._click_response_name = None
            return
        if not locked_banner_appear(getattr(self, 'image', None)):
            return

        # 同一次点击只记一次，横幅持续期间的后续截图不再重复计数
        self._click_response_name = None
        if self._click_response_hits is None:
            self._click_response_hits = {}
        count = self._click_response_hits.get(name, 0) + 1
        self._click_response_hits[name] = count
        logger.warning(f'Locked banner appeared after clicking {name} ({count}/{self.click_response_limit})')
        if count >= self.click_response_limit:
            self.click_response_clear()
            raise GameClickNoResponseError(f'Click {name} shows locked banner {count} times')

    def click_response_clear(self):
        self._click_response_name = None
        self._click_response_time = 0.
        self._click_response_hits = None
