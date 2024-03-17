import json
import threading
import time
import asyncio
import base64
from io import BytesIO
from loguru import logger
from pydub import AudioSegment
from quart import Quart, request, abort, make_response

import lark_oapi as lark
from lark_oapi.adapter.flask import *
from lark_oapi.api.im.v1 import *

from constants import config

Port = config.feishu.port
AppId = config.feishu.app_id
AppSecret = config.feishu.app_secret
Token = config.feishu.token
EncryptKey = config.feishu.encryptKey
app = Quart(__name__)

request_dic = {}


def do_p2_im_message_receive_v1(data: P2ImMessageReceiveV1) -> None:
    print(lark.JSON.marshal(data))


def do_customized_event(data: lark.CustomizedEvent) -> None:
    print(lark.JSON.marshal(data))


handler = lark.EventDispatcherHandler.builder(lark.ENCRYPT_KEY, lark.VERIFICATION_TOKEN, lark.LogLevel.DEBUG) \
    .register_p2_im_message_receive_v1(do_p2_im_message_receive_v1) \
    .register_p1_customized_event("message", do_customized_event) \
    .build()


@app.route("/event", methods=["POST"])
def event():
    resp = handler.do(parse_req())
    return parse_resp(resp)


def convert_mp3_to_amr(mp3):
    mp3 = BytesIO(base64.b64decode(mp3))
    amr = BytesIO()
    AudioSegment.from_file(mp3,format="mp3").set_frame_rate(8000).set_channels(1).export(amr, format="amr", codec="libopencore_amrnb")
    return amr


def clear_request_dict():
    logger.debug("Watch and clean request_dic.")
    while True:
        now = time.time()
        keys_to_delete = []
        for key, bot_request in request_dic.items():
            if now - int(key)/1000 > 600:
                logger.debug(f"Remove time out request -> {key}|{bot_request.session_id}|{bot_request.username}"
                             f"|{bot_request.message}")
                keys_to_delete.append(key)
        for key in keys_to_delete:
            request_dic.pop(key)
        time.sleep(60)


async def start_task():
    """|coro|
    以异步方式启动
    """
    threading.Thread(target=clear_request_dict).start()
    return await app.run_task(port=Port, debug=config.feishu.debug)