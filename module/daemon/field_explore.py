import cv2
import numpy as np

from module.logger import logger

# 野外左上角小地图的内框（屏幕坐标）。小地图视野跟着镜头走，小队圈基本固定在中间偏上
MINIMAP_AREA = (24, 105, 292, 373)
MINIMAP_SIZE = MINIMAP_AREA[2] - MINIMAP_AREA[0]
# 被放大镜、关闭按钮、搜索计数挡住的部分（小地图局部坐标），既不算可走也不算墙
MINIMAP_OVERLAYS = ((0, 0, 50, 50), (212, 0, 268, 50), (166, 212, 268, 268))
# 内框四周约 7px 是暗色边框，当成墙会把可走区域和视野外隔开，导致找不到未探索边界
MINIMAP_BORDER = 8
MINIMAP_SQUAD = (134, 129)
MINIMAP_SQUAD_SEARCH = 30
MINIMAP_SQUAD_RADIUS = 18
MINIMAP_WALK_HSV = ((95, 120, 150), (115, 255, 255))
MINIMAP_ENEMY_HSV = (((160, 70, 150), (180, 255, 255)), ((0, 70, 150), (8, 255, 255)))
MINIMAP_WHITE_HSV = ((0, 0, 200), (180, 60, 255))
# 红三角约 10px；更小的是贴着视野边缘只露出一角的，位置不准，等走近再算
MINIMAP_ENEMY_MIN_AREA = 25
# EX 关卡是带叹号的空心大三角（约 50px 宽），普通怪最宽约 26px。EX 要手动操作，挂机打不过，不打
MINIMAP_EX_MIN_WIDTH = 40
# 小地图是斜视的透视图，镜头平移时远处挪得少、近处挪得多，不能直接按平移拼接。
# 地面网格两组线的消失点连成地平线（小地图局部坐标下的直线 l·(x, y, 1) = 0），
# 把它送到无穷远就得到仿射校正后的俯视图，此时镜头平移就是纯平移。由多帧网格线拟合。
MINIMAP_HORIZON = (-3.4869e-04, 5.3018e-03, 1.0)

# 全局地图：把每帧校正后的小地图按位置拼起来。-1 未知、0 墙、1 可走
CANVAS_SIZE = 3000
UNKNOWN, BLOCKED, WALK = -1, 0, 1
# 两次观测之间视野最多平移的距离（校正后像素）
LOCATE_RANGE = 100
# 匹配一致率低于这个值视为换了区域（过场、切区域），重建地图
LOCATE_MIN_RATIO = 0.85
# 寻路在降采样的网格上做
PLAN_SCALE = 2
FRONTIER_MIN_CELLS = 3
# 敌人位置合并距离、目标放弃后屏蔽的半径（校正后像素）
ENEMY_MERGE_DISTANCE = 14
TARGET_BLOCK_RADIUS = 16
# 同一个敌人点了这么多次还在，多半是打不过或点不到，跳过
ENEMY_MAX_ATTEMPTS = 3
# 寻路绕开 EX 的半径（校正后像素），路过时走进 EX 范围会弹出接战
EX_AVOID_RADIUS = 14
# 连续这么多次移动没有位移，放弃当前目标
TARGET_MAX_STUCK = 2
# 目标保持：上一个目标还在候选里（位置会随地图补全挪动几格）就继续追，
# 除非最近的新目标路径长度不到它的一半
TARGET_KEEP_RADIUS = 24
TARGET_SWITCH_RATIO = 0.5
# 连续这么多次规划，到目标的路径（网格）都没缩短这么多，视为被挡住（比如关着的能量门）
TARGET_MAX_NO_PROGRESS = 3
TARGET_PROGRESS_MIN = 4

# 小地图原始像素到屏幕像素的比例，由小地图红三角和屏幕上敌人位置对照拟合。小地图比主镜头更接近俯视，
# 竖直方向压缩得少，所以竖直比例更小
MINIMAP_TO_SCREEN = (3.9, 2.7)
# 点地面的安全区：避开顶部资源栏、小地图、任务条和底部按钮
SCREEN_CLICK_AREA = (40, 400, 680, 1110)
# 单步最远走多远（校正后像素），太远时点击位置误差大，也容易越过拐角
STEP_MAX_DISTANCE = 60
# 路径长度最多是直线距离的这么多倍，超过说明中间隔着墙
STEP_MAX_DETOUR = 1.6
# 离小队太近的点会点到小队自己身上，小队不会动
STEP_MIN_DISTANCE = 16


def _rectify_matrix():
    l1, l2, l3 = MINIMAP_HORIZON
    # 以小队处为 1:1，远处放大、近处缩小
    w0 = l1 * MINIMAP_SQUAD[0] + l2 * MINIMAP_SQUAD[1] + l3
    matrix = np.array([[w0, 0, 0], [0, w0, 0], [l1, l2, l3]], dtype=np.float64)
    corners = np.array([[[0, 0], [MINIMAP_SIZE, 0], [0, MINIMAP_SIZE], [MINIMAP_SIZE, MINIMAP_SIZE]]], np.float64)
    warped = cv2.perspectiveTransform(corners, matrix)[0]
    size = int(np.ceil(warped[:, 0].max())) + 1, int(np.ceil(warped[:, 1].max())) + 1
    return matrix, size


RECTIFY_MATRIX, RECTIFY_SIZE = _rectify_matrix()
RECTIFY_INVERSE = np.linalg.inv(RECTIFY_MATRIX)


def rectify_point(point):
    return tuple(cv2.perspectiveTransform(np.array([[point]], np.float64), RECTIFY_MATRIX)[0, 0])


def unrectify_point(point):
    return tuple(cv2.perspectiveTransform(np.array([[point]], np.float64), RECTIFY_INVERSE)[0, 0])


def _warp(mask):
    return cv2.warpPerspective(mask.astype(np.uint8), RECTIFY_MATRIX, RECTIFY_SIZE, flags=cv2.INTER_NEAREST) > 0


class Minimap:
    def __init__(self, image):
        x1, y1, x2, y2 = MINIMAP_AREA
        hsv = cv2.cvtColor(np.ascontiguousarray(image[y1:y2, x1:x2]), cv2.COLOR_RGB2HSV)
        walk = cv2.inRange(hsv, *MINIMAP_WALK_HSV) > 0
        enemy = np.zeros(walk.shape, dtype=np.uint8)
        for lower, upper in MINIMAP_ENEMY_HSV:
            enemy |= cv2.inRange(hsv, lower, upper)
        white = cv2.inRange(hsv, *MINIMAP_WHITE_HSV) > 0

        b = MINIMAP_BORDER
        inside = np.zeros(walk.shape, dtype=bool)
        inside[b:-b, b:-b] = True
        valid = inside.copy()
        for ox1, oy1, ox2, oy2 in MINIMAP_OVERLAYS:
            valid[oy1:oy2, ox1:ox2] = False
        enemy[~valid] = 0

        # 原始小地图坐标下的小队位置，换算屏幕坐标时用
        self.squad_raw = self._squad(white)
        enemies_raw, ex_raw = [], []
        # EX 是空心大三角，会断成几段，先膨胀合并
        merged = cv2.dilate(enemy, np.ones((3, 3), dtype=np.uint8))
        count, labels, stats, _ = cv2.connectedComponentsWithStats(merged)
        for index in range(1, count):
            ys, xs = np.nonzero((labels == index) & (enemy > 0))
            if len(xs) < MINIMAP_ENEMY_MIN_AREA:
                continue
            center = (float(xs.mean()), float(ys.mean()))
            if stats[index][cv2.CC_STAT_WIDTH] >= MINIMAP_EX_MIN_WIDTH:
                ex_raw.append(center)
            else:
                enemies_raw.append(center)

        # 红三角和小队圈画在可走区域上面，盖住的部分按可走处理
        cover = cv2.dilate(enemy, np.ones((5, 5), dtype=np.uint8))
        cv2.circle(cover, self.squad_raw, MINIMAP_SQUAD_RADIUS, 1, -1)
        cover = cover > 0
        # 小地图关着或被弹窗盖住时几乎没有蓝色
        self.is_open = walk.mean() > 0.05

        self.walk = _warp((walk | cover) & valid)
        self.blocked = _warp(~walk & ~cover & ~white & valid)
        self.valid = _warp(valid)
        # 视野内但被图标挡住的部分，不能当成未探索区域
        self.hidden = _warp(inside) & ~self.valid
        self.squad = tuple(int(round(v)) for v in rectify_point(self.squad_raw))
        self.enemies = [tuple(int(round(v)) for v in rectify_point(p)) for p in enemies_raw]
        self.ex = [tuple(int(round(v)) for v in rectify_point(p)) for p in ex_raw]

    @staticmethod
    def _squad(white):
        cx, cy = MINIMAP_SQUAD
        r = MINIMAP_SQUAD_SEARCH
        ys, xs = np.nonzero(white[cy - r:cy + r, cx - r:cx + r])
        if len(xs) < 10:
            return MINIMAP_SQUAD
        return cx - r + int(np.mean(xs)), cy - r + int(np.mean(ys))


class FieldExplorer:
    """
    野外自主找怪：把校正后的小地图拼成全局地图，在已知区域里寻路，先去打见过的红三角，
    没有就去最近的未探索边界，直到既没有怪也没有边界。困难图没有叹号指引，只能靠它。
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = np.full((CANVAS_SIZE, CANVAS_SIZE), UNKNOWN, dtype=np.int8)
        # 当前视野（校正后）左上角在全局地图上的位置
        self.pos = None
        self.minimap = None
        # [x, y, attempts]
        self.enemies = []
        self.ex = []
        self.blocked_targets = []
        self.target = None
        self.target_stuck = 0
        self.target_best = 0
        self.target_no_progress = 0
        self.last_click_pos = None

    @property
    def squad_canvas(self):
        return self.pos[0] + self.minimap.squad[0], self.pos[1] + self.minimap.squad[1]

    def _locate(self, minimap):
        x0, y0 = self.pos
        r = LOCATE_RANGE
        width, height = RECTIFY_SIZE
        top, left = y0 - r, x0 - r
        if top < 0 or left < 0 or top + height + 2 * r > CANVAS_SIZE or left + width + 2 * r > CANVAS_SIZE:
            return None
        window = self.state[top:top + height + 2 * r, left:left + width + 2 * r]
        cw = (window == WALK).astype(np.float32)
        cb = (window == BLOCKED).astype(np.float32)
        tw = minimap.walk.astype(np.float32)
        tb = minimap.blocked.astype(np.float32)
        agree = cv2.matchTemplate(cw, tw, cv2.TM_CCORR) + cv2.matchTemplate(cb, tb, cv2.TM_CCORR)
        disagree = cv2.matchTemplate(cw, tb, cv2.TM_CCORR) + cv2.matchTemplate(cb, tw, cv2.TM_CCORR)
        score = agree - 2 * disagree
        _, _, _, (dx, dy) = cv2.minMaxLoc(score)
        a, d = agree[dy, dx], disagree[dy, dx]
        ratio = a / max(a + d, 1)
        if a + d < minimap.valid.sum() * 0.2 or ratio < LOCATE_MIN_RATIO:
            logger.info(f'Field explore lost position, match ratio {ratio:.2f}, overlap {a + d:.0f}')
            return None
        return left + dx, top + dy

    def observe(self, image):
        """
        Returns:
            bool: 小地图是否可用
        """
        minimap = Minimap(image)
        if not minimap.is_open:
            return False

        pos = None
        if self.pos is not None:
            pos = self._locate(minimap)
            if pos is None:
                self.reset()
        if pos is None:
            width, height = RECTIFY_SIZE
            pos = ((CANVAS_SIZE - width) // 2, (CANVAS_SIZE - height) // 2)
            logger.info('Field explore map created')
        self.pos = pos
        self.minimap = minimap

        x, y = pos
        width, height = RECTIFY_SIZE
        view = self.state[y:y + height, x:x + width]
        view[minimap.blocked] = BLOCKED
        view[minimap.walk] = WALK
        self._update_enemies(minimap)
        return True

    def _update_enemies(self, minimap):
        x, y = self.pos
        width, height = RECTIFY_SIZE
        seen = [(x + ex, y + ey) for ex, ey in minimap.enemies]
        kept = []
        for enemy in self.enemies:
            lx, ly = int(enemy[0] - x), int(enemy[1] - y)
            # 视野内且没被图标挡住，这一帧却没看到，说明已经打掉了
            in_view = 0 <= lx < width and 0 <= ly < height and minimap.valid[ly, lx]
            match = [p for p in seen if np.hypot(p[0] - enemy[0], p[1] - enemy[1]) <= ENEMY_MERGE_DISTANCE]
            if match:
                enemy[0], enemy[1] = match[0]
                seen.remove(match[0])
                kept.append(enemy)
            elif not in_view:
                kept.append(enemy)
        for point in seen:
            kept.append([point[0], point[1], 0])
        self.enemies = kept

        for ex, ey in minimap.ex:
            point = (x + ex, y + ey)
            if all(np.hypot(point[0] - px, point[1] - py) > EX_AVOID_RADIUS for px, py in self.ex):
                logger.info(f'Field explore found EX {point}, skipped')
                self.ex.append(point)

    def _give_up(self, reason):
        logger.warning(f'Field explore give up target {self.target}, {reason}')
        self.blocked_targets.append(self.target)
        self.target = None
        self.target_stuck = 0

    def skip_target(self):
        """
        当前目标打不过（战败），以后不再去
        """
        if self.target is not None:
            self._give_up('fight failed')

    def _is_blocked_target(self, point):
        return any(np.hypot(point[0] - bx, point[1] - by) <= TARGET_BLOCK_RADIUS for bx, by in self.blocked_targets)

    def _grid(self, avoid_ex=True):
        """
        Returns:
            tuple: 降采样后的可走网格、外部未知网格、网格左上角在全局地图上的位置
        """
        known = np.argwhere(self.state != UNKNOWN)
        margin = PLAN_SCALE * 4
        (top, left), (bottom, right) = known.min(axis=0) - margin, known.max(axis=0) + margin
        top, left = max(top, 0), max(left, 0)
        crop = self.state[top:bottom, left:right]
        h, w = crop.shape[0] // PLAN_SCALE, crop.shape[1] // PLAN_SCALE
        crop = crop[:h * PLAN_SCALE, :w * PLAN_SCALE].reshape(h, PLAN_SCALE, w, PLAN_SCALE)
        # 一格里有墙就当墙，避免贴墙走
        walk = (crop == WALK).any(axis=(1, 3)) & ~(crop == BLOCKED).any(axis=(1, 3))
        unknown = (crop == UNKNOWN).all(axis=(1, 3))
        if avoid_ex and self.ex:
            avoid = np.zeros(walk.shape, dtype=np.uint8)
            for ex, ey in self.ex:
                center = (int((ex - left) // PLAN_SCALE), int((ey - top) // PLAN_SCALE))
                cv2.circle(avoid, center, EX_AVOID_RADIUS // PLAN_SCALE, 1, -1)
            # 小队就站在 EX 旁边时不能把起点也挡住
            sx, sy = self.squad_canvas
            cv2.circle(avoid, (int((sx - left) // PLAN_SCALE), int((sy - top) // PLAN_SCALE)), 4, 0, -1)
            walk &= avoid == 0

        # 当前视野里被图标挡住的地方不算未探索
        x, y = self.pos
        hidden = np.zeros(self.state.shape, dtype=bool)
        width, height = RECTIFY_SIZE
        hidden[y:y + height, x:x + width] = self.minimap.hidden
        hidden = hidden[top:top + h * PLAN_SCALE, left:left + w * PLAN_SCALE]
        hidden = hidden.reshape(h, PLAN_SCALE, w, PLAN_SCALE).any(axis=(1, 3))
        unknown &= ~hidden
        # 只算连着地图外面的未知区域，已探索区域中间被图标挡出来的洞不算
        count, labels = cv2.connectedComponents(unknown.astype(np.uint8), connectivity=4)
        outer = set(labels[0, :]) | set(labels[-1, :]) | set(labels[:, 0]) | set(labels[:, -1])
        outer.discard(0)
        unknown = np.isin(labels, list(outer))
        return walk, unknown, (left, top)

    @staticmethod
    def _distance(walk, start):
        dist = np.full(walk.shape, -1, dtype=np.int32)
        reached = np.zeros(walk.shape, dtype=np.uint8)
        reached[start[1], start[0]] = 1
        dist[start[1], start[0]] = 0
        walk_u8 = walk.astype(np.uint8)
        kernel = np.ones((3, 3), dtype=np.uint8)
        step = 0
        while 1:
            step += 1
            grown = cv2.dilate(reached, kernel) & walk_u8 & (1 - reached)
            if not grown.any():
                return dist
            dist[grown > 0] = step
            reached |= grown

    @staticmethod
    def _nearest(mask, point, radius):
        x, y = point
        h, w = mask.shape
        best = None
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h and mask[ny, nx]:
                    d = dx * dx + dy * dy
                    if best is None or d < best[0]:
                        best = (d, (nx, ny))
        return None if best is None else best[1]

    @staticmethod
    def _path(dist, target):
        h, w = dist.shape
        path = [target]
        x, y = target
        while dist[y, x] > 0:
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                nx, ny = x + dx, y + dy
                if 0 <= nx < w and 0 <= ny < h and dist[ny, nx] == dist[y, x] - 1:
                    x, y = nx, ny
                    break
            else:
                break
            path.append((x, y))
        path.reverse()
        return path

    def _candidates(self, unknown, origin, dist):
        """
        Returns:
            list[tuple]: (路径长度, 网格坐标, 类型, 全局坐标)，按路径长度排序，有敌人时只返回敌人
        """
        left, top = origin
        reachable = dist >= 0
        enemies = []
        for enemy in self.enemies:
            if enemy[2] >= ENEMY_MAX_ATTEMPTS or self._is_blocked_target(enemy):
                continue
            # EX 标记被图标或视野边缘切掉一部分时，剩下的碎片会被当成普通红三角
            if any(np.hypot(enemy[0] - ex, enemy[1] - ey) <= EX_AVOID_RADIUS * 1.5 for ex, ey in self.ex):
                continue
            cell = ((enemy[0] - left) // PLAN_SCALE, (enemy[1] - top) // PLAN_SCALE)
            cell = self._nearest(reachable, cell, 5)
            if cell is not None:
                enemies.append((dist[cell[1], cell[0]], cell, 'enemy', (enemy[0], enemy[1])))
        if enemies:
            return sorted(enemies, key=lambda c: c[0])

        # 未探索边界：能走到的格子挨着外部未知区域。
        # 墙边一格会因为降采样被判成墙，放宽到隔一格也算挨着
        border = cv2.dilate(unknown.astype(np.uint8), np.ones((5, 5), dtype=np.uint8)) > 0
        frontier = border & reachable
        frontiers = []
        count, labels, stats, _ = cv2.connectedComponentsWithStats(frontier.astype(np.uint8))
        for index in range(1, count):
            if stats[index][cv2.CC_STAT_AREA] < FRONTIER_MIN_CELLS:
                continue
            ys, xs = np.nonzero(labels == index)
            nearest = np.argmin(dist[ys, xs])
            cell = (int(xs[nearest]), int(ys[nearest]))
            point = (left + cell[0] * PLAN_SCALE, top + cell[1] * PLAN_SCALE)
            if self._is_blocked_target(point):
                continue
            frontiers.append((dist[cell[1], cell[0]], cell, 'frontier', point))
        return sorted(frontiers, key=lambda c: c[0])

    def _is_target(self, point):
        return self.target is not None and np.hypot(
            point[0] - self.target[0], point[1] - self.target[1]
        ) <= TARGET_KEEP_RADIUS

    def _choose(self, candidates):
        """
        选目标并检查进度。每走一步最近的目标都可能换一个，不加保持会在两个目标之间来回跑；
        被能量门挡住时小队在门前来回挪，不会触发原地不动，要看路径长度有没有在缩短。

        Returns:
            tuple | None: 选中的候选，目标因为没有进展被放弃时返回 None
        """
        chosen = candidates[0]
        for candidate in candidates:
            if self._is_target(candidate[3]):
                # 新出现的目标近得多才换
                if chosen[0] >= candidate[0] * TARGET_SWITCH_RATIO:
                    chosen = candidate
                break

        length, _, kind, goal = chosen
        if not self._is_target(goal):
            self.target_stuck = 0
            self.target_best = length
            self.target_no_progress = 0
        elif length <= self.target_best - TARGET_PROGRESS_MIN:
            self.target_best = length
            self.target_no_progress = 0
        else:
            self.target_no_progress += 1
            if self.target_no_progress >= TARGET_MAX_NO_PROGRESS:
                self.target = goal
                self._give_up(f'{kind} path {length * PLAN_SCALE} not getting shorter')
                return None
        self.target = goal
        return chosen

    def _to_screen(self, point, squad_screen):
        # 全局坐标 -> 当前视野校正坐标 -> 原始小地图坐标 -> 屏幕
        rx, ry = unrectify_point((point[0] - self.pos[0], point[1] - self.pos[1]))
        sx, sy = self.minimap.squad_raw
        kx, ky = MINIMAP_TO_SCREEN
        return int(squad_screen[0] + (rx - sx) * kx), int(squad_screen[1] + (ry - sy) * ky)

    @staticmethod
    def _in_click_area(point):
        x1, y1, x2, y2 = SCREEN_CLICK_AREA
        return x1 <= point[0] <= x2 and y1 <= point[1] <= y2

    def plan(self, squad_screen):
        """
        Args:
            squad_screen (tuple[int, int]): 小队在屏幕上的位置

        Returns:
            tuple | None: (屏幕坐标, 类型)，类型为 'enemy' 时点击即接战；None 表示探索完毕
        """
        sx, sy = self.squad_canvas
        while 1:
            # 小队贴着 EX 时绕行区会把出路堵死，这时允许从 EX 旁边穿过去
            for avoid_ex in (True, False):
                walk, unknown, origin = self._grid(avoid_ex=avoid_ex)
                left, top = origin
                start = self._nearest(walk, ((sx - left) // PLAN_SCALE, (sy - top) // PLAN_SCALE), 6)
                if start is None:
                    continue
                dist = self._distance(walk, start)
                candidates = self._candidates(unknown, origin, dist)
                if candidates or not self.ex:
                    break
            else:
                return None
            if not candidates:
                return None
            chosen = self._choose(candidates)
            # 目标刚被放弃，换下一个重新规划
            if chosen is not None:
                break
        _, cell, kind, goal = chosen
        self.last_click_pos = (sx, sy)

        path = self._path(dist, cell)
        path_length = len(path) * PLAN_SCALE

        # 敌人就在单步范围内，直接点敌人。游戏自己会绕路，不要求直线可达
        if (
            kind == 'enemy'
            and path_length <= STEP_MAX_DISTANCE * STEP_MAX_DETOUR
            and np.hypot(goal[0] - sx, goal[1] - sy) <= STEP_MAX_DISTANCE
        ):
            point = self._to_screen(goal, squad_screen)
            if self._in_click_area(point):
                for enemy in self.enemies:
                    if (enemy[0], enemy[1]) == goal:
                        enemy[2] += 1
                logger.info(f'Field explore engage enemy {goal}, squad {(sx, sy)}, click {point}')
                return point, 'enemy'

        # 沿路径取单步范围内最远的点。路径上的点都可走，游戏会自己寻路过去；
        # 路径长度也要限制，否则会点到墙另一边、要绕一大圈才能到的点
        waypoint = None
        max_index = int(STEP_MAX_DISTANCE * STEP_MAX_DETOUR / PLAN_SCALE)
        for px, py in path[:max_index + 1]:
            canvas_point = (left + px * PLAN_SCALE + PLAN_SCALE // 2, top + py * PLAN_SCALE + PLAN_SCALE // 2)
            distance = np.hypot(canvas_point[0] - sx, canvas_point[1] - sy)
            if distance > STEP_MAX_DISTANCE or distance < STEP_MIN_DISTANCE:
                continue
            if self._in_click_area(self._to_screen(canvas_point, squad_screen)):
                waypoint = canvas_point
        if waypoint is None:
            px, py = path[-1]
            waypoint = (left + px * PLAN_SCALE + PLAN_SCALE // 2, top + py * PLAN_SCALE + PLAN_SCALE // 2)

        point = self._to_screen(waypoint, squad_screen)
        x1, y1, x2, y2 = SCREEN_CLICK_AREA
        point = (min(max(point[0], x1), x2), min(max(point[1], y1), y2))
        logger.info(
            f'Field explore {kind} {goal}, path {path_length}, squad {(sx, sy)}, waypoint {waypoint}, click {point}'
        )
        return point, kind

    def report_move(self):
        """
        移动结束后调用：没有位移就累计卡住次数，超过上限放弃当前目标
        """
        if self.last_click_pos is None or self.target is None:
            return
        sx, sy = self.squad_canvas
        moved = np.hypot(sx - self.last_click_pos[0], sy - self.last_click_pos[1])
        self.last_click_pos = None
        if moved >= 3:
            self.target_stuck = 0
            return
        self.target_stuck += 1
        logger.info(f'Field explore no movement towards {self.target}, stuck {self.target_stuck}')
        if self.target_stuck >= TARGET_MAX_STUCK:
            self._give_up('no movement')
