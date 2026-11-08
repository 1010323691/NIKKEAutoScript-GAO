import cv2
import numpy as np

from module.base.timer import Timer
from module.daily.assets import RECEIVE
from module.daemon.assets import (
    MAIN_STORY_FIGHT_FAILED_BACK,
    MAIN_STORY_HARD,
    MAIN_STORY_MAP_CLOSE,
    MAIN_STORY_MARK_IN,
    MAIN_STORY_MARK_OUT,
    MAIN_STORY_MINIMAP_OPEN,
    MAIN_STORY_NEW_CHAPTER,
    MAIN_STORY_NEW_CHAPTER_HARD,
    MAIN_STORY_NORMAL,
    MAIN_STORY_UNLOCK,
)
from module.daemon.daemon_base import DaemonBase
from module.daemon.field_explore import FieldExplorer
from module.event.assets import FIELD_CHANGE
from module.handler.assets import CONFIRM_B
from module.logger import logger
from module.simulation_room.assets import AUTO_BURST, AUTO_SHOOT, END_FIGHTING, FIGHT, PAUSE
from module.tribe_tower.assets import NEXT_STAGE, OPERATION_FAILED
from module.ui.assets import DAILY_CHECK, DAILY_GOTO_MAIN, FIGHT_QUICKLY_ENABLE, SKIP
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
# 红圈有偏红和偏橙两种，饱和度也不一样；雪地上的红圈偏淡粉，饱和度只有 40~100。
# 偏橙那段不能同样放宽，否则会把队员的发色和小队连成一片
ENEMY_RING_HSV_RANGES = (((0, 80, 140), (18, 255, 255)), ((160, 40, 150), (180, 255, 255)))
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
# 推图时任务完成会弹出任务页。全部领取后要等奖励动画播完再关，否则点 X 会被动画吞掉
MISSION_RECEIVE_SETTLE = 2.5
MISSION_RECEIVE_MAX_CLICKS = 3
MISSION_POPUP_TIMEOUT = 30
# 野外靠小地图自主找怪（困难图没有叹号）。点地面后等小队停下再规划下一步：
# 点击到起步有延迟，先等一会再判断是否停下，连续两帧没位移算停下
MAIN_STORY_EXPLORE_MOVE_TIMEOUT = 8
MAIN_STORY_EXPLORE_MOVE_START = 1.0
MAIN_STORY_EXPLORE_STILL_FRAMES = 2


class SemiCombat(UI, DaemonBase):
    explorer: FieldExplorer = None
    explore_finished = False

    def in_main_story_field(self):
        return self.appear(MAIN_STORY_HARD, offset=30) or self.appear(MAIN_STORY_NORMAL, offset=30)

    def main_story_explore_wait(self):
        """
        点地面后等小队走完：位置连续不变、离开野外（接战面板、剧情）或超时
        """
        start_timer = Timer(MAIN_STORY_EXPLORE_MOVE_START).start()
        timeout = Timer(MAIN_STORY_EXPLORE_MOVE_TIMEOUT).start()
        last = None
        still = 0
        while 1:
            self.device.screenshot()
            if timeout.reached():
                logger.info('Main story explore move timeout')
                break
            if not self.in_main_story_field():
                logger.info('Main story explore left field')
                break
            if not self.explorer.observe(self.device.image):
                break
            if not start_timer.reached():
                continue
            pos = self.explorer.squad_canvas
            if last is not None and np.hypot(pos[0] - last[0], pos[1] - last[1]) < 2:
                still += 1
                if still >= MAIN_STORY_EXPLORE_STILL_FRAMES:
                    break
            else:
                still = 0
            last = pos
        self.explorer.report_move()

    def main_story_explore(self):
        """
        野外自主找怪：小地图关着先打开，否则规划一步、点地面并等小队走完。

        Returns:
            bool: 是否执行了操作，区域内没有怪也没有未探索边界时返回 False
        """
        if self.appear_then_click(MAIN_STORY_MINIMAP_OPEN, offset=(10, 10), interval=2):
            return True
        if self.explorer is None:
            self.explorer = FieldExplorer()
        if not self.explorer.observe(self.device.image):
            return False
        hsv = cv2.cvtColor(self.device.image, cv2.COLOR_RGB2HSV)
        plan = self.explorer.plan(self.main_story_squad_position(hsv))
        if plan is None:
            if not self.explore_finished:
                logger.hr('Main story explore finished', 2)
                logger.info(f'Enemies skipped: {[tuple(e) for e in self.explorer.enemies]}')
                self.explore_finished = True
            return False
        self.explore_finished = False
        point, _ = plan
        self.device.click_xy(*point)
        self.main_story_explore_wait()
        return True

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

    def handle_mission_popup(self):
        """
        推图中弹出任务页时，全部领取奖励后点右上角 X 关闭，直到任务页消失。

        Returns:
            bool: 是否处理了任务页
        """
        if not self.appear(DAILY_CHECK, offset=(10, 10)):
            return False

        logger.hr('Main story mission popup', 2)
        settle_timer = Timer(MISSION_RECEIVE_SETTLE, count=2).start()
        # 关闭后可能还有奖励弹窗或过场，任务页连续消失一段时间才算关掉
        closed_timer = Timer(1, count=2).start()
        click_timer = Timer(0.3)
        timeout = Timer(MISSION_POPUP_TIMEOUT).start()
        receive_count = 0
        skip_first_screenshot = True
        while 1:
            if skip_first_screenshot:
                skip_first_screenshot = False
            else:
                self.device.screenshot()

            if timeout.reached():
                logger.warning('Main story mission popup not closed, timeout')
                return True

            if (
                click_timer.reached()
                and receive_count < MISSION_RECEIVE_MAX_CLICKS
                and self.appear_then_click(RECEIVE, offset=(5, 5), interval=2, static=False)
            ):
                receive_count += 1
                settle_timer.reset()
                closed_timer.reset()
                click_timer.reset()
                continue

            if click_timer.reached() and self.handle_reward(1):
                settle_timer.reset()
                closed_timer.reset()
                click_timer.reset()
                continue

            if self.appear(DAILY_CHECK, offset=(10, 10)):
                closed_timer.reset()
                # 没关掉就继续点 X
                if (
                    settle_timer.reached()
                    and click_timer.reached()
                    and self.appear_then_click(DAILY_GOTO_MAIN, offset=(10, 10), interval=2)
                ):
                    click_timer.reset()
                continue

            if closed_timer.reached():
                logger.info('Main story mission popup closed')
                self.device.click_record_clear()
                return True

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

            # 任务页会盖住整个画面，先处理
            if self.handle_mission_popup():
                timeout.clear()
                click_timer.reset()
                continue

            # 推图解锁新功能的提示，点任意处继续。背后压暗的野外仍能匹配到，必须先于找怪处理
            if click_timer.reached() and self.appear_then_click(MAIN_STORY_UNLOCK, offset=(30, 30), interval=2):
                click_timer.reset()
                continue

            # 战败：返回野外，跳过这个目标。右下角的重新挑战和 FIGHT 位置重叠，必须先于 FIGHT 判断
            if self.appear(OPERATION_FAILED, offset=(30, 30)):
                if click_timer.reached() and self.appear_then_click(
                    MAIN_STORY_FIGHT_FAILED_BACK, offset=(30, 30), interval=2
                ):
                    logger.info('Main story fight failed')
                    if self.explorer is not None:
                        self.explorer.skip_target()
                    click_timer.reset()
                continue

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

            # 章节推完后弹出章节列表，点新章节。困难模式的标识是深底白字，单独一个模板
            new_chapter = None
            if self.config.SemiCombat_MainStoryMark and click_timer.reached():
                for button in (MAIN_STORY_NEW_CHAPTER, MAIN_STORY_NEW_CHAPTER_HARD):
                    if self.appear(button, offset=30, threshold=0.85, interval=3, static=False):
                        new_chapter = button
                        break
            if new_chapter is not None:
                self.device.click(new_chapter, MAIN_STORY_NEW_CHAPTER_CLICK_OFFSET)
                logger.info('Main story new chapter')
                # 换章节后是新地图
                self.explorer = None
                new_chapter_confirm_timer.reset()
                click_timer.reset()
                continue

            # 关闭地图。野外要靠小地图找怪，不关
            if (
                self.config.SemiCombat_MainStoryMark
                and click_timer.reached()
                and not self.in_main_story_field()
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

            # 野外自主找怪：先清完当前区域的怪和未探索边界，再走下面的叹号推主线。
            # 困难图没有叹号，只靠这里
            if (
                self.config.SemiCombat_MainStoryMark
                and click_timer.reached()
                and not self.appear(FIGHT_QUICKLY_ENABLE, threshold=20)
                and not self.appear(FIGHT, threshold=20)
                and self.in_main_story_field()
                and self.main_story_explore()
            ):
                timeout.clear()
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
