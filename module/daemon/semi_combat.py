import cv2
import numpy as np

from module.base.timer import Timer
from module.daemon.assets import (
    MAIN_STORY_MAP_CLOSE,
    MAIN_STORY_MARK_IN,
    MAIN_STORY_MARK_OUT,
    MAIN_STORY_NEW_CHAPTER,
    MAIN_STORY_NORMAL,
)
from module.daemon.daemon_base import DaemonBase
from module.event.assets import FIELD_CHANGE
from module.handler.assets import CONFIRM_B
from module.logger import logger
from module.simulation_room.assets import AUTO_BURST, AUTO_SHOOT, END_FIGHTING, FIGHT, PAUSE
from module.tribe_tower.assets import NEXT_STAGE
from module.ui.assets import FIGHT_QUICKLY_ENABLE, SKIP
from module.ui.ui import UI

MAIN_STORY_MARK_CLICK_OFFSET = (0, 130)
# 红圈怪只有点到小队下方的红圈（中间是紫色六边形）才会接战，叹号下方 130 只能点到小队。
# 以下距离和面积都以叹号模板原始尺寸为单位，使用时乘以匹配缩放：实测普通小怪在叹号下方约 75~105，
# 红圈中心约 220；小队和光柱区域的紫色面积不到 100，红圈区域约 3000。
RED_CIRCLE_SEARCH_AREA = (-60, 150, 60, 300)
RED_CIRCLE_PURPLE_LOWER = (140, 50, 110)
RED_CIRCLE_PURPLE_UPPER = (176, 140, 210)
RED_CIRCLE_PURPLE_MIN_AREA = 800
# 章节列表里 NEW CHAPTER 标识正下方就是新章节按钮
MAIN_STORY_NEW_CHAPTER_CLICK_OFFSET = (0, 45)
# 野外没有叹号的小怪靠脚下红圈识别。红圈常被怪物身体遮断成几段弧，先膨胀合并再按椭圆的尺寸筛选；
# 通知红点、小地图红三角都远小于红圈。只在资源栏和底部返回栏之间找，避免点到 UI。
# 红圈有偏红和偏橙两种，饱和度也不一样
ENEMY_RING_HSV_RANGES = (((0, 80, 140), (18, 255, 255)), ((170, 80, 140), (180, 255, 255)))
ENEMY_RING_SEARCH_Y = (60, 1110)
ENEMY_RING_MERGE_KERNEL = (25, 13)
ENEMY_RING_MIN_PIXELS = 120
ENEMY_RING_WIDTH = (45, 260)
ENEMY_RING_MIN_HEIGHT = 12
ENEMY_RING_MIN_RATIO = 1.2
# 镜头跟随小队，小队在画面中部但不一定正中，用脚下蓝圈定位；蓝圈被队员挡成几段，取蓝色像素中位数。
# 队员的头发、衣服会被当成红圈，排除小队周围：队员站在蓝圈上方，所以往上排除得多。
MAIN_STORY_SQUAD_CENTER = (360, 640)
MAIN_STORY_SQUAD_SEARCH = (160, 440, 560, 840)
MAIN_STORY_SQUAD_HSV_RANGE = ((90, 80, 200), (112, 255, 255))
MAIN_STORY_SQUAD_MIN_PIXELS = 40
MAIN_STORY_SQUAD_EXCLUDE = (100, 120, 40)
# 连续点怪这么多次仍没进战斗，视为点不到（被地形挡住等），暂停点怪改走叹号
MAIN_STORY_ENEMY_MAX_CLICKS = 3
MAIN_STORY_ENEMY_PAUSE = 30


class SemiCombat(UI, DaemonBase):
    def main_story_mark_click_offset(self):
        """
        在 MAIN_STORY_MARK_IN 匹配成功后调用，叹号下方是红圈怪时点红圈中心，否则点叹号下方。

        Returns:
            tuple[int, int]: 相对 MAIN_STORY_MARK_IN.location 的点击偏移
        """
        button = MAIN_STORY_MARK_IN
        scale = button.match_scale
        x1, y1 = button.button[:2]
        w, h = button.area[2] - button.area[0], button.area[3] - button.area[1]
        center_x, center_y = x1 + w * scale / 2, y1 + h * scale / 2

        image = self.device.image
        height, width = image.shape[:2]
        left, top, right, bottom = RED_CIRCLE_SEARCH_AREA
        left = max(int(center_x + left * scale), 0)
        top = max(int(center_y + top * scale), 0)
        right = min(int(center_x + right * scale), width)
        bottom = min(int(center_y + bottom * scale), height)
        if right - left < 2 or bottom - top < 2:
            return MAIN_STORY_MARK_CLICK_OFFSET

        hsv = cv2.cvtColor(np.ascontiguousarray(image[top:bottom, left:right]), cv2.COLOR_RGB2HSV)
        purple = cv2.inRange(hsv, RED_CIRCLE_PURPLE_LOWER, RED_CIRCLE_PURPLE_UPPER)
        ys, xs = np.nonzero(purple)
        purple_area = len(xs) / scale / scale
        if purple_area < RED_CIRCLE_PURPLE_MIN_AREA:
            return MAIN_STORY_MARK_CLICK_OFFSET

        target_x, target_y = left + xs.mean(), top + ys.mean()
        location_x, location_y = button.location
        logger.info(
            f'Main story red circle enemy at ({target_x:.0f}, {target_y:.0f}), '
            f'purple area {purple_area:.0f}, scale {scale:.2f}'
        )
        return int(target_x - location_x), int(target_y - location_y)

    @staticmethod
    def main_story_squad_position(hsv):
        """
        Returns:
            tuple[int, int]: 小队蓝圈位置，找不到时返回画面中心
        """
        left, top, right, bottom = MAIN_STORY_SQUAD_SEARCH
        mask = cv2.inRange(hsv[top:bottom, left:right], *MAIN_STORY_SQUAD_HSV_RANGE)
        ys, xs = np.nonzero(mask)
        if len(xs) < MAIN_STORY_SQUAD_MIN_PIXELS:
            return MAIN_STORY_SQUAD_CENTER
        return left + int(np.median(xs)), top + int(np.median(ys))

    def main_story_nearest_enemy(self):
        """
        Returns:
            tuple[int, int] | None: 离小队最近的红圈怪物坐标，没有时返回 None
        """
        hsv = cv2.cvtColor(self.device.image, cv2.COLOR_RGB2HSV)
        squad_x, squad_y = self.main_story_squad_position(hsv)
        exclude_x, exclude_up, exclude_down = MAIN_STORY_SQUAD_EXCLUDE
        mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for lower, upper in ENEMY_RING_HSV_RANGES:
            mask |= cv2.inRange(hsv, lower, upper)
        top, bottom = ENEMY_RING_SEARCH_Y
        mask[:top] = 0
        mask[bottom:] = 0

        merged = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, ENEMY_RING_MERGE_KERNEL))
        count, labels = cv2.connectedComponents(merged)
        enemies = []
        for label in range(1, count):
            ys, xs = np.nonzero((labels == label) & (mask > 0))
            if len(xs) < ENEMY_RING_MIN_PIXELS:
                continue
            width, height = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
            if not ENEMY_RING_WIDTH[0] <= width <= ENEMY_RING_WIDTH[1]:
                continue
            if height < ENEMY_RING_MIN_HEIGHT or width / height < ENEMY_RING_MIN_RATIO:
                continue
            x, y = int((xs.min() + xs.max()) / 2), int((ys.min() + ys.max()) / 2)
            if abs(x - squad_x) <= exclude_x and -exclude_up <= y - squad_y <= exclude_down:
                continue
            enemies.append((x, y))

        if not enemies:
            return None
        return min(enemies, key=lambda point: np.hypot(point[0] - squad_x, point[1] - squad_y))

    def run(self):
        timeout = Timer(600, count=10)
        click_timer = Timer(0.3)
        # CONFIRM_B 是通用的双按钮确认，只在刚点过新章节时点，避免误确认其他弹窗
        new_chapter_confirm_timer = Timer(10)
        # 点怪后给小队走过去的时间
        enemy_timer = Timer(5)
        enemy_pause_timer = Timer(MAIN_STORY_ENEMY_PAUSE)
        enemy_click_count = 0

        while 1:
            self.device.screenshot()

            # 新章节的前置任务提示，确认后前往。弹窗背后压暗的 NEW CHAPTER 仍可能被识别，所以先于下面判断
            if (
                new_chapter_confirm_timer.started()
                and not new_chapter_confirm_timer.reached()
                and click_timer.reached()
                and self.appear_then_click(CONFIRM_B, offset=30, interval=2)
            ):
                new_chapter_confirm_timer.clear()
                click_timer.reset()
                continue

            # 章节推完后弹出章节列表，点新章节
            if (
                self.config.SemiCombat_MainStoryMark
                and click_timer.reached()
                and self.appear(MAIN_STORY_NEW_CHAPTER, offset=30, threshold=0.85, interval=3, static=False)
            ):
                self.device.click(MAIN_STORY_NEW_CHAPTER, MAIN_STORY_NEW_CHAPTER_CLICK_OFFSET)
                logger.info('Main story new chapter')
                new_chapter_confirm_timer.reset()
                click_timer.reset()
                continue

            # 关闭地图
            if (
                self.config.SemiCombat_MainStoryMark
                and click_timer.reached()
                and self.appear_then_click(MAIN_STORY_MAP_CLOSE, offset=30, interval=1)
            ):
                click_timer.reset()
                continue

            # 快速战斗
            if (
                self.config.SemiCombat_FightQuickly
                and click_timer.reached()
                and self.appear_then_click(FIGHT_QUICKLY_ENABLE, threshold=20, interval=2)
            ):
                click_timer.reset()
                continue

            # 进入战斗
            if click_timer.reached() and self.appear_then_click(FIGHT, threshold=20, interval=2):
                enemy_click_count = 0
                click_timer.reset()
                continue

            # 路上没有叹号的小怪，优先于叹号怪
            if (
                self.config.SemiCombat_MainStoryMark
                and click_timer.reached()
                and enemy_timer.reached()
                and enemy_pause_timer.reached()
                and not self.appear(FIGHT_QUICKLY_ENABLE, threshold=20)
                and not self.appear(FIGHT, threshold=20)
                and self.appear(MAIN_STORY_NORMAL, offset=30)
            ):
                enemy = self.main_story_nearest_enemy()
                if enemy is not None and enemy_click_count >= MAIN_STORY_ENEMY_MAX_CLICKS:
                    logger.warning(f'Main story enemy at {enemy} not reachable, fallback to story mark')
                    enemy_click_count = 0
                    enemy_pause_timer.reset()
                elif enemy is not None:
                    logger.info(f'Click main story enemy at {enemy}')
                    self.device.click_xy(*enemy)
                    enemy_click_count += 1
                    enemy_timer.reset()
                    click_timer.reset()
                    continue

            # 主线剧情图标，界面外。刚点过小怪时小队还在走过去，不能被叹号拉走
            if (
                self.config.SemiCombat_MainStoryMark
                and click_timer.reached()
                and enemy_timer.reached()
                and self.appear(MAIN_STORY_NORMAL, offset=30)
                and self.appear_then_click(MAIN_STORY_MARK_OUT, offset=30, threshold=0.85, interval=5, static=False)
            ):
                click_timer.reset()
                continue

            # 主线剧情图标，界面内
            if (
                self.config.SemiCombat_MainStoryMark
                and click_timer.reached()
                and enemy_timer.reached()
                and not self.appear(FIGHT_QUICKLY_ENABLE, threshold=20)
                and not self.appear(FIGHT, threshold=20)
                and self.appear(MAIN_STORY_NORMAL, offset=30)
                and self.appear_with_scale(MAIN_STORY_MARK_IN, scale_range=(0.7, 1.2), interval=5)
            ):
                self.device.click(MAIN_STORY_MARK_IN, self.main_story_mark_click_offset())
                click_timer.reset()
                continue

            # 跳过剧情
            if (
                self.config.SemiCombat_SkipStory
                and click_timer.reached()
                and self.appear_then_click(SKIP, offset=(150, 10), interval=1)
            ):
                click_timer.reset()
                continue

            # 下一关卡
            if click_timer.reached() and self.appear_then_click(NEXT_STAGE, offset=(100, 30), interval=2):
                click_timer.reset()
                continue

            if click_timer.reached() and self.appear_then_click(END_FIGHTING, offset=30):
                click_timer.reset()
                continue

            # 前往区域
            if click_timer.reached() and self.appear_then_click(FIELD_CHANGE, offset=30, interval=1):
                click_timer.reset()
                continue

            # 自动射击
            if click_timer.reached() and self.appear_then_click(AUTO_SHOOT, offset=10, threshold=0.9, interval=5):
                click_timer.reset()
                continue
            if click_timer.reached() and self.appear_then_click(AUTO_BURST, offset=10, threshold=0.9, interval=5):
                click_timer.reset()
                continue

            # 红圈
            if self.config.Optimization_AutoRedCircle and self.appear(PAUSE, offset=10):
                if self.handle_red_circles():
                    click_timer.reset()
                    continue

            if not timeout.started():
                timeout.start()
            if timeout.reached():
                break
            else:
                timeout.clear()


if __name__ == '__main__':
    b = SemiCombat('nkas', task='SemiCombat')
    b.run()
