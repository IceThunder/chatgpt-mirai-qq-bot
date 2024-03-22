import json
import threading
import time
import asyncio
import hashlib
import base64
from Crypto.Cipher import AES
from io import BytesIO
from loguru import logger
from pydub import AudioSegment
from quart import Quart, request, abort, make_response

from graia.ariadne.message.chain import MessageChain
from graia.ariadne.message.element import Image, Voice
from graia.ariadne.message.element import Plain

import constants
from constants import config
from universal import handle_message

Port = config.feishu.port
AppId = config.feishu.app_id
AppSecret = config.feishu.app_secret
Token = config.feishu.token
EncryptKey = config.feishu.encrypt_key
# client = lark.Client.builder().app_id(AppId).app_secret(AppSecret).build()
app = Quart(__name__)

lock = threading.Lock()

request_dic = {}

RESPONSE_SUCCESS = "SUCCESS"
RESPONSE_FAILED = "FAILED"
RESPONSE_DONE = "DONE"


class InvalidEventException(Exception):
    def __init__(self, error_info):
        self.error_info = error_info

    def __str__(self) -> str:
        return "Invalid event: {}".format(self.error_info)


class AESCipher(object):
    def __init__(self, key):
        self.bs = AES.block_size
        self.key = hashlib.sha256(AESCipher.str_to_bytes(key)).digest()

    @staticmethod
    def str_to_bytes(data):
        u_type = type(b"".decode('utf8'))
        if isinstance(data, u_type):
            return data.encode('utf8')
        return data

    @staticmethod
    def _unpad(s):
        return s[:-ord(s[len(s) - 1:])]

    def decrypt(self, enc):
        iv = enc[:AES.block_size]
        cipher = AES.new(self.key, AES.MODE_CBC, iv)
        return self._unpad(cipher.decrypt(enc[AES.block_size:]))

    def decrypt_string(self, enc):
        enc = base64.b64decode(enc)
        return self.decrypt(enc).decode('utf8')


def validate(request, encrypt_key):
    if request.header.token != Token:
        raise InvalidEventException("invalid token")
    timestamp = request.headers.get("X-Lark-Request-Timestamp")
    nonce = request.headers.get("X-Lark-Request-Nonce")
    signature = request.headers.get("X-Lark-Signature")
    body = request.data
    bytes_b1 = (timestamp + nonce + encrypt_key).encode("utf-8")
    bytes_b = bytes_b1 + body
    h = hashlib.sha256(bytes_b)
    if signature != h.hexdigest():
        raise InvalidEventException("invalid signature in event")


def decryptJson(encrypt_json):
    logger.info(f"encrypt.get('encrypt')={encrypt_json.get('encrypt')}")
    cipher = AESCipher(EncryptKey)
    return cipher.decrypt_string(encrypt_json.get('encrypt'))


"""
    先判断event_type == im.message.receive_v1 (或者是v2的消息体)
    msg = event.get("message")
    幂等判断消息是否重复（通过msg.get("message_id")）
    判断msg.get("chat_type")
        group
            判断@的相关处理
            receive_id_type = "chat_id"
        p2p
            receive_id_type = "open_id"
    这个receive_id_type似乎关系到后面发给谁
    """


@app.route("/event", methods=["POST"])
async def event():
    logger.info("validate")
    validate(Token, EncryptKey)
    logger.info("decrypt")
    encrypt_json = await request.get_json()
    decrypt_json = decryptJson(encrypt_json)
    logger.info(f"decrypt_json={decrypt_json}")
    response = await make_response(decrypt_json)
    response.status_code = 200
    return response


def clear_request_dict():
    logger.debug("Watch and clean request_dic.")
    while True:
        now = time.time()
        keys_to_delete = []
        for key, bot_request in request_dic.items():
            if now - int(key) / 1000 > 600:
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
    return await app.run_task(host="0.0.0.0", port=Port, debug=config.feishu.debug)
