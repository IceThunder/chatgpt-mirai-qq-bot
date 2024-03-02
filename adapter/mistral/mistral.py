import json
import time
import aiohttp
import async_timeout
from loguru import logger
from typing import AsyncGenerator

from adapter.botservice import BotAdapter
from config import MistralAIAPI
from constants import botManager, config

class MistralAIAPIAdapter(BotAdapter):
    api_info: MistralAIAPI = None
    """API Key"""

    def __init__(self, session_id: str = "unknown"):
        super().__init__()
        self.latest_role = None
        self.__conversation_keep_from = 0
        self.session_id = session_id
        self.api_info = botManager.pick('mistral')
        self.temperature = self.api_info.temperature
        self.top_p = self.api_info.top_p
        self.safe_prompt = self.api_info.safe_prompt
        self.conversation_id = None
        self.parent_id = None
        self.bot.conversation[self.session_id] = []
        self.current_model = self.bot.engine
        self.supported_models = [
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
            "open-mixtral-8x7b",
            "open-mistral-7b",
        ]

    def manage_conversation(self, session_id: str, prompt: str):
        if session_id not in self.bot.conversation:
            self.bot.conversation[session_id] = [
                {"role": "system", "content": prompt}
            ]
            self.__conversation_keep_from = 1

        while self.bot.max_tokens - self.bot.count_tokens(session_id) < config.openai.gpt_params.min_tokens and \
                len(self.bot.conversation[session_id]) > self.__conversation_keep_from:
            self.bot.conversation[session_id].pop(self.__conversation_keep_from)
            logger.debug(
                f"清理 token，历史记录遗忘后使用 token 数：{str(self.bot.count_tokens(session_id))}"
            )

    async def switch_model(self, model_name):
        self.current_model = model_name
        self.bot.engine = self.current_model

    async def rollback(self):
        if len(self.bot.conversation[self.session_id]) <= 0:
            return False
        await self.bot.rollback(self.session_id, n=2)
        return True

    async def on_reset(self):
        self.api_info = botManager.pick('openai-api')
        self.bot.api_key = self.api_info.api_key
        self.bot.proxy = self.api_info.proxy
        self.bot.conversation[self.session_id] = []
        self.bot.engine = self.current_model
        self.__conversation_keep_from = 0

    def construct_data(self, messages: list = None, api_key: str = None):
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}'
        }
        data = {
            'model': self.bot.engine,
            'messages': messages,
            'stream': True,
            'temperature': self.bot.temperature,
            'top_p': self.bot.top_p,
            'max_tokens': self.bot.get_max_tokens(self.session_id, self.bot.engine),
        }
        return headers, data

    async def _process_response(self, resp, session_id: str = None):

        result = await resp.json()

        total_tokens = result.get('usage', {}).get('total_tokens', None)
        logger.debug(f"[MistralAI-API:{self.bot.engine}] 使用 token 数：{total_tokens}")
        if total_tokens is None:
            raise Exception("Response does not contain 'total_tokens'")

        content = result.get('choices', [{}])[0].get('message', {}).get('content', None)
        logger.debug(f"[MistralAI-API:{self.bot.engine}] 响应：{content}")
        if content is None:
            raise Exception("Response does not contain 'content'")

        response_role = result.get('choices', [{}])[0].get('message', {}).get('role', None)
        if response_role is None:
            raise Exception("Response does not contain 'role'")

        self.bot.add_to_conversation(content, response_role, session_id)

        return content

    async def request(self, session_id: str = None, messages: list = None) -> str:
        self.api_info = botManager.pick('mistral')
        api_key = self.api_info.api_key
        proxy = self.api_info.proxy
        api_endpoint = config.mistral.api_endpoint or "https://api.mistral.ai/v1"

        if not messages:
            messages = self.bot.conversation[session_id]

        headers, data = self.construct_data(messages, api_key)

        async with aiohttp.ClientSession() as session:
            with async_timeout.timeout(self.bot.timeout):
                async with session.post(f'{api_endpoint}/chat/completions', headers=headers,
                                                    data=json.dumps(data), proxy=proxy) as resp:
                    if resp.status != 200:
                        response_text = await resp.text()
                        raise Exception(
                            f"{resp.status} {resp.reason} {response_text}",
                        )
                    return await self._process_response(resp, session_id)

    async def ask(self, prompt: str) -> AsyncGenerator[str, None]:
        """Send a message to api and return the response with stream."""

        self.manage_conversation(self.session_id, prompt)

        try:
            if self.bot.engine not in self.supported_models:
                logger.warning(f"当前模型非官方支持的模型，请注意控制台输出，当前使用的模型为 {self.bot.engine}")
            logger.debug(f"[尝试使用MistralAI-API:{self.bot.engine}] 请求：{prompt}")
            self.bot.add_to_conversation(prompt, "user", session_id=self.session_id)
            start_time = time.time()
            yield await self.request(session_id=self.session_id)
            event_time = time.time() - start_time
            if event_time is not None:
                logger.debug(f"[MistralAI-API:{self.bot.engine}] 接收到全部消息花费了{event_time:.2f}秒")

        except Exception as e:
            logger.error(f"[MistralAI-API:{self.bot.engine}] 请求失败：\n{e}")
            yield f"发生错误: \n{e}"
            raise

    async def preset_ask(self, role: str, text: str):
        self.bot.engine = self.current_model
        if role.endswith('bot') or role in {'assistant', 'mistral'}:
            logger.debug(f"[预设] 响应：{text}")
            yield text
            role = 'assistant'
        if role not in ['assistant', 'user', 'system']:
            raise ValueError(f"预设文本有误！仅支持设定 assistant、user 或 system 的预设文本，但你写了{role}。")
        if self.session_id not in self.bot.conversation:
            self.bot.conversation[self.session_id] = []
            self.__conversation_keep_from = 0
        self.bot.conversation[self.session_id].append({"role": role, "content": text})
        self.__conversation_keep_from = len(self.bot.conversation[self.session_id])
