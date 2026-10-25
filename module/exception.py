class RequestHumanTakeover(Exception):
    # 请求人工接管
    pass


class CaptchaRequired(RequestHumanTakeover):
    # 登录出现验证码，需人工处理
    pass


class EmulatorNotRunningError(Exception):
    # 模拟器未运行
    pass


class ScriptError(Exception):
    # This is likely to be a mistake of developers, but sometimes a random issue
    pass


class GameTooManyClickError(Exception):
    pass


class GameStuckError(Exception):
    pass


class GameClickNoResponseError(Exception):
    # 点击同一按钮反复弹出未解锁提示横幅，多见于小号，调度器跳过当前任务
    pass


class GameSystemError(Exception):
    pass


class GameServerUnderMaintenance(Exception):
    pass


class GameNotRunningError(Exception):
    pass


class WarehouseStatsScanError(Exception):
    pass


class GameStart(Exception):
    pass


class GamePageUnknownError(Exception):
    pass


class OperationFailed(Exception):
    pass


class AccountError(Exception):
    pass


class ScreenshotError(Exception):
    pass


class ScreenResolutionNotEnough(Exception):
    pass
