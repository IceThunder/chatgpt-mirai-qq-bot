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


class BotRequest:
    def __init__(self, session_id, user_id, username, message, request_time):
        self.session_id: str = session_id
        self.user_id: str = user_id
        self.username: str = username
        self.message: str = message
        self.result: ResponseResult = ResponseResult()
        self.request_time = request_time
        self.done: bool = False
        """请求是否处理完毕"""

    def set_result_status(self, result_status):
        if not self.result:
            self.result = ResponseResult()
        self.result.result_status = result_status

    def append_result(self, result_type, result):
        with lock:
            if result_type == "message":
                self.result.message.append(result)
            elif result_type == "voice":
                self.result.voice.append(result)
            elif result_type == "image":
                self.result.image.append(result)


class ResponseResult:
    def __init__(self, message=None, voice=None, image=None, result_status=RESPONSE_SUCCESS):
        self.result_status = result_status
        self.message = self._ensure_list(message)
        self.voice = self._ensure_list(voice)
        self.image = self._ensure_list(image)

    def _ensure_list(self, value):
        if value is None:
            return []
        elif isinstance(value, list):
            return value
        else:
            return [value]

    def is_empty(self):
        return not self.message and not self.voice and not self.image

    def pop_all(self):
        with lock:
            self.message = []
            self.voice = []
            self.image = []

    def to_json(self):
        return json.dumps({
            'result': self.result_status,
            'message': self.message,
            'voice': self.voice,
            'image': self.image
        })


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


class Obj(dict):
    def __init__(self, d):
        for a, b in d.items():
            if isinstance(b, (list, tuple)):
                setattr(self, a, [Obj(x) if isinstance(x, dict) else x for x in b])
            else:
                setattr(self, a, Obj(b) if isinstance(b, dict) else b)


def dict_2_obj(d: dict):
    return Obj(d)


def validate(my_request, encrypt_key):
    timestamp = my_request.headers.get("X-Lark-Request-Timestamp")
    nonce = my_request.headers.get("X-Lark-Request-Nonce")
    signature = my_request.headers.get("X-Lark-Signature")
    body = my_request.data
    bytes_b1 = (timestamp + nonce + encrypt_key).encode("utf-8")
    bytes_b = bytes_b1 + body
    h = hashlib.sha256(bytes_b)
    if signature != h.hexdigest():
        raise InvalidEventException("invalid signature in event")


def decryptJson(encrypt_json):
    logger.info(f"encrypt.get('encrypt')={encrypt_json.get('encrypt')}")
    cipher = AESCipher(EncryptKey)
    return json.loads(cipher.decrypt_string(encrypt_json.get('encrypt')))


@app.route("/event", methods=["POST"])
async def event():
    # logger.info("validate")
    # validate(request, EncryptKey)
    logger.info("decrypt")
    encrypt_json = await request.get_json()
    logger.info(f"encrypt_json={encrypt_json}")
    decrypt_string = decryptJson(encrypt_json)
    logger.info(f"decrypt_string={decrypt_string}")

    decrypt_json = dict_2_obj(decrypt_string)
    header = decrypt_json.header
    event = decrypt_json.event
    if header.token != Token:
        logger.info(f"header.get(‘token’)={header.token}")
        raise InvalidEventException("invalid token")

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

    if (header.event_type == "im.message.receive_v1"):
        event_id = header.event_id
        message = event.message
        construct_bot_request(message)
        request_dic[event_id] = message
        logger.info(f"receive_id_type={header.receive_id_type}")

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


def construct_bot_request(data):
    session_id = f"wecom-{str(data.source)}" or "wecom-default_session"
    user_id = data.source
    username = client.user.get(user_id) or "某人"
    message = data.content
    logger.info(f"Get message from {session_id}[{user_id}]:\n{message}")
    with lock:
        bot_request = BotRequest(session_id, user_id, username,
                                 message, str(int(time.time() * 1000)))
    return bot_request


async def start_task():
    """|coro|
    以异步方式启动
    """
    threading.Thread(target=clear_request_dict).start()
    return await app.run_task(host="0.0.0.0", port=Port, debug=config.feishu.debug)
