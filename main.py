import json
import time
import argparse
import os
import logging
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

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


SLEEPTIME = 0.05  # 每次抢座的间隔，进一步降低到0.01
ENDTIME = "15:01:00"  # 根据学校的预约座位时间+1min即可
STARTTIME = "15:00:00"  # GitHub Actions 场景下，脚本会等待到该时刻再开始抢座（精确到秒）
WAIT_UNTIL_STARTTIME_IN_ACTIONS = True  # 仅在 --action 时生效
PRE_LOGIN_BEFORE_START = 20  # 在STARTTIME前多少秒开始预热登录（秒）

ENABLE_SLIDER = True  # 是否有滑块验证
MAX_ATTEMPT = 20  # 最大尝试次数，从5提升到20
RESERVE_NEXT_DAY = False  # 预约明天而不是今天的


def _parse_hhmm(s: str) -> datetime.datetime:
    return datetime.datetime.strptime(s, "%H:%M")


def wait_until_time(target_hms: str, action: bool, check_interval: float = 0.2):
    """
    等待直到 target_hms（格式 HH:MM:SS）。
    注意：GitHub Actions 的 schedule 触发本身只能精确到"分钟"，且启动时间可能有抖动；
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


def _create_and_login(username, password):
    """创建reserve实例并完成登录，可用于预热"""
    s = reserve(
        sleep_time=SLEEPTIME,
        max_attempt=MAX_ATTEMPT,
        enable_slider=ENABLE_SLIDER,
        reserve_next_day=RESERVE_NEXT_DAY,
    )
    s.get_login_status()
    s.login(username, password)
    s.requests.headers.update({"Host": "office.chaoxing.com"})
    return s


def reserve_single_user(user, username, password, action, current_dayofweek, prebuilt_session=None):
    """
    为单个用户执行预约（包含登录和所有时间段）。
    返回 (index, success_list)
    """
    time_cfg = user.get("time")
    roomid = user.get("roomid")
    seatid = user.get("seatid")
    daysofweek = user.get("daysofweek", [])
    max_hours = user.get("max_hours_per_reserve", 5)

    times_list = split_time_ranges(time_cfg, max_hours_per_reserve=max_hours)
    if isinstance(seatid, str):
        seatid = [seatid]

    if current_dayofweek not in daysofweek:
        logging.info(f"{username}: Today not set to reserve")
        return None

    logging.info(
        f"----------- {username} -- {times_list} -- {seatid} try -----------"
    )

    if prebuilt_session is not None:
        s = prebuilt_session
    else:
        s = _create_and_login(username, password)

    success_seg = [False] * len(times_list)

    if len(times_list) == 1:
        s.max_attempt = MAX_ATTEMPT
        success_seg[0] = s.submit(times_list[0], roomid, seatid, action)
    else:
        seg_workers = min(len(times_list), 5)
        with ThreadPoolExecutor(max_workers=seg_workers) as seg_executor:
            seg_futures = {}
            for seg_i, times in enumerate(times_list):
                s_seg = s if seg_i == 0 else _create_and_login(username, password)
                s_seg.max_attempt = MAX_ATTEMPT
                seg_futures[
                    seg_executor.submit(s_seg.submit, times, roomid, seatid, action)
                ] = seg_i
            for seg_future in as_completed(seg_futures):
                seg_i = seg_futures[seg_future]
                try:
                    success_seg[seg_i] = seg_future.result()
                except Exception as e:
                    logging.error(f"Segment {seg_i} failed: {e}")
                    success_seg[seg_i] = False

    logging.info(f"{username} segments result: {success_seg}")
    return success_seg


def pre_login_all_users(users, usernames, passwords, action):
    """在STARTTIME之前，提前为所有用户完成登录（预热）"""
    current_dayofweek = get_current_dayofweek(action)
    sessions = [None] * len(users)

    def _do_pre_login(index):
        user = users[index]
        username = user.get("username", "")
        password = user.get("password", "")
        if action:
            username, password = (
                usernames.split(",")[index],
                passwords.split(",")[index],
            )
        daysofweek = user.get("daysofweek", [])
        if current_dayofweek not in daysofweek:
            return index, None
        try:
            s = _create_and_login(username, password)
            return index, s
        except Exception as e:
            logging.warning(f"Pre-login user {index} failed: {e}")
            return index, None

    with ThreadPoolExecutor(max_workers=min(len(users), 5)) as executor:
        futures = [executor.submit(_do_pre_login, i) for i in range(len(users))]
        for future in as_completed(futures):
            idx, sess = future.result()
            sessions[idx] = sess
    return sessions


def login_and_reserve(users, usernames, passwords, action, success_list=None, prebuilt_sessions=None):
    logging.info(
        f"Global settings: \nSLEEPTIME: {SLEEPTIME}\nENDTIME: {ENDTIME}\nENABLE_SLIDER: {ENABLE_SLIDER}\nRESERVE_NEXT_DAY: {RESERVE_NEXT_DAY}"
    )
    if action and len(usernames.split(",")) != len(users):
        raise Exception("user number should match the number of config")
    if success_list is None:
        success_list = [None] * len(users)

    current_dayofweek = get_current_dayofweek(action)

    with ThreadPoolExecutor(max_workers=min(len(users), 5)) as executor:
        future_to_index = {}
        for index, user in enumerate(users):
            username = user.get("username", "")
            password = user.get("password", "")

            if action:
                username, password = (
                    usernames.split(",")[index],
                    passwords.split(",")[index],
                )

            if success_list[index] is not None and all(success_list[index]):
                continue

            pre_sess = prebuilt_sessions[index] if prebuilt_sessions else None
            future = executor.submit(
                reserve_single_user,
                user,
                username,
                password,
                action,
                current_dayofweek,
                pre_sess,
            )
            future_to_index[future] = index

        for future in as_completed(future_to_index):
            index = future_to_index[future]
            try:
                result = future.result()
                if result is not None:
                    success_list[index] = result
            except Exception as e:
                logging.error(f"User {index} reservation failed: {e}")

    return success_list


def main(users, action=False):
    current_time = get_current_time(action)
    logging.info(f"start time {current_time}, action {'on' if action else 'off'}")
    attempt_times = 0
    usernames, passwords = None, None
    if action:
        usernames, passwords = get_user_credentials(action)
    success_list = None
    prebuilt_sessions = None
    current_dayofweek = get_current_dayofweek(action)

    today_reservation_num = 0
    for d in users:
        if current_dayofweek not in d.get("daysofweek", []):
            continue
        max_hours = d.get("max_hours_per_reserve", 5)
        today_reservation_num += len(
            split_time_ranges(d.get("time"), max_hours_per_reserve=max_hours)
        )

    if action and WAIT_UNTIL_STARTTIME_IN_ACTIONS:
        start_dt = datetime.datetime.strptime(STARTTIME, "%H:%M:%S")
        pre_login_dt = start_dt - datetime.timedelta(seconds=PRE_LOGIN_BEFORE_START)
        pre_login_time = pre_login_dt.strftime("%H:%M:%S")

        logging.info(f"Waiting until {pre_login_time} to pre-login users...")
        wait_until_time(pre_login_time, action, check_interval=0.2)
        logging.info("Start pre-login all users (warm up)...")
        prebuilt_sessions = pre_login_all_users(users, usernames, passwords, action)
        logging.info(f"Pre-login done. Waiting until {STARTTIME} to start reserving...")
        wait_until_time(STARTTIME, action, check_interval=0.01)
        current_time = get_current_time(action)

    while current_time < ENDTIME:
        attempt_times += 1
        success_list = login_and_reserve(
            users, usernames, passwords, action, success_list,
            prebuilt_sessions if attempt_times == 1 else None
        )
        if attempt_times == 1:
            prebuilt_sessions = None
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

    prebuilt_sessions = None
    if action and WAIT_UNTIL_STARTTIME_IN_ACTIONS:
        start_dt = datetime.datetime.strptime(STARTTIME, "%H:%M:%S")
        pre_login_dt = start_dt - datetime.timedelta(seconds=PRE_LOGIN_BEFORE_START)
        pre_login_time = pre_login_dt.strftime("%H:%M:%S")

        logging.info(f"Waiting until {pre_login_time} to pre-login users...")
        wait_until_time(pre_login_time, action, check_interval=0.2)
        logging.info("Start pre-login all users (warm up)...")
        prebuilt_sessions = pre_login_all_users(users, usernames, passwords, action)
        logging.info(f"Pre-login done. Waiting until {STARTTIME} to start debug submit...")
        wait_until_time(STARTTIME, action, check_interval=0.01)

    with ThreadPoolExecutor(max_workers=min(len(users), 5)) as executor:
        futures = []
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

            pre_sess = prebuilt_sessions[index] if prebuilt_sessions else None
            future = executor.submit(
                _debug_single_user,
                username,
                password,
                times_list,
                roomid,
                seatid,
                action,
                pre_sess,
            )
            futures.append((future, username))

        for future, username in futures:
            try:
                success_seg = future.result()
                logging.info(f"{username} segments result: {success_seg}")
            except Exception as e:
                logging.error(f"{username} debug failed: {e}")


def _debug_single_user(username, password, times_list, roomid, seatid, action, prebuilt_session=None):
    """debug 模式下单个用户的预约逻辑"""
    if prebuilt_session is not None:
        s = prebuilt_session
    else:
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
    if len(times_list) == 1:
        s.max_attempt = MAX_ATTEMPT
        success_seg.append(s.submit(times_list[0], roomid, seatid, action))
    else:
        seg_workers = min(len(times_list), 5)
        with ThreadPoolExecutor(max_workers=seg_workers) as seg_executor:
            seg_futures = []
            for seg_i, times in enumerate(times_list):
                s_seg = s if seg_i == 0 else _create_and_login(username, password)
                s_seg.max_attempt = MAX_ATTEMPT
                seg_futures.append(
                    seg_executor.submit(s_seg.submit, times, roomid, seatid, action)
                )
            for sf in seg_futures:
                success_seg.append(sf.result())
    return success_seg


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
