#!/usr/bin/python
# coding:utf-8

# @FileName:    main.py
# @Time:        2024/1/2 22:27
# @Author:      bubu
# @Project:     douyinLiveWebFetcher

from liveMan import DouyinLiveWebFetcher

if __name__ == '__main__':
    live_id = '605278220102'
    device_id = '12:34:56:78:90:ab'
    config_json = {
        "timed": {
            "enabled": True,
            "interval": 30,
            "sequential": False,
            "templates": [
                "说3个让用户非买不可的理由",
                "今天直播间有哪些优惠给大家介绍下",
                "三二一上一下链接, 制造一下库存的紧张感",
            ],
        },
        "awkward": {
            "enabled": True,
            "interval": 45,
            "sequential": True,
            "templates": [
                "现在冷场了, 从你上次冷场聊的话题里选一个继续聊下去",
                "现在冷场了, 你从刚才进入直播间的用户里选一个和他聊聊",
                "现在冷场了, 以随机的方式说说今天天气怎么样",
            ],
        },
    }
    room = DouyinLiveWebFetcher(
        live_id,
        device_id,
        config_json=config_json,
    )
    # room.get_room_status() # 失效
    room.start()
