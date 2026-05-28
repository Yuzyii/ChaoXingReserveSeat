import json
import time
import argparse
import os
import logging
import datetime

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


from utils import reserve, get_user_credentials

get_current_time = lambda action: (
    time.strftime("%H:%M:%S", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%H:%M:%S", time.localtime(time.time()))
)
get_current_dayofweek = lambda action: (
    time.strftime("%A", time.localtime(time.time() + 8 * 3600))
    if action
    else time.strftime("%A", time.localtime(time.time()))
)


SLEEPTIME = 0.2  # 每次抢座的间隔
ENDTIME = "15:01:00"  # 根据学校的预约座位时间+1min即可
STARTTIME = "13:37:03"  # GitHub Actions 场景下，脚本会等待到该时刻再开始抢座（精确到秒）
WAIT_UNTIL_STARTTIME_IN_ACTIONS = True  # 仅在 --action 时生效

ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 5  # 最大尝试次数
RESERVE_NEXT_DAY = False  # 预约明天而不是今天的


def _parse_hhmm(s: str) -> datetime.datetime:
    return datetime.datetime.strptime(s, "%H:%M")


def wait_until_time(target_hms: str, action: bool, check_interval: float = 0.2):
    """
    等待直到 target_hms（格式 HH:MM:SS）。
    注意：GitHub Actions 的 schedule 触发本身只能精确到“分钟”，且启动时间可能有抖动；
    该等待只能保证脚本逻辑在 runner 启动后尽量贴近目标秒执行。
    """
    while get_current_time(action) < target_hms:
        time.sleep(check_interval)


def split_time_ranges(time_cfg, max_hours_per_reserve: float = 5):
    """
    将配置中的 time 转为多个 [start, end] 段。

    支持两种写法：
    1) time: ["08:00", "22:00"]  -> 自动按 max_hours_per_reserve 切分
    2) time: [["08:00", "13:00"], ["13:00", "18:00"]] -> 直接按分段预约
    """
    if not time_cfg:
        return []

    # 显式分段：[[start,end], ...]
    if (
        isinstance(time_cfg, list)
        and len(time_cfg) > 0
        and isinstance(time_cfg[0], (list, tuple))
    ):
        return [[seg[0], seg[1]] for seg in time_cfg]

    # 单段：["08:00","22:00"] -> 自动切分
    if not (isinstance(time_cfg, list) and len(time_cfg) == 2):
        raise ValueError(
            f"time 配置格式不正确：{time_cfg}，应为 ['HH:MM','HH:MM'] 或 [['HH:MM','HH:MM'], ...]"
        )

    start_s, end_s = time_cfg[0], time_cfg[1]
    start_dt, end_dt = _parse_hhmm(start_s), _parse_hhmm(end_s)
    if end_dt <= start_dt:
        raise ValueError(
            f"time 结束时间必须大于开始时间（不支持跨天）：{start_s} -> {end_s}"
        )

    max_minutes = int(max_hours_per_reserve * 60)
    if max_minutes <= 0:
        raise ValueError("max_hours_per_reserve 必须大于 0")

    segs = []
    cur = start_dt
    while cur < end_dt:
        nxt = min(cur + datetime.timedelta(minutes=max_minutes), end_dt)
        segs.append([cur.strftime("%H:%M"), nxt.strftime("%H:%M")])
        cur = nxt
    return segs


def login_and_reserve(users, usernames, passwords, action, success_list=None):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    if action and len(usernames.split(",")) != len(users):
        raise Exception("user number should match the number of config")
    if success_list is None:
        success_list = [None] * len(users)  # 每个用户对应一个分段成功列表
    current_dayofweek = get_current_dayofweek(action)
    for index, user in enumerate(users):
        username = user.get("username", "")
        password = user.get("password", "")
        time_cfg = user.get("time")
        roomid = user.get("roomid")
        seatid = user.get("seatid")
        daysofweek = user.get("daysofweek", [])
        max_hours = user.get("max_hours_per_reserve", 5)

        times_list = split_time_ranges(time_cfg, max_hours_per_reserve=max_hours)
        if isinstance(seatid, str):
            seatid = [seatid]

        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue

        if success_list[index] is None or len(success_list[index]) != len(times_list):
            success_list[index] = [False] * len(times_list)

        # 只对未成功的时间段继续尝试
        if not all(success_list[index]):
            logging.info(
                f"----------- {username} -- {times_list} -- {seatid} try -----------"
            )
            s = reserve(
                sleep_time=SLEEPTIME,
                max_attempt=MAX_ATTEMPT,
                enable_slider=ENABLE_SLIDER,
                reserve_next_day=RESERVE_NEXT_DAY,
            )
            s.get_login_status()
            s.login(username, password)
            s.requests.headers.update({"Host": "office.chaoxing.com"})

            for seg_i, times in enumerate(times_list):
                if success_list[index][seg_i]:
                    continue
                # 每个时间段都重置最大尝试次数，避免前一个时间段消耗完重试次数
                s.max_attempt = MAX_ATTEMPT
                suc = s.submit(times, roomid, seatid, action)
                success_list[index][seg_i] = suc
    return success_list


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"start time {current_time}, action {'on' if action else 'off'}")
    attempt_times = 0
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    success_list = None
    current_dayofweek = get_current_dayofweek(action)

    # 统计当天需要预约的“分段数”（而不是用户数）
    today_reservation_num = 0
    for d in users:
        if current_dayofweek not in d.get("daysofweek", []):
            continue
        max_hours = d.get("max_hours_per_reserve", 5)
        today_reservation_num += len(
            split_time_ranges(d.get("time"), max_hours_per_reserve=max_hours)
        )

    # GitHub Actions：先等到目标秒再开始（避免 15:00:xx 前就开始请求）
    if action and WAIT_UNTIL_STARTTIME_IN_ACTIONS:
        logging.info(f"Waiting until {STARTTIME} to start reserving...")
        wait_until_time(STARTTIME, action, check_interval=0.2)
        current_time = get_current_time(action)

    while current_time < ENDTIME:
        attempt_times += 1
        # try:
        success_list = login_and_reserve(
            users, usernames, passwords, action, success_list
        )
        # except Exception as e:
        #     print(f"An error occurred: {e}")
        reserved_num = sum(sum(u) for u in success_list if isinstance(u, list))
        print(
            f"attempt time {attempt_times}, time now {current_time}, reserved {reserved_num}/{today_reservation_num}, success list {success_list}"
        )
        current_time = get_current_time(action)
        if reserved_num == today_reservation_num:
            print(f"reserved successfully!")
            return


def debug(users, action=False):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    suc = False
    logging.info(f" Debug Mode start! , action {'on' if action else 'off'}")
    if action:
        usernames, passwords = get_user_credentials(action)
    current_dayofweek = get_current_dayofweek(action)

    if action and WAIT_UNTIL_STARTTIME_IN_ACTIONS:
        logging.info(f"Waiting until {STARTTIME} to start debug submit...")
        wait_until_time(STARTTIME, action, check_interval=0.2)

    for index, user in enumerate(users):
        username = user.get("username", "")
        password = user.get("password", "")
        time_cfg = user.get("time")
        roomid = user.get("roomid")
        seatid = user.get("seatid")
        daysofweek = user.get("daysofweek", [])
        max_hours = user.get("max_hours_per_reserve", 5)

        times_list = split_time_ranges(time_cfg, max_hours_per_reserve=max_hours)
        if isinstance(seatid, str):
            seatid = [seatid]

        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        if current_dayofweek not in daysofweek:
            logging.info("Today not set to reserve")
            continue
        logging.info(
            f"----------- {username} -- {times_list} -- {seatid} try -----------"
        )
        s = reserve(
            sleep_time=SLEEPTIME,
            max_attempt=MAX_ATTEMPT,
            enable_slider=ENABLE_SLIDER,
            reserve_next_day=RESERVE_NEXT_DAY,
        )
        s.get_login_status()
        s.login(username, password)
        s.requests.headers.update({"Host": "office.chaoxing.com"})
        success_seg = []
        for times in times_list:
            s.max_attempt = MAX_ATTEMPT
            suc = s.submit(times, roomid, seatid, action)
            success_seg.append(suc)
        logging.info(f"{username} segments result: {success_seg}")


def get_roomid(args1, args2):
    username = input("请输入用户名：")
    password = input("请输入密码：")
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username=username, password=password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    encode = input("请输入deptldEnc：")
    s.roomid(encode)


if __name__ == "__main__":
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    parser = argparse.ArgumentParser(prog="Chao Xing seat auto reserve")
    parser.add_argument("-u", "--user", default=config_path, help="user config file")
    parser.add_argument(
        "-m",
        "--method",
        default="reserve",
        choices=["reserve", "debug", "room"],
        help="for debug",
    )
    parser.add_argument(
        "-a",
        "--action",
        action="store_true",
        help="use --action to enable in github action",
    )
    args = parser.parse_args()
    func_dict = {"reserve": main, "debug": debug, "room": get_roomid}
    with open(args.user, "r+") as data:
        usersdata = json.load(data)["reserve"]
    func_dict[args.method](usersdata, args.action)
