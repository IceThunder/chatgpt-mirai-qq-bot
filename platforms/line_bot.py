import json
import os
import sys
import threading
import time
from io import BytesIO

from loguru import logger
from quart import Quart, request, abort, make_response

from graia.ariadne.message.chain import MessageChain
from graia.ariadne.message.element import Image, Plain, Voice

from universal import handle_message

from linebot.v3.messaging import ReplyMessageRequest
from linebot import (
    LineBotApi, WebhookHandler
)
from linebot.exceptions import (
    InvalidSignatureError
)
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, ImageMessage, ImageSendMessage, AudioMessage, AudioSendMessage
)

sys.path.append(os.getcwd())

from constants import config, BotPlatform

ChannelSecret = config.line.channel_secret
AssertionSigning = config.line.assertion_signing
ChannelAccessToken = config.line.channel_access_token

app = Quart(__name__)

lock = threading.Lock()

request_dic = {}

line_bot_api = LineBotApi(ChannelAccessToken)
handler = WebhookHandler(ChannelSecret)


@app.route("/callback", methods=['POST'])
def callback():
#    return "ok"
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'


@handler.add(MessageEvent, message=TextMessage or ImageMessage or AudioMessage)
async def handle_message(event):
    if event.message.type == "text":
        # 开始处理文字信息
        logger.info("Received text message: " + event.message.text)
    elif event.message.type == "image":
        # 开始处理图片信息
        logger.info("Received image message and message id:" + event.message.id)
    elif event.message.type == "audio":
        # 开始处理语音信息
        logger.info("Received audio message and message id:" + event.message.id)
    else:
        # 不支持的消息类型，只做日志输出
        logger.info(f"Received {event.message.type} message and message id:{event.message.id}")
    # 向channel发送消息等待反馈

    # 发送消息
    async def response(msg):
        if isinstance(msg, MessageChain):
            for elem in msg:
                if isinstance(elem, Plain) and str(elem):
                    chunks = [str(elem)[i:i + 1500] for i in range(0, len(str(elem)), 1500)]
                    for chunk in chunks:
                        await line_bot_api.reply_message(
                            ReplyMessageRequest(
                                reply_token=event.reply_token,
                                messages=[TextMessage(text=chunk)]
                            )
                        )
                        # TODO
                # if isinstance(elem, Image):
                #     await message.reply(file=discord.File(BytesIO(await elem.get_bytes()), filename='image.png'))
                # if isinstance(elem, Voice):
                #     await message.reply(file=discord.File(BytesIO(await elem.get_bytes()), filename="voice.wav"))
            return
        if isinstance(msg, str):
            chunks = [str(msg)[i:i + 1500] for i in range(0, len(str(msg)), 1500)]
            for chunk in chunks:
                await line_bot_api.reply_message(
                    ReplyMessageRequest(
                        reply_token=event.reply_token,
                        messages=[TextMessage(text=chunk)]
                    )
                )
            return
        # TODO
        # if isinstance(msg, Image):
        #     return await message.reply(file=discord.File(BytesIO(await msg.get_bytes()), filename='image.png'))
        # if isinstance(msg, Voice):
        #     await message.reply(file=discord.File(BytesIO(await msg.get_bytes()), filename="voice.wav"))
        #     return

    # TODO
    # await handle_message(response,
    #                      f"{'friend' if isinstance(message.channel, discord.DMChannel) else 'group'}-{message.channel.id}",
    #                      message.content.replace(f"<@{bot_id}>", "").strip(), is_manager=False,
    #                      nickname=message.author.name, request_from=BotPlatform.DiscordBot)


def clear_request_dict():
    logger.debug("Watch and clean request_dic.")
    while True:
        now = time.time()
        keys_to_delete = []
        for key, bot_request in request_dic.items():
            if now - int(key)/1000 > 600:
                logger.debug(f"Remove time out request -> {key}|{bot_request.session_id}|{bot_request.user_id}"
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
    return await app.run_task(host=config.wecom.host, port=config.wecom.port, debug=config.wecom.debug)
