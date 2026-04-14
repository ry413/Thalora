#!/usr/bin/python
# coding:utf-8

# @FileName:    liveMan.py
# @Time:        2024/1/2 21:51
# @Author:      bubu
# @Project:     douyinLiveWebFetcher

import codecs
from collections import deque
from datetime import datetime
import gzip
import hashlib
import random
import re
import string
import subprocess
import threading
import time
import execjs
import urllib.parse
from contextlib import contextmanager
from unittest.mock import patch

import requests
import websocket
from py_mini_racer import MiniRacer

from ac_signature import get__ac_signature
from api import sendPrompt
from protobuf.douyin import *

from urllib3.util.url import parse_url

timed_templates = [
    "说3个让用户非买不可的理由",
    "今天直播间有哪些优惠给大家介绍下",
    "三二一上一下链接, 制造一下库存的紧张感",
]

awkward_templates = [
    "现在冷场了, 从你上次冷场聊的话题里选一个继续聊下去",
    "现在冷场了, 从刚才进入直播间的用户里选一个和他聊聊",
    "现在冷场了, 以随机的方式说说今天天气怎么样"
]

DEFAULT_TIMED_CONFIG = {
    "enabled": False,
    "interval": 60,
    "sequential": False,
    "templates": timed_templates,
}

DEFAULT_AWKWARD_CONFIG = {
    "enabled": False,
    "interval": 45,
    "sequential": False,
    "templates": awkward_templates,
}

DEFAULT_CONFIG_JSON = {
    "gift": {"templates": [], "latestCount": 5},
    "like": {"templates": [], "latestCount": 5},
    "basic": {"ignoreMaskedName": False, "ignoreNumericName": False},
    "danmu": {"latestCount": 10, "fixedTemplate": "", "keywordReplies": [], "blockedKeywords": []},
    "timed": DEFAULT_TIMED_CONFIG,
    "follow": {"templates": [], "latestCount": 5},
    "manual": {"latestCount": 10, "fixedTemplate": ""},
    "awkward": dict(DEFAULT_AWKWARD_CONFIG, interruptEnabled=False),
    "welcome": {"templates": [], "latestCount": 5},
}

PROMPT_SOURCE_PRIORITY = {
    "manual": 70,
    "timed": 60,
    "gift": 50,
    "danmu": 40,
    "follow": 30,
    "like": 25,
    "welcome": 20,
    "awkward": 10,
}

def execute_js(js_file: str):
    """
    执行 JavaScript 文件
    :param js_file: JavaScript 文件路径
    :return: 执行结果
    """
    with open(js_file, 'r', encoding='utf-8') as file:
        js_code = file.read()
    
    ctx = execjs.compile(js_code)
    return ctx


@contextmanager
def patched_popen_encoding(encoding='utf-8'):
    original_popen_init = subprocess.Popen.__init__
    
    def new_popen_init(self, *args, **kwargs):
        kwargs['encoding'] = encoding
        original_popen_init(self, *args, **kwargs)
    
    with patch.object(subprocess.Popen, '__init__', new_popen_init):
        yield


def generateSignature(wss, script_file='sign.js'):
    """
    出现gbk编码问题则修改 python模块subprocess.py的源码中Popen类的__init__函数参数encoding值为 "utf-8"
    """
    params = ("live_id,aid,version_code,webcast_sdk_version,"
              "room_id,sub_room_id,sub_channel_id,did_rule,"
              "user_unique_id,device_platform,device_type,ac,"
              "identity").split(',')
    wss_params = urllib.parse.urlparse(wss).query.split('&')
    wss_maps = {i.split('=')[0]: i.split("=")[-1] for i in wss_params}
    tpl_params = [f"{i}={wss_maps.get(i, '')}" for i in params]
    param = ','.join(tpl_params)
    md5 = hashlib.md5()
    md5.update(param.encode())
    md5_param = md5.hexdigest()
    
    with codecs.open(script_file, 'r', encoding='utf8') as f:
        script = f.read()
    
    ctx = MiniRacer()
    ctx.eval(script)
    
    try:
        signature = ctx.call("get_sign", md5_param)
        return signature
    except Exception as e:
        print(e)
    
    # 以下代码对应js脚本为sign_v0.js
    # context = execjs.compile(script)
    # with patched_popen_encoding(encoding='utf-8'):
    #     ret = context.call('getSign', {'X-MS-STUB': md5_param})
    # return ret.get('X-Bogus')


def generateMsToken(length=182):
    """
    产生请求头部cookie中的msToken字段，其实为随机的107位字符
    :param length:字符位数
    :return:msToken
    """
    random_str = ''
    base_str = string.ascii_letters + string.digits + '-_'
    _len = len(base_str) - 1
    for _ in range(length):
        random_str += base_str[random.randint(0, _len)]
    return random_str


class DouyinLiveWebFetcher:

    def __init__(
        self,
        live_id,
        device_id,
        token=None,
        abogus_file='a_bogus.js',
        config_json=None,
        sent_prompt_callback=None,
    ):
        """
        直播间弹幕抓取对象
        :param live_id: 直播间的直播id，打开直播间web首页的链接如：https://live.douyin.com/261378947940，
                        其中的261378947940即是live_id
        :param device_id: 设备ID
        """
        self.abogus_file = abogus_file
        self.__ttwid = None
        self.__room_id = None
        self.session = requests.Session()
        self.live_id = live_id
        self.device_id = device_id
        self.token = token
        self.sent_prompt_callback = sent_prompt_callback
        self.host = "https://www.douyin.com/"
        self.live_url = "https://live.douyin.com/"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
        self.headers = {
            'User-Agent': self.user_agent
        }
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._last_interaction_ts = time.time()
        self._last_silence_push_ts = 0.0
        self._live_state_lock = threading.Lock()
        self._room_status = "unknown"
        self._current_viewer_count = None
        self._total_viewer_count = None
        self._room_stats_updated_at = None
        self._timed_idx = 0
        self._awkward_idx = 0
        self._background_threads = []
        self.config_json = self._normalize_config_json(config_json)
        self._prompt_lock = threading.Lock()
        self._can_send_prompt = True
        self._pending_prompts_by_source = {}
    
    def start(self):
        self._stop_event.clear()
        self._mark_interaction()
        try:
            self.get_room_status()
        except Exception as err:
            print(f"【X】初始化直播间状态失败: {err}")
        self._connectWebSocket()

    
    def stop(self):
        self._stop_event.set()
        ws = getattr(self, "ws", None)
        if ws:
            ws.close()

    def _normalize_schedule_config(self, raw_config, default_config):
        cfg = {
            "enabled": bool(default_config.get("enabled", False)),
            "interval": int(default_config.get("interval", 60)),
            "sequential": bool(default_config.get("sequential", False)),
            "templates": list(default_config.get("templates", [])),
        }
        if not isinstance(raw_config, dict):
            return cfg

        if "enabled" in raw_config:
            cfg["enabled"] = bool(raw_config.get("enabled"))

        interval = raw_config.get("interval")
        if interval is not None:
            try:
                interval = int(interval)
                if interval > 0:
                    cfg["interval"] = interval
            except (TypeError, ValueError):
                pass

        if "sequential" in raw_config:
            cfg["sequential"] = bool(raw_config.get("sequential"))

        templates = raw_config.get("templates")
        if isinstance(templates, list):
            cleaned = [str(item).strip() for item in templates if str(item).strip()]
            cfg["templates"] = cleaned

        return cfg

    def _normalize_latest_config(self, raw_config, default_config):
        cfg = {
            "templates": list(default_config.get("templates", [])),
            "latestCount": int(default_config.get("latestCount", 1)),
        }
        if not isinstance(raw_config, dict):
            return cfg

        templates = raw_config.get("templates")
        if isinstance(templates, list):
            cleaned = [str(item).strip() for item in templates if str(item).strip()]
            cfg["templates"] = cleaned

        latest_count = raw_config.get("latestCount")
        if latest_count is not None:
            try:
                latest_count = int(latest_count)
                if latest_count >= 0:
                    cfg["latestCount"] = latest_count
            except (TypeError, ValueError):
                pass
        return cfg

    def _normalize_config_json(self, raw_config):
        cfg = {
            "gift": self._normalize_latest_config(raw_config.get("gift") if isinstance(raw_config, dict) else None, DEFAULT_CONFIG_JSON["gift"]),
            "like": self._normalize_latest_config(raw_config.get("like") if isinstance(raw_config, dict) else None, DEFAULT_CONFIG_JSON["like"]),
            "basic": dict(DEFAULT_CONFIG_JSON["basic"]),
            "danmu": {
                "latestCount": int(DEFAULT_CONFIG_JSON["danmu"]["latestCount"]),
                "fixedTemplate": str(DEFAULT_CONFIG_JSON["danmu"]["fixedTemplate"]),
                "keywordReplies": list(DEFAULT_CONFIG_JSON["danmu"]["keywordReplies"]),
                "blockedKeywords": list(DEFAULT_CONFIG_JSON["danmu"]["blockedKeywords"]),
            },
            "timed": self._normalize_schedule_config(raw_config.get("timed") if isinstance(raw_config, dict) else None, DEFAULT_CONFIG_JSON["timed"]),
            "follow": self._normalize_latest_config(raw_config.get("follow") if isinstance(raw_config, dict) else None, DEFAULT_CONFIG_JSON["follow"]),
            "manual": dict(DEFAULT_CONFIG_JSON["manual"]),
            "awkward": self._normalize_schedule_config(raw_config.get("awkward") if isinstance(raw_config, dict) else None, DEFAULT_CONFIG_JSON["awkward"]),
            "welcome": self._normalize_latest_config(raw_config.get("welcome") if isinstance(raw_config, dict) else None, DEFAULT_CONFIG_JSON["welcome"]),
        }
        cfg["awkward"]["interruptEnabled"] = bool(DEFAULT_CONFIG_JSON["awkward"].get("interruptEnabled", False))

        if not isinstance(raw_config, dict):
            return cfg

        danmu_raw = raw_config.get("danmu")
        if isinstance(danmu_raw, dict):
            latest_count = danmu_raw.get("latestCount")
            if latest_count is not None:
                try:
                    latest_count = int(latest_count)
                    if latest_count >= 0:
                        cfg["danmu"]["latestCount"] = latest_count
                except (TypeError, ValueError):
                    pass
            fixed_template = danmu_raw.get("fixedTemplate")
            if fixed_template is not None:
                cfg["danmu"]["fixedTemplate"] = str(fixed_template)
            keyword_replies = danmu_raw.get("keywordReplies")
            if isinstance(keyword_replies, list):
                normalized_keyword_replies = []
                for item in keyword_replies:
                    if not isinstance(item, dict):
                        continue
                    keyword = str(item.get("keyword", "")).strip()
                    reply = str(item.get("reply", "")).strip()
                    if not keyword or not reply:
                        continue
                    normalized_keyword_replies.append({
                        "keyword": keyword,
                        "reply": reply,
                    })
                cfg["danmu"]["keywordReplies"] = normalized_keyword_replies
            blocked_keywords = danmu_raw.get("blockedKeywords")
            if isinstance(blocked_keywords, list):
                cfg["danmu"]["blockedKeywords"] = [str(item).strip() for item in blocked_keywords if str(item).strip()]

        basic_raw = raw_config.get("basic")
        if isinstance(basic_raw, dict):
            cfg["basic"]["ignoreMaskedName"] = bool(basic_raw.get("ignoreMaskedName", cfg["basic"]["ignoreMaskedName"]))
            cfg["basic"]["ignoreNumericName"] = bool(basic_raw.get("ignoreNumericName", cfg["basic"]["ignoreNumericName"]))

        manual_raw = raw_config.get("manual")
        if isinstance(manual_raw, dict):
            latest_count = manual_raw.get("latestCount")
            if latest_count is not None:
                try:
                    latest_count = int(latest_count)
                    if latest_count >= 0:
                        cfg["manual"]["latestCount"] = latest_count
                except (TypeError, ValueError):
                    pass
            fixed_template = manual_raw.get("fixedTemplate")
            if fixed_template is not None:
                cfg["manual"]["fixedTemplate"] = str(fixed_template)

        awkward_raw = raw_config.get("awkward")
        if isinstance(awkward_raw, dict) and "interruptEnabled" in awkward_raw:
            cfg["awkward"]["interruptEnabled"] = bool(awkward_raw.get("interruptEnabled"))

        return cfg

    def _mark_interaction(self):
        now = time.time()
        with self._state_lock:
            self._last_interaction_ts = now

    def _pick_template(self, config, idx_attr):
        templates = config.get("templates", [])
        if not templates:
            return None

        if config.get("sequential"):
            with self._state_lock:
                idx = getattr(self, idx_attr)
                prompt = templates[idx % len(templates)]
                setattr(self, idx_attr, idx + 1)
                return prompt

        return random.choice(templates)

    def _render_template(self, template, variables=None):
        if template is None:
            return None
        rendered = str(template)
        if not isinstance(variables, dict):
            return rendered

        for key, value in variables.items():
            rendered = rendered.replace("{" + str(key) + "}", str(value))
        return rendered

    def _build_prompt_from_templates(self, source, variables=None, fallback=None):
        config = self.config_json.get(source, {})

        fixed_template = config.get("fixedTemplate")
        if isinstance(fixed_template, str) and fixed_template.strip():
            prompt = self._render_template(fixed_template.strip(), variables)
            if prompt:
                return prompt

        templates = config.get("templates")
        if isinstance(templates, list):
            cleaned = [str(item).strip() for item in templates if str(item).strip()]
            if cleaned:
                prompt = self._render_template(random.choice(cleaned), variables)
                if prompt:
                    return prompt

        return fallback

    def _contains_blocked_keyword(self, text, blocked_keywords):
        if not isinstance(text, str) or not text:
            return False
        if not isinstance(blocked_keywords, list):
            return False
        return any(keyword for keyword in blocked_keywords if keyword and keyword in text)

    def _match_keyword_reply(self, text, keyword_replies, variables=None):
        if not isinstance(text, str) or not text:
            return None
        if not isinstance(keyword_replies, list):
            return None

        for item in keyword_replies:
            if not isinstance(item, dict):
                continue
            keyword = str(item.get("keyword", "")).strip()
            reply = str(item.get("reply", "")).strip()
            if not keyword or not reply:
                continue
            if keyword in text:
                return self._render_template(reply, variables)
        return None

    def _should_ignore_user_name(self, user_name):
        if not isinstance(user_name, str):
            return False

        basic_config = self.config_json.get("basic", {})
        normalized_name = user_name.strip()
        if not normalized_name:
            return False

        if basic_config.get("ignoreMaskedName") and normalized_name.count("*") >= 2:
            return True

        if basic_config.get("ignoreNumericName") and re.fullmatch(r"用户\d+", normalized_name):
            return True

        return False

    def _dispatch_prompt(self, prompt):
        try:
            result = sendPrompt(self.device_id, prompt)
            if result and result.get("ok") and callable(self.sent_prompt_callback):
                self.sent_prompt_callback({
                    "device_id": self.device_id,
                    "live_id": self.live_id,
                    "room_id": self.__room_id,
                    "prompt": prompt,
                    "response": result,
                })
        except Exception as err:
            print(f"【X】sendPrompt error: {err}")
            with self._prompt_lock:
                self._can_send_prompt = True
            self._try_dispatch_prompt()

    def _source_queue_limit(self, source):
        if source in {"timed", "awkward"}:
            return 1
        return self.config_json.get(source, {}).get("latestCount", 1)

    def _get_or_create_source_queue_unlocked(self, source):
        limit = self._source_queue_limit(source)
        if limit <= 0:
            return None
        queue = self._pending_prompts_by_source.get(source)
        if queue is not None:
            return queue
        queue = deque(maxlen=limit)
        self._pending_prompts_by_source[source] = queue
        return queue

    def _build_next_prompt_unlocked(self):
        if not self._pending_prompts_by_source:
            return None

        next_source = None
        for source in sorted(PROMPT_SOURCE_PRIORITY, key=lambda s: PROMPT_SOURCE_PRIORITY.get(s, 0), reverse=True):
            source_queue = self._pending_prompts_by_source.get(source)
            if source_queue:
                next_source = source
                break
        if next_source is None:
            return None

        if next_source != "danmu":
            source_queue = self._pending_prompts_by_source.get(next_source)
            if not source_queue:
                return None
            return source_queue.popleft()["prompt"]

        # 合并弹幕
        # danmu_batch_count = 3
        danmu_batch_count = 1   # 先不合并
        source_queue = self._pending_prompts_by_source.get("danmu")
        if not source_queue:
            return None
        danmu_prompts = []
        while source_queue and len(danmu_prompts) < danmu_batch_count:
            danmu_prompts.append(source_queue.popleft()["prompt"])
        if not danmu_prompts:
            return None
        return " | ".join(danmu_prompts)

    def _try_dispatch_prompt(self):
        with self._prompt_lock:
            if not self._can_send_prompt:
                return
            prompt = self._build_next_prompt_unlocked()
            if not prompt:
                return
            self._can_send_prompt = False
        self._dispatch_prompt(prompt)

    def enqueue_prompt(self, prompt, source="danmu"):
        if not prompt:
            return
        with self._prompt_lock:
            source_queue = self._get_or_create_source_queue_unlocked(source)
            if source_queue is None:
                return
            source_queue.append({
                "source": source,
                "prompt": prompt,
                "ts": time.time(),
            })
        self._try_dispatch_prompt()

    def enqueue_manual_prompt(self, message):
        if message is None:
            return
        message = str(message).strip()
        if not message:
            return
        prompt = self._build_prompt_from_templates(
            "manual",
            variables={
                "message": message,
                "text": message,
            },
            fallback=message,
        )
        self.enqueue_prompt(prompt, source="manual")

    def allow_send_prompt(self):
        with self._prompt_lock:
            self._can_send_prompt = True
        self._try_dispatch_prompt()

    def get_prompt_state(self):
        with self._prompt_lock:
            pending_by_source = {
                source: len(queue)
                for source, queue in self._pending_prompts_by_source.items()
                if queue
            }
            return {
                "can_send_prompt": self._can_send_prompt,
                "pending_prompt_count": sum(pending_by_source.values()),
                "pending_by_source": pending_by_source,
            }

    def get_live_state(self):
        with self._live_state_lock:
            return {
                "room_status": self._room_status,
                "current_viewer_count": self._current_viewer_count,
                "total_viewer_count": self._total_viewer_count,
                "room_stats_updated_at": self._room_stats_updated_at,
            }

    def _start_background_tasks(self):
        self._background_threads = []
        if self.config_json["timed"].get("enabled") and self.config_json["timed"].get("templates"):
            t = threading.Thread(target=self._timed_loop, daemon=True)
            t.start()
            self._background_threads.append(t)

        if self.config_json["awkward"].get("enabled") and self.config_json["awkward"].get("templates"):
            t = threading.Thread(target=self._awkward_loop, daemon=True)
            t.start()
            self._background_threads.append(t)

    def _timed_loop(self):
        interval = self.config_json["timed"].get("interval", 60)
        while not self._stop_event.wait(interval):
            prompt = self._pick_template(self.config_json["timed"], "_timed_idx")
            self.enqueue_prompt(prompt, source="timed")

    def _awkward_loop(self):
        interval = self.config_json["awkward"].get("interval", 45)
        while not self._stop_event.wait(1):
            now = time.time()
            with self._state_lock:
                no_interaction_for = now - self._last_interaction_ts
                last_silence_push = self._last_silence_push_ts
            if no_interaction_for < interval:
                continue
            if last_silence_push and (now - last_silence_push) < interval:
                continue

            prompt = self._pick_template(self.config_json["awkward"], "_awkward_idx")
            self.enqueue_prompt(prompt, source="awkward")
            with self._state_lock:
                self._last_silence_push_ts = time.time()
    
    @property
    def ttwid(self):
        """
        产生请求头部cookie中的ttwid字段，访问抖音网页版直播间首页可以获取到响应cookie中的ttwid
        :return: ttwid
        """
        if self.__ttwid:
            return self.__ttwid
        headers = {
            "User-Agent": self.user_agent,
        }
        try:
            response = self.session.get(self.live_url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            print("【X】Request the live url error: ", err)
        else:
            self.__ttwid = response.cookies.get('ttwid')
            return self.__ttwid
    
    @property
    def room_id(self):
        """
        根据直播间的地址获取到真正的直播间roomId，有时会有错误，可以重试请求解决
        :return:room_id
        """
        if self.__room_id:
            return self.__room_id
        url = self.live_url + self.live_id
        headers = {
            "User-Agent": self.user_agent,
            "cookie": f"ttwid={self.ttwid}&msToken={generateMsToken()}; __ac_nonce=0123407cc00a9e438deb4",
        }
        try:
            response = self.session.get(url, headers=headers)
            response.raise_for_status()
        except Exception as err:
            print("【X】Request the live room url error: ", err)
        else:
            match = re.search(r'roomId\\":\\"(\d+)\\"', response.text)
            if match is None or len(match.groups()) < 1:
                print("【X】No match found for roomId")
            
            self.__room_id = match.group(1)
            
            return self.__room_id
    
    def get_ac_nonce(self):
        """
        获取 __ac_nonce
        """
        resp_cookies = self.session.get(self.host, headers=self.headers).cookies
        return resp_cookies.get("__ac_nonce")
    
    def get_ac_signature(self, __ac_nonce: str = None) -> str:
        """
        获取 __ac_signature
        """
        __ac_signature = get__ac_signature(self.host[8:], __ac_nonce, self.user_agent)
        self.session.cookies.set("__ac_signature", __ac_signature)
        return __ac_signature
    
    def get_a_bogus(self, url_params: dict):
        """
        获取 a_bogus
        """
        url = urllib.parse.urlencode(url_params)
        ctx = execute_js(self.abogus_file)
        _a_bogus = ctx.call("get_ab", url, self.user_agent)
        return _a_bogus
    
    def get_room_status(self):
        """
        获取直播间开播状态:
        room_status: 2 直播已结束
        room_status: 0 直播进行中
        """
        msToken = generateMsToken()
        nonce = self.get_ac_nonce()
        signature = self.get_ac_signature(nonce)
        url = ('https://live.douyin.com/webcast/room/web/enter/?aid=6383'
               '&app_name=douyin_web&live_id=1&device_platform=web&language=zh-CN&enter_from=page_refresh'
               '&cookie_enabled=true&screen_width=5120&screen_height=1440&browser_language=zh-CN&browser_platform=Win32'
               '&browser_name=Edge&browser_version=140.0.0.0'
               f'&web_rid={self.live_id}'
               f'&room_id_str={self.room_id}'
               '&enter_source=&is_need_double_stream=false&insert_task_id=&live_reason=&msToken=' + msToken)
        query = parse_url(url).query
        params = {i[0]: i[1] for i in [j.split('=') for j in query.split('&')]}
        a_bogus = self.get_a_bogus(params)  # 计算a_bogus,成功率不是100%，出现失败时重试即可
        url += f"&a_bogus={a_bogus}"
        headers = self.headers.copy()
        headers.update({
            'Referer': f'https://live.douyin.com/{self.live_id}',
            'Cookie': f'ttwid={self.ttwid};__ac_nonce={nonce}; __ac_signature={signature}',
        })
        resp = self.session.get(url, headers=headers)
        data = resp.json().get('data')
        if data:
            room_status = data.get('room_status')
            user = data.get('user')
            user_id = user.get('id_str')
            nickname = user.get('nickname')
            with self._live_state_lock:
                self._room_status = "ended" if bool(room_status) else "running"
            print(f"【{nickname}】[{user_id}]直播间：{['正在直播', '已结束'][bool(room_status)]}.")
    
    def _connectWebSocket(self):
        """
        连接抖音直播间websocket服务器，请求直播间数据
        """
        wss = ("wss://webcast100-ws-web-lq.douyin.com/webcast/im/push/v2/?app_name=douyin_web"
               "&version_code=180800&webcast_sdk_version=1.0.14-beta.0"
               "&update_version_code=1.0.14-beta.0&compress=gzip&device_platform=web&cookie_enabled=true"
               "&screen_width=1536&screen_height=864&browser_language=zh-CN&browser_platform=Win32"
               "&browser_name=Mozilla"
               "&browser_version=5.0%20(Windows%20NT%2010.0;%20Win64;%20x64)%20AppleWebKit/537.36%20(KHTML,"
               "%20like%20Gecko)%20Chrome/126.0.0.0%20Safari/537.36"
               "&browser_online=true&tz_name=Asia/Shanghai"
               "&cursor=d-1_u-1_fh-7392091211001140287_t-1721106114633_r-1"
               f"&internal_ext=internal_src:dim|wss_push_room_id:{self.room_id}|wss_push_did:7319483754668557238"
               f"|first_req_ms:1721106114541|fetch_time:1721106114633|seq:1|wss_info:0-1721106114633-0-0|"
               f"wrds_v:7392094459690748497"
               f"&host=https://live.douyin.com&aid=6383&live_id=1&did_rule=3&endpoint=live_pc&support_wrds=1"
               f"&user_unique_id=7319483754668557238&im_path=/webcast/im/fetch/&identity=audience"
               f"&need_persist_msg_count=15&insert_task_id=&live_reason=&room_id={self.room_id}&heartbeatDuration=0")
        
        signature = generateSignature(wss)
        wss += f"&signature={signature}"
        
        headers = {
            "cookie": f"ttwid={self.ttwid}",
            'user-agent': self.user_agent,
        }
        self.ws = websocket.WebSocketApp(wss,
                                         header=headers,
                                         on_open=self._wsOnOpen,
                                         on_message=self._wsOnMessage,
                                         on_error=self._wsOnError,
                                         on_close=self._wsOnClose)
        try:
            self.ws.run_forever()
        except Exception:
            self.stop()
            raise
    
    def _sendHeartbeat(self):
        """
        发送心跳包
        """
        while True:
            try:
                heartbeat = PushFrame(payload_type='hb').SerializeToString()
                self.ws.send(heartbeat, websocket.ABNF.OPCODE_PING)
                print("【√】发送心跳包")
            except Exception as e:
                print("【X】心跳包检测错误: ", e)
                break
            else:
                time.sleep(5)
    
    def _wsOnOpen(self, ws):
        """
        连接建立成功
        """
        print("【√】WebSocket连接成功.")
        threading.Thread(target=self._sendHeartbeat, daemon=True).start()
        self._start_background_tasks()
    
    def _wsOnMessage(self, ws, message):
        """
        接收到数据
        :param ws: websocket实例
        :param message: 数据
        """
        
        # 根据proto结构体解析对象
        package = PushFrame().parse(message)
        response = Response().parse(gzip.decompress(package.payload))
        
        # 返回直播间服务器链接存活确认消息，便于持续获取数据
        if response.need_ack:
            ack = PushFrame(log_id=package.log_id,
                            payload_type='ack',
                            payload=response.internal_ext.encode('utf-8')
                            ).SerializeToString()
            ws.send(ack, websocket.ABNF.OPCODE_BINARY)
        
        # 根据消息类别解析消息体
        for msg in response.messages_list:
            method = msg.method
            if method in {'WebcastChatMessage', 'WebcastGiftMessage', 'WebcastSocialMessage', 'WebcastMemberMessage'}:
                self._mark_interaction()
            try:
                {
                    'WebcastChatMessage': self._parseChatMsg,  # 聊天消息
                    'WebcastGiftMessage': self._parseGiftMsg,  # 礼物消息
                    'WebcastLikeMessage': self._parseLikeMsg,  # 点赞消息
                    'WebcastMemberMessage': self._parseMemberMsg,  # 进入直播间消息
                    'WebcastSocialMessage': self._parseSocialMsg,  # 关注消息
                    'WebcastRoomUserSeqMessage': self._parseRoomUserSeqMsg,  # 直播间统计
                    'WebcastFansclubMessage': self._parseFansclubMsg,  # 粉丝团消息
                    'WebcastControlMessage': self._parseControlMsg,  # 直播间状态消息
                    'WebcastEmojiChatMessage': self._parseEmojiChatMsg,  # 聊天表情包消息
                    'WebcastRoomStatsMessage': self._parseRoomStatsMsg,  # 直播间统计信息
                    'WebcastRoomMessage': self._parseRoomMsg,  # 直播间信息
                    'WebcastRoomRankMessage': self._parseRankMsg,  # 直播间排行榜信息
                    'WebcastRoomStreamAdaptationMessage': self._parseRoomStreamAdaptationMsg,  # 直播间流配置
                }.get(method)(msg.payload)
            except Exception:
                pass
    
    def _wsOnError(self, ws, error):
        print("WebSocket error: ", error)
    
    def _wsOnClose(self, ws, *args):
        self.get_room_status()
        print("WebSocket connection closed.")
    
    def _parseChatMsg(self, payload):
        """聊天消息"""
        message = ChatMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        content = message.content
        print(f"【聊天msg】[{user_id}]{user_name}: {content}")
        if self._should_ignore_user_name(user_name):
            return

        danmu_config = self.config_json.get("danmu", {})
        if self._contains_blocked_keyword(content, danmu_config.get("blockedKeywords")):
            return

        variables = {
            "name": user_name,
            "text": content,
            "userId": user_id,
        }
        keyword_reply = self._match_keyword_reply(
            content,
            danmu_config.get("keywordReplies"),
            variables=variables,
        )
        if keyword_reply:
            self.enqueue_prompt(keyword_reply, source="danmu")
            return

        prompt = self._build_prompt_from_templates(
            "danmu",
            variables=variables,
            fallback=f"【弹幕】{user_name}: {content}",
        )
        self.enqueue_prompt(prompt, source="danmu")
        
    
    def _parseGiftMsg(self, payload):
        """礼物消息"""
        message = GiftMessage().parse(payload)
        user_name = message.user.nick_name
        gift_name = message.gift.name
        gift_cnt = message.combo_count
        print(f"【礼物msg】{user_name} 送出了 {gift_name}x{gift_cnt}")
        if self._should_ignore_user_name(user_name):
            return
        prompt = self._build_prompt_from_templates(
            "gift",
            variables={
                "name": user_name,
                "giftName": gift_name,
                "count": gift_cnt,
            },
            fallback=f"【刷礼物】{user_name} 送出了 {gift_name}x{gift_cnt}",
        )
        self.enqueue_prompt(prompt, source="gift")
    
    def _parseLikeMsg(self, payload):
        '''点赞消息'''
        message = LikeMessage().parse(payload)
        user_name = message.user.nick_name
        count = message.count
        # print(f"【点赞msg】{user_name} 点了{count}个赞")
        if self._should_ignore_user_name(user_name):
            return
        prompt = self._build_prompt_from_templates(
            "like",
            variables={
                "name": user_name,
                "count": count,
            },
            fallback=f"【点赞】{user_name} 点了{count}个赞",
        )
        self.enqueue_prompt(prompt, source="like")
    
    def _parseMemberMsg(self, payload):
        '''进入直播间消息'''
        message = MemberMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        gender = ["女", "男"][message.user.gender]
        # print(f"【进场msg】[{user_id}][{gender}]{user_name} 进入了直播间")
        if self._should_ignore_user_name(user_name):
            return
        prompt = self._build_prompt_from_templates(
            "welcome",
            variables={
                "name": user_name,
                "userId": user_id,
                "gender": gender,
            },
            fallback=f"【欢迎进场】{user_name} 进入了直播间",
        )
        self.enqueue_prompt(prompt, source="welcome")
    
    def _parseSocialMsg(self, payload):
        '''关注消息'''
        message = SocialMessage().parse(payload)
        user_name = message.user.nick_name
        user_id = message.user.id
        if self._should_ignore_user_name(user_name):
            return
        prompt = self._build_prompt_from_templates(
            "follow",
            variables={
                "name": user_name,
                "userId": user_id,
            },
            fallback=f"【关注】[{user_id}]{user_name} 关注了主播",
        )
        self.enqueue_prompt(prompt, source="follow")
    
    def _parseRoomUserSeqMsg(self, payload):
        '''直播间统计'''
        message = RoomUserSeqMessage().parse(payload)
        current = message.total
        total = message.total_pv_for_anchor
        with self._live_state_lock:
            self._current_viewer_count = current
            self._total_viewer_count = total
            self._room_stats_updated_at = datetime.now().astimezone().isoformat()
        # self._safe_send_prompt(f"【统计】当前观看人数: {current}, 累计观看人数: {total}")
    
    def _parseFansclubMsg(self, payload):
        '''粉丝团消息'''
        message = FansclubMessage().parse(payload)
        content = message.content
        print(f"【粉丝团msg】 {content}")
    
    def _parseEmojiChatMsg(self, payload):
        '''聊天表情包消息'''
        message = EmojiChatMessage().parse(payload)
        emoji_id = message.emoji_id
        user = message.user
        common = message.common
        default_content = message.default_content
        print(f"【聊天表情包id】 {emoji_id},user：{user},common:{common},default_content:{default_content}")
    
    def _parseRoomMsg(self, payload):
        message = RoomMessage().parse(payload)
        common = message.common
        room_id = common.room_id
        # print(f"【直播间msg】直播间id:{room_id}")
    
    def _parseRoomStatsMsg(self, payload):
        message = RoomStatsMessage().parse(payload)
        display_long = message.display_long
        # print(f"【直播间统计msg】{display_long}")
    
    def _parseRankMsg(self, payload):
        message = RoomRankMessage().parse(payload)
        ranks_list = message.ranks_list
        # print(f"【直播间排行榜msg】{ranks_list}")
    
    def _parseControlMsg(self, payload):
        '''直播间状态消息'''
        message = ControlMessage().parse(payload)
        
        if message.status == 3:
            with self._live_state_lock:
                self._room_status = "ended"
            print("直播间已结束")
            self.stop()
    
    def _parseRoomStreamAdaptationMsg(self, payload):
        message = RoomStreamAdaptationMessage().parse(payload)
        adaptationType = message.adaptation_type
        print(f'直播间adaptation: {adaptationType}')
