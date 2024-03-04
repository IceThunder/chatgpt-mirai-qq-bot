import json
import time
import aiohttp
import async_timeout
import tiktoken
from loguru import logger
from typing import AsyncGenerator

from adapter.botservice import BotAdapter
from config import GeminiAIAPIKey
from constants import botManager, config

DEFAULT_ENGINE: str = "gemini-pro"


class GeminiAIChatbot:
    def __init__(self, api_info: GeminiAIAPIKey):
        self.api_key = api_info.api_key
        self.proxy = api_info.proxy
        self.top_p = config.gemini.gemini_params.top_p
        self.temperature = config.gemini.gemini_params.temperature
        self.max_tokens = config.gemini.gemini_params.max_tokens
        self.engine = api_info.model or DEFAULT_ENGINE
        self.timeout = config.response.max_timeout
        self.conversation: dict[str, list[dict]] = {
            "default": [
                {
                    "role": "model",
                    "parts": [{"text": "你是 Gemini，现在需要用中文进行交流。"}],
                },
            ],
        }

    async def rollback(self, session_id: str = "default", n: int = 1) -> None:
        try:
            if session_id not in self.conversation:
                raise ValueError(f"会话 ID {session_id} 不存在。")

            if n > len(self.conversation[session_id]):
                raise ValueError(f"回滚次数 {n} 超过了会话 {session_id} 的消息数量。")

            for _ in range(n):
                self.conversation[session_id].pop()

        except ValueError as ve:
            logger.error(ve)
            raise
        except Exception as e:
            logger.error(f"未知错误: {e}")
            raise

    def add_to_conversation(self, message: str, role: str, session_id: str = "default") -> None:
        if role and message is not None:
            self.conversation[session_id].append({"role": role, "parts": [{"text": message}]})
        else:
            logger.warning("出现错误！返回消息为空，不添加到会话。")
            raise ValueError("出现错误！返回消息为空，不添加到会话。")

    # https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
    def count_tokens(self, session_id: str = "default", model: str = DEFAULT_ENGINE):
        """Return the number of tokens used by a list of messages."""
        if model is None:
            model = DEFAULT_ENGINE
        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        tokens_per_message = 4
        tokens_per_name = 1

        num_tokens = 0
        for message in self.conversation[session_id]:
            num_tokens += tokens_per_message
            for key, value in message.items():
                if value is not None:
                    num_tokens += len(encoding.encode(value))
                    if key == "name":
                        num_tokens += tokens_per_name
        num_tokens += 3  # every reply is primed with model
        return num_tokens

    def get_max_tokens(self, session_id: str, model: str) -> int:
        """Get max tokens"""
        return self.max_tokens - self.count_tokens(session_id, model)


class GeminiAIAPIAdapter(BotAdapter):
    api_info: GeminiAIAPIKey = None
    """API Key"""

    def __init__(self, session_id: str = "unknown"):
        self.latest_role = None
        self.__conversation_keep_from = 0
        self.session_id = session_id
        self.api_info = botManager.pick('gemini')
        self.bot = GeminiAIChatbot(self.api_info)
        self.conversation_id = None
        self.parent_id = None
        super().__init__()
        self.bot.conversation[self.session_id] = []
        self.current_model = self.bot.engine
        self.supported_models = [
            "gemini-pro",
        ]

    def manage_conversation(self, session_id: str, prompt: str):
        if session_id not in self.bot.conversation:
            self.bot.conversation[session_id] = [
                {"role": "model", "parts": [{"text": prompt}]}
            ]
            self.__conversation_keep_from = 1

        while self.bot.max_tokens - self.bot.count_tokens(session_id) < config.gemini.gemini_params.min_tokens and \
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
        self.api_info = botManager.pick('gemini')
        self.bot.api_key = self.api_info.api_key
        self.bot.proxy = self.api_info.proxy
        self.bot.conversation[self.session_id] = []
        self.bot.engine = self.current_model
        self.__conversation_keep_from = 0

    def construct_data(self, messages: list = None):
        headers = {
            'Content-Type': 'application/json',
        }
        data = {
            'model': self.bot.engine,
            'contents': messages,
            'generationConfig': {
                'temperature': self.bot.temperature,
                'topP': self.bot.top_p,
                'maxOutputTokens': self.bot.get_max_tokens(self.session_id, self.bot.engine),
            },
        }
        return headers, data

    def _prepare_request(self, session_id: str = None, messages: list = None):
        self.api_info = botManager.pick('gemini')
        proxy = self.api_info.proxy
        api_endpoint = config.gemini.api_endpoint or "https://generativelanguage.googleapis.com/v1"

        if not messages:
            messages = self.bot.conversation[session_id]

        headers, data = self.construct_data(messages)

        return proxy, api_endpoint, headers, data

    async def _process_response(self, resp, session_id: str = None):

        result = await resp.json()

        total_tokens = result.get('usage', {}).get('total_tokens', None)
        logger.debug(f"[Gemini-API:{self.bot.engine}] 使用 token 数：{total_tokens}")
        if total_tokens is None:
            raise Exception("Response does not contain 'total_tokens'")

        content = result.get('contents', [{}])[0].get('parts', [{}])[0].get('text', None)
        logger.debug(f"[Gemini-API:{self.bot.engine}] 响应：{content}")
        if content is None:
            raise Exception("Response does not contain 'parts'")

        response_role = result.get('contents', [{}])[0].get('role', None)
        if response_role is None:
            raise Exception("Response does not contain 'role'")

        self.bot.add_to_conversation(content, response_role, session_id)

        return content

    async def request(self, session_id: str = None, messages: list = None) -> str:
        proxy, api_endpoint, headers, data = self._prepare_request(session_id, messages)

        async with aiohttp.ClientSession() as session:
            with async_timeout.timeout(self.bot.timeout):
                async with session.post(f'{api_endpoint}/models/{self.current_model}:generateContent?key={self.api_info.api_key}', headers=headers,
                                                    data=json.dumps(data), proxy=proxy) as resp:
                    if resp.status != 200:
                        response_text = await resp.text()
                        raise Exception(
                            f"{resp.status} {resp.reason} {response_text}",
                        )
                    return await self._process_response(resp, session_id)

    async def request_with_stream(self, session_id: str = None, messages: list = None) -> AsyncGenerator[str, None]:
        proxy, api_endpoint, headers, data = self._prepare_request(session_id, messages)

        async with aiohttp.ClientSession() as session:
            with async_timeout.timeout(self.bot.timeout):
                async with session.post(f'{api_endpoint}/chat/completions', headers=headers, data=json.dumps(data),
                                        proxy=proxy) as resp:
                    if resp.status != 200:
                        response_text = await resp.text()
                        raise Exception(
                            f"{resp.status} {resp.reason} {response_text}",
                        )

                    response_role: str = ''
                    completion_text: str = ''

                    async for line in resp.content:
                        try:
                            line = line.decode('utf-8').strip()
                            if not line.startswith("data: "):
                                continue
                            line = line[len("data: "):]
                            if line == "[DONE]":
                                break
                            if not line:
                                continue
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            raise Exception(f"JSON解码错误: {line}") from None
                        except Exception as e:
                            logger.error(f"未知错误: {e}\n响应内容: {resp.content}")
                            logger.error("请将该段日记提交到项目issue中，以便修复该问题。")
                            raise Exception(f"未知错误: {e}") from None
                        if 'error' in event:
                            raise Exception(f"响应错误: {event['error']}")
                        if 'candidates' in event and len(event['candidates']) > 0:
                            content = event['choices'][0]['content']
                            if 'role' in content:
                                if content['role'] is not None:
                                    response_role = content['role']
                            if 'text' in content:
                                event_text = content['text']
                                if event_text is not None:
                                    completion_text += event_text
                                    self.latest_role = response_role
                                    yield event_text
        self.bot.add_to_conversation(completion_text, response_role, session_id)

    async def compressed_session(self, session_id: str):
        if session_id not in self.bot.conversation or not self.bot.conversation[session_id]:
            logger.debug(f"不存在该会话，不进行压缩: {session_id}")
            return

        if self.bot.count_tokens(session_id) > config.gemini.gemini_params.compressed_tokens:
            logger.debug('开始进行会话压缩')

            filtered_data = [entry for entry in self.bot.conversation[session_id] if entry['role'] != 'model']
            self.bot.conversation[session_id] = [entry for entry in self.bot.conversation[session_id] if
                                                 entry['role'] not in ['model', 'user']]

            filtered_data.append(({"role": "model",
                                   "parts": [{"text": "Summarize the discussion briefly in 200 words or less to use as a prompt for future context."}]
                                   }))

            token_count = self.bot.count_tokens(self.session_id, self.bot.engine)
            logger.debug(f"压缩会话后使用 token 数：{token_count}")

    async def ask(self, prompt: str) -> AsyncGenerator[str, None]:
        """Send a message to api and return the response with stream."""

        self.manage_conversation(self.session_id, prompt)

        if config.gemini.gemini_params.compressed_session:
            await self.compressed_session(self.session_id)

        try:
            if self.bot.engine not in self.supported_models:
                logger.warning(f"当前模型非官方支持的模型，请注意控制台输出，当前使用的模型为 {self.bot.engine}")
            logger.debug(f"[尝试使用Gemini-API:{self.bot.engine}] 请求：{prompt}")
            self.bot.add_to_conversation(prompt, "user", session_id=self.session_id)
            start_time = time.time()

            yield await self.request(session_id=self.session_id)
            event_time = time.time() - start_time
            if event_time is not None:
                logger.debug(f"[geminiAI-API:{self.bot.engine}] 接收到全部消息花费了{event_time:.2f}秒")

        except Exception as e:
            logger.error(f"[Gemini-API:{self.bot.engine}] 请求失败：\n{e}")
            yield f"发生错误: \n{e}"
            raise

    async def preset_ask(self, role: str, text: str):
        self.bot.engine = self.current_model
        if role.endswith('bot') or role in {'model', 'gemini'}:
            logger.debug(f"[预设] 响应：{text}")
            yield text
            role = 'model'
        if role not in ['model', 'user']:
            raise ValueError(f"预设文本有误！仅支持设定 model 或 user 的预设文本，但你写了{role}。")
        if self.session_id not in self.bot.conversation:
            self.bot.conversation[self.session_id] = []
            self.__conversation_keep_from = 0
        self.bot.conversation[self.session_id].append({"role": role, "parts": [{"text": text}]})
        self.__conversation_keep_from = len(self.bot.conversation[self.session_id])