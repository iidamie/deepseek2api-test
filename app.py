import json
import base64
import time
import logging
import random
import os
import re
import struct
import ctypes
import threading
import queue

from curl_cffi import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
import transformers

from wasmtime import Store, Module, Linker

# -------------------------- 初始化 tokenizer --------------------------
chat_tokenizer_dir = "./"  # 请确保目录下有正确的 tokenizer 配置文件或模型文件
tokenizer = transformers.AutoTokenizer.from_pretrained(chat_tokenizer_dir, trust_remote_code=True)

# -------------------------- 日志配置 --------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("main")

app = FastAPI()

# 添加 CORS 中间件，允许所有来源
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# 模板目录（请确保存在 templates/welcome.html 文件）
templates = Jinja2Templates(directory="templates")

# ----------------------------------------------------------------------
# (1) 配置文件的读写函数
# ----------------------------------------------------------------------
CONFIG_PATH = "config.json"

def load_config():
    """从 config.json 加载配置，出错则返回空 dict"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[load_config] 无法读取配置文件: {e}")
        return {}

def save_config(cfg):
    """将配置写回 config.json"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[save_config] 写入 config.json 失败: {e}")

CONFIG = load_config()

# -------------------------- 全局账号队列 --------------------------
account_queue = []  # 维护所有可用账号（不在队列中的即为正在使用）

def init_account_queue():
    """初始化时从配置加载账号"""
    global account_queue
    account_queue = CONFIG.get("accounts", [])[:]  # 深拷贝
    random.shuffle(account_queue)  # 初始随机排序

init_account_queue()

# ----------------------------------------------------------------------
# (2) DeepSeek 相关常量
# ----------------------------------------------------------------------
DEEPSEEK_HOST = "chat.deepseek.com"
DEEPSEEK_LOGIN_URL = f"https://{DEEPSEEK_HOST}/api/v0/users/login"
DEEPSEEK_CREATE_SESSION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat_session/create"
DEEPSEEK_CREATE_POW_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/create_pow_challenge"
DEEPSEEK_COMPLETION_URL = f"https://{DEEPSEEK_HOST}/api/v0/chat/completion"
BASE_HEADERS = {
    'Host': "chat.deepseek.com",
    'User-Agent': "DeepSeek/1.0.11 Android/35",
    'Accept': "application/json",
    'Accept-Encoding': "gzip",
    'Content-Type': "application/json",
    'x-client-platform': "android",
    'x-client-version': "1.0.11",
    'x-client-locale': "zh_CN",
    'accept-charset': "UTF-8"
}

# WASM 模块文件路径（请确保文件存在）
WASM_PATH = "sha3_wasm_bg.7b9ca65ddd.wasm"

# ----------------------------------------------------------------------
# 辅助函数：获取账号唯一标识（优先 email，否则 mobile）
# ----------------------------------------------------------------------
def get_account_identifier(account):
    """返回账号的唯一标识，优先使用 email，否则使用 mobile"""
    return account.get("email", "").strip() or account.get("mobile", "").strip()

# ----------------------------------------------------------------------
# (3) 登录函数：支持使用 email 或 mobile 登录
# ----------------------------------------------------------------------
def login_deepseek_via_account(account):
    """使用 account 中的 email 或 mobile 登录 DeepSeek，
    成功后将返回的 token 写入 account 并保存至配置文件，返回新 token。
    """
    email = account.get("email", "").strip()
    mobile = account.get("mobile", "").strip()
    password = account.get("password", "").strip()
    if not password or (not email and not mobile):
        raise HTTPException(status_code=400, detail="账号缺少必要的登录信息（必须提供 email 或 mobile 以及 password）")
    if email:
        payload = {
            "email": email,
            "password": password,
            "device_id": "deepseek_to_api",
            "os": "android"
        }
    else:
        payload = {
            "mobile": mobile,
            "area_code": None,
            "password": password,
            "device_id": "deepseek_to_api",
            "os": "android"
        }
    try:
        resp = requests.post(DEEPSEEK_LOGIN_URL, headers=BASE_HEADERS, json=payload)
        resp.raise_for_status()
    except Exception as e:
        logger.error(f"[login_deepseek_via_account] 登录请求异常: {e}")
        raise HTTPException(status_code=500, detail="Account login failed: 请求异常")
    try:
        data = resp.json()
    except Exception as e:
        logger.error(f"[login_deepseek_via_account] JSON解析失败: {e}")
        raise HTTPException(status_code=500, detail="Account login failed: invalid JSON response")
    # 校验响应数据格式是否正确
    if (data.get("data") is None or 
        data["data"].get("biz_data") is None or 
        data["data"]["biz_data"].get("user") is None):
        logger.error(f"[login_deepseek_via_account] 登录响应格式错误: {data}")
        raise HTTPException(status_code=500, detail="Account login failed: invalid response format")
    new_token = data["data"]["biz_data"]["user"].get("token")
    if not new_token:
        logger.error(f"[login_deepseek_via_account] 登录响应中缺少 token: {data}")
        raise HTTPException(status_code=500, detail="Account login failed: missing token")
    account["token"] = new_token
    save_config(CONFIG)
    identifier = email if email else mobile
    return new_token

# ----------------------------------------------------------------------
# (4) 从 accounts 中随机选择一个未忙且未尝试过的账号
# ----------------------------------------------------------------------
def choose_new_account():
    """选择策略：
    1. 遍历队列，找到第一个未被 exclude_ids 包含的账号
    2. 从队列中移除该账号
    3. 返回该账号（由后续逻辑保证最终会重新入队）
    """
    for i in range(len(account_queue)):
        acc = account_queue[i]
        acc_id = get_account_identifier(acc)
        if acc_id:
            # 从队列中移除并返回
            # logger.info(f"[choose_new_account] 新选择账号: {acc_id}")
            return account_queue.pop(i)
            
    logger.warning("[choose_new_account] 没有可用的账号")
    return None
    
def release_account(account):
    """将账号重新加入队列末尾"""
    account_queue.append(account)

# ----------------------------------------------------------------------
# (5) 判断调用模式：配置模式 vs 用户自带 token
# ----------------------------------------------------------------------
def determine_mode_and_token(request: Request):
    """
    根据请求头 Authorization 判断使用哪种模式：
    - 如果 Bearer token 出现在 CONFIG["keys"] 中，则为配置模式，从 CONFIG["accounts"] 中随机选择一个账号（排除已尝试账号），
      检查该账号是否已有 token，否则调用登录接口获取；
    - 否则，直接使用请求中的 Bearer 值作为 DeepSeek token。
    结果存入 request.state.deepseek_token；配置模式下同时存入 request.state.account 与 request.state.tried_accounts。
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized: missing Bearer token.")
    caller_key = auth_header.replace("Bearer ", "", 1).strip()
    config_keys = CONFIG.get("keys", [])
    if caller_key in config_keys:
        request.state.use_config_token = True
        request.state.tried_accounts = []  # 初始化已尝试账号
        selected_account = choose_new_account()
        if not selected_account:
            raise HTTPException(status_code=500, detail="No accounts configured.")
        if not selected_account.get("token", "").strip():
            try:
                login_deepseek_via_account(selected_account)
            except Exception as e:
                logger.error(f"[determine_mode_and_token] 账号 {get_account_identifier(selected_account)} 登录失败：{e}")
                raise HTTPException(status_code=500, detail="Account login failed.")

        request.state.deepseek_token = selected_account.get("token")
        request.state.account = selected_account

    else:
        request.state.use_config_token = False
        request.state.deepseek_token = caller_key

def get_auth_headers(request: Request):
    """返回 DeepSeek 请求所需的公共请求头"""
    return { **BASE_HEADERS, "authorization": f"Bearer {request.state.deepseek_token}" }

# ----------------------------------------------------------------------
# (6) 封装对话接口调用的重试机制
# ----------------------------------------------------------------------
def call_completion_endpoint(payload, headers, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        try:
            deepseek_resp = requests.post(DEEPSEEK_COMPLETION_URL, headers=headers, json=payload, stream=True)
        except Exception as e:
            logger.warning(f"[call_completion_endpoint] 请求异常: {e}")
            time.sleep(1)
            attempts += 1
            continue
        if deepseek_resp.status_code == 200:
            return deepseek_resp
        else:
            logger.warning(f"[call_completion_endpoint] 调用对话接口失败, 状态码: {deepseek_resp.status_code}")
            deepseek_resp.close()
            time.sleep(1)
            attempts += 1
    return None

# ----------------------------------------------------------------------
# (7) 创建会话 & 获取 PoW（重试时，配置模式下错误会切换账号；用户自带 token 模式下仅重试）
# ----------------------------------------------------------------------
def create_session(request: Request, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        headers = get_auth_headers(request)
        try:
            resp = requests.post(DEEPSEEK_CREATE_SESSION_URL, headers=headers, json={"agent": "chat"})
        except Exception as e:
            logger.error(f"[create_session] 请求异常: {e}")
            attempts += 1
            continue
        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"[create_session] JSON解析异常: {e}")
            data = {}
        if resp.status_code == 200 and data.get("code") == 0:
            session_id = data["data"]["biz_data"]["id"]

            resp.close()
            return session_id
        else:
            code = data.get("code")
            logger.warning(f"[create_session] 创建会话失败, code={code}, msg={data.get('msg')}")
            resp.close()
            if request.state.use_config_token:
                current_id = get_account_identifier(request.state.account)
                if not hasattr(request.state, 'tried_accounts'):
                    request.state.tried_accounts = []
                if current_id not in request.state.tried_accounts:
                    request.state.tried_accounts.append(current_id)
                new_account = choose_new_account(request.state.tried_accounts)
                if new_account is None:
                    break
                try:
                    login_deepseek_via_account(new_account)
                except Exception as e:
                    logger.error(f"[create_session] 账号 {get_account_identifier(new_account)} 登录失败：{e}")
                    attempts += 1
                    continue
                request.state.account = new_account
                request.state.deepseek_token = new_account.get("token")
            else:
                attempts += 1
                continue
        attempts += 1
    return None

# ----------------------------------------------------------------------
# (7.1) 使用 WASM 模块计算 PoW 答案的辅助函数
# ----------------------------------------------------------------------
def compute_pow_answer(algorithm: str,
                       challenge_str: str,
                       salt: str,
                       difficulty: int,
                       expire_at: int,
                       signature: str,
                       target_path: str,
                       wasm_path: str) -> int:
    """
    使用 WASM 模块计算 DeepSeekHash 答案（answer）。
    根据 JS 逻辑：
      - 拼接前缀： "{salt}_{expire_at}_"
      - 将 challenge 与前缀写入 wasm 内存后调用 wasm_solve 进行求解，
      - 从 wasm 内存中读取状态与求解结果，
      - 若状态非 0，则返回整数形式的答案，否则返回 None。
    """
    if algorithm != "DeepSeekHashV1":
        raise ValueError(f"不支持的算法：{algorithm}")
    prefix = f"{salt}_{expire_at}_"
    # --- 加载 wasm 模块 ---
    store = Store()
    linker = Linker(store.engine)
    try:
        with open(wasm_path, "rb") as f:
            wasm_bytes = f.read()
    except Exception as e:
        raise RuntimeError(f"加载 wasm 文件失败: {wasm_path}, 错误: {e}")
    module = Module(store.engine, wasm_bytes)
    instance = linker.instantiate(store, module)
    exports = instance.exports(store)
    try:
        memory = exports["memory"]
        add_to_stack = exports["__wbindgen_add_to_stack_pointer"]
        alloc = exports["__wbindgen_export_0"]
        wasm_solve = exports["wasm_solve"]
    except KeyError as e:
        raise RuntimeError(f"缺少 wasm 导出函数: {e}")

    def write_memory(offset: int, data: bytes):
        size = len(data)
        base_addr = ctypes.cast(memory.data_ptr(store), ctypes.c_void_p).value
        ctypes.memmove(base_addr + offset, data, size)

    def read_memory(offset: int, size: int) -> bytes:
        base_addr = ctypes.cast(memory.data_ptr(store), ctypes.c_void_p).value
        return ctypes.string_at(base_addr + offset, size)

    def encode_string(text: str):
        data = text.encode("utf-8")
        length = len(data)
        ptr_val = alloc(store, length, 1)
        ptr = int(ptr_val.value) if hasattr(ptr_val, "value") else int(ptr_val)
        write_memory(ptr, data)
        return ptr, length

    # 1. 申请 16 字节栈空间
    retptr = add_to_stack(store, -16)
    # 2. 编码 challenge 与 prefix 到 wasm 内存中
    ptr_challenge, len_challenge = encode_string(challenge_str)
    ptr_prefix, len_prefix = encode_string(prefix)
    # 3. 调用 wasm_solve（注意：difficulty 以 float 形式传入）
    wasm_solve(store, retptr, ptr_challenge, len_challenge, ptr_prefix, len_prefix, float(difficulty))
    # 4. 从 retptr 处读取 4 字节状态和 8 字节求解结果
    status_bytes = read_memory(retptr, 4)
    if len(status_bytes) != 4:
        add_to_stack(store, 16)
        raise RuntimeError("读取状态字节失败")
    status = struct.unpack("<i", status_bytes)[0]
    value_bytes = read_memory(retptr + 8, 8)
    if len(value_bytes) != 8:
        add_to_stack(store, 16)
        raise RuntimeError("读取结果字节失败")
    value = struct.unpack("<d", value_bytes)[0]
    # 5. 恢复栈指针
    add_to_stack(store, 16)
    if status == 0:
        return None
    return int(value)

# ----------------------------------------------------------------------
# (7.2) 获取 PoW 响应，融合计算 answer 逻辑
# ----------------------------------------------------------------------
def get_pow_response(request: Request, max_attempts=3):
    attempts = 0
    while attempts < max_attempts:
        headers = get_auth_headers(request)
        try:
            resp = requests.post(DEEPSEEK_CREATE_POW_URL, headers=headers, json={"target_path": "/api/v0/chat/completion"}, timeout=30)
        except Exception as e:
            logger.error(f"[get_pow_response] 请求异常: {e}")
            attempts += 1
            continue
        try:
            data = resp.json()
        except Exception as e:
            logger.error(f"[get_pow_response] JSON解析异常: {e}")
            data = {}
        if resp.status_code == 200 and data.get("code") == 0:
            challenge = data["data"]["biz_data"]["challenge"]
            difficulty = challenge.get("difficulty", 144000)
            expire_at = challenge.get("expire_at", 1680000000)
            try:
                answer = compute_pow_answer(
                    challenge["algorithm"],
                    challenge["challenge"],
                    challenge["salt"],
                    difficulty,
                    expire_at,
                    challenge["signature"],
                    challenge["target_path"],
                    WASM_PATH
                )
            except Exception as e:
                logger.error(f"[get_pow_response] PoW 答案计算异常: {e}")
                answer = None
            if answer is None:
                logger.warning("[get_pow_response] PoW 答案计算失败，重试中...")
                resp.close()
                attempts += 1
                continue
            pow_dict = {
                "algorithm": challenge["algorithm"],
                "challenge": challenge["challenge"],
                "salt": challenge["salt"],
                "answer": answer,  # 整数形式答案
                "signature": challenge["signature"],
                "target_path": challenge["target_path"]
            }
            pow_str = json.dumps(pow_dict, separators=(',', ':'), ensure_ascii=False)
            encoded = base64.b64encode(pow_str.encode("utf-8")).decode("utf-8").rstrip("=")
            resp.close()
            return encoded
        else:
            code = data.get("code")
            logger.warning(f"[get_pow_response] 获取 PoW 失败, code={code}, msg={data.get('msg')}")
            resp.close()
            if request.state.use_config_token:
                current_id = get_account_identifier(request.state.account)
                if not hasattr(request.state, 'tried_accounts'):
                    request.state.tried_accounts = []
                if current_id not in request.state.tried_accounts:
                    request.state.tried_accounts.append(current_id)
                new_account = choose_new_account(request.state.tried_accounts)
                if new_account is None:
                    break
                try:
                    login_deepseek_via_account(new_account)
                except Exception as e:
                    logger.error(f"[get_pow_response] 账号 {get_account_identifier(new_account)} 登录失败：{e}")
                    attempts += 1
                    continue
                request.state.account = new_account
                request.state.deepseek_token = new_account.get("token")
            else:
                attempts += 1
                continue
            attempts += 1
    return None

# ----------------------------------------------------------------------
# (8) 路由：/v1/models（模拟 OpenAI 模型列表）
# ----------------------------------------------------------------------
@app.get("/v1/models")
def list_models():
    models_list = [
        {
            "id": "deepseek-chat",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": []
        },
        {
            "id": "deepseek-reasoner",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": []
        },
        {
            "id": "deepseek-chat-search",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": []
        },
        {
            "id": "deepseek-reasoner-search",
            "object": "model",
            "created": 1677610602,
            "owned_by": "deepseek",
            "permission": []
        }
    ]
    data = {"object": "list", "data": models_list}
    return JSONResponse(content=data, status_code=200)

# ----------------------------------------------------------------------
# (新增) 消息预处理函数，将多轮对话合并成最终 prompt
# ----------------------------------------------------------------------
def messages_prepare(messages: list) -> str:
    """处理消息列表，合并连续相同角色的消息，并添加角色标签：
    - 对于 assistant 消息，加上 <｜Assistant｜> 前缀及 <｜end▁of▁sentence｜> 结束标签；
    - 对于 user/system 消息（除第一条外）加上 <｜User｜> 前缀；
    - 如果消息 content 为数组，则提取其中 type 为 "text" 的部分；
    - 最后移除 markdown 图片格式的内容。
    """
    processed = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if isinstance(content, list):
            texts = [item.get("text", "") for item in content if item.get("type") == "text"]
            text = "\n".join(texts)
        else:
            text = str(content)
        processed.append({"role": role, "text": text})
    if not processed:
        return ""
    # 合并连续同一角色的消息
    merged = [processed[0]]
    for msg in processed[1:]:
        if msg["role"] == merged[-1]["role"]:
            merged[-1]["text"] += "\n\n" + msg["text"]
        else:
            merged.append(msg)
    # 添加标签
    parts = []
    for idx, block in enumerate(merged):
        role = block["role"]
        text = block["text"]
        if role == "assistant":
            parts.append(f"<｜Assistant｜>{text}<｜end▁of▁sentence｜>")
        elif role in ("user", "system"):
            if idx > 0:
                parts.append(f"<｜User｜>{text}")
            else:
                parts.append(text)
        else:
            parts.append(text)
    final_prompt = "".join(parts)
    # 移除 markdown 图片格式：
    final_prompt = re.sub(r"!", "", final_prompt)
    return final_prompt

# 添加保活超时配置（5秒）
KEEP_ALIVE_TIMEOUT = 5

# ----------------------------------------------------------------------
# (10) 路由：/v1/chat/completions
# ----------------------------------------------------------------------
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        # 处理 token 相关逻辑，若登录失败则直接返回错误响应
        try:
            determine_mode_and_token(request)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
        except Exception as exc:
            logger.error(f"[chat_completions] determine_mode_and_token 异常: {exc}")
            return JSONResponse(status_code=500, content={"error": "Account login failed."})

        req_data = await request.json()
        model = req_data.get("model")
        messages = req_data.get("messages", [])
        if not model or not messages:
            raise HTTPException(status_code=400, detail="Request must include 'model' and 'messages'.")
        # 判断是否启用"思考"或"搜索"功能（这里根据模型名称判断）
        model_lower = model.lower()
        if model_lower in ["deepseek-v3", "deepseek-chat"]:
            thinking_enabled = False
            search_enabled = False
        elif model_lower in ["deepseek-r1", "deepseek-reasoner"]:
            thinking_enabled = True
            search_enabled = False
        elif model_lower in ["deepseek-v3-search", "deepseek-chat-search"]:
            thinking_enabled = False
            search_enabled = True
        elif model_lower in ["deepseek-r1-search", "deepseek-reasoner-search"]:
            thinking_enabled = True
            search_enabled = True
        else:
            raise HTTPException(status_code=503, detail=f"Model '{model}' is not available.")
        # 使用 messages_prepare 函数构造最终 prompt
        final_prompt = messages_prepare(messages)
        session_id = create_session(request)
        if not session_id:
            raise HTTPException(status_code=401, detail="invalid token.")
        pow_resp = get_pow_response(request)
        if not pow_resp:
            raise HTTPException(status_code=401, detail="Failed to get PoW (invalid token or unknown error).")
        headers = {
            **get_auth_headers(request),
            "x-ds-pow-response": pow_resp
        }
        payload = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": final_prompt,
            "ref_file_ids": [],
            "thinking_enabled": thinking_enabled,
            "search_enabled": search_enabled
        }

        deepseek_resp = call_completion_endpoint(payload, headers, max_attempts=3)
        if not deepseek_resp:
            raise HTTPException(status_code=500, detail="Failed to get completion.")
        created_time = int(time.time())
        completion_id = f"{session_id}"

        # 流式响应（SSE）或普通响应
        if bool(req_data.get("stream", False)):
            if deepseek_resp.status_code != 200:
                deepseek_resp.close()
                return JSONResponse(content=deepseek_resp.content, status_code=deepseek_resp.status_code)
            def sse_stream():
                try:
                    final_text = ""
                    final_thinking = ""
                    first_chunk_sent = False
                    result_queue = queue.Queue()
                    last_send_time = time.time()
                    citation_map = {}  # 用于存储引用链接的字典

                    def process_data():
                        try:
                            for raw_line in deepseek_resp.iter_lines():
                                try:
                                    line = raw_line.decode("utf-8")
                                except Exception as e:
                                    logger.warning(f"[sse_stream] 解码失败: {e}")
                                    continue
                                if not line:
                                    continue
                                if line.startswith("data:"):
                                    data_str = line[5:].strip()
                                    if data_str == "[DONE]":
                                        result_queue.put(None)  # 结束信号
                                        break
                                    try:
                                        chunk = json.loads(data_str)
                                        # 处理搜索索引数据
                                        if chunk.get("choices", [{}])[0].get("delta", {}).get("type") == "search_index":
                                            search_indexes = chunk["choices"][0]["delta"].get("search_indexes", [])
                                            for idx in search_indexes:
                                                citation_map[str(idx.get("cite_index"))] = idx.get("url", "")
                                            continue
                                        result_queue.put(chunk)  # 将数据放入队列
                                    except Exception as e:
                                        logger.warning(f"[sse_stream] 无法解析: {data_str}, 错误: {e}")
                        finally:
                            deepseek_resp.close()

                    process_thread = threading.Thread(target=process_data)
                    process_thread.start()

                    while True:
                        current_time = time.time()
                        if current_time - last_send_time >= KEEP_ALIVE_TIMEOUT:

                            yield ": keep-alive\n\n"
                            last_send_time = current_time
                            continue
                        try:
                            chunk = result_queue.get(timeout=0.1)
                            if chunk is None:
                                # 发送最终统计信息
                                prompt_tokens = len(tokenizer.encode(final_prompt))
                                completion_tokens = len(tokenizer.encode(final_text))
                                usage = {
                                    "prompt_tokens": prompt_tokens,
                                    "completion_tokens": completion_tokens,
                                    "total_tokens": prompt_tokens + completion_tokens
                                }
                                finish_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": [
                                        {"delta": {}, "index": 0, "finish_reason": "stop"}
                                    ],
                                    "usage": usage
                                }
                                yield f"data: {json.dumps(finish_chunk, ensure_ascii=False)}\n\n"
                                yield "data: [DONE]\n\n"
                                last_send_time = current_time
                                break
                            new_choices = []
                            for choice in chunk.get("choices", []):
                                delta = choice.get("delta", {})
                                ctype = delta.get("type")
                                ctext = delta.get("content", "")
                                if search_enabled and ctext.startswith("[citation:"):
                                    ctext = ""
                                if ctype == "thinking":
                                    if thinking_enabled:
                                        final_thinking += ctext
                                elif ctype == "text":
                                    final_text += ctext
                                delta_obj = {}
                                if not first_chunk_sent:
                                    delta_obj["role"] = "assistant"
                                    first_chunk_sent = True
                                if ctype == "thinking":
                                    if thinking_enabled:
                                        delta_obj["reasoning_content"] = ctext
                                elif ctype == "text":
                                    delta_obj["content"] = ctext
                                if delta_obj:
                                    new_choices.append({"delta": delta_obj, "index": choice.get("index", 0)})
                            if new_choices:
                                out_chunk = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created_time,
                                    "model": model,
                                    "choices": new_choices
                                }
                                yield f"data: {json.dumps(out_chunk, ensure_ascii=False)}\n\n"
                                last_send_time = current_time
                        except queue.Empty:
                            continue
                except Exception as e:
                    logger.error(f"[sse_stream] 异常: {e}")
                finally:
                    if getattr(request.state, "use_config_token", False) and hasattr(request.state, "account"):
                        release_account(request.state.account)
            
            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers={"Content-Type": "text/event-stream"}
            )
        else:
            # 非流式响应处理
            think_list = []
            text_list = []
            result = None
            citation_map = {}

            data_queue = queue.Queue()
            def collect_data():
                nonlocal result
                try:
                    for raw_line in deepseek_resp.iter_lines():
                        try:
                            line = raw_line.decode("utf-8")
                        except Exception as e:
                            logger.warning(f"[chat_completions] 解码失败: {e}")
                            continue
                        if not line:
                            continue
                        if line.startswith("data:"):
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                data_queue.put(None)
                                break
                            try:
                                chunk = json.loads(data_str)
                                if chunk.get("choices", [{}])[0].get("delta", {}).get("type") == "search_index":
                                    search_indexes = chunk["choices"][0]["delta"].get("search_indexes", [])
                                    for idx in search_indexes:
                                        citation_map[str(idx.get("cite_index"))] = idx.get("url", "")
                                    continue
                                for choice in chunk.get("choices", []):
                                    delta = choice.get("delta", {})
                                    ctype = delta.get("type")
                                    ctext = delta.get("content", "")
                                    if search_enabled and ctext.startswith("[citation:"):
                                        ctext = ""
                                    if ctype == "thinking" and thinking_enabled:
                                        think_list.append(ctext)
                                    elif ctype == "text":
                                        text_list.append(ctext)
                            except Exception as e:
                                logger.warning(f"[chat_completions] 无法解析: {data_str}, 错误: {e}")
                                continue
                finally:
                    deepseek_resp.close()
                    final_reasoning = "".join(think_list)
                    final_content = "".join(text_list)
                    prompt_tokens = len(tokenizer.encode(final_prompt))
                    completion_tokens = len(tokenizer.encode(final_content))
                    result = {
                        "id": completion_id,
                        "object": "chat.completion",
                        "created": created_time,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": final_content,
                                    "reasoning_content": final_reasoning
                                },
                                "finish_reason": "stop"
                            }
                        ],
                        "usage": {
                            "prompt_tokens": prompt_tokens,
                            "completion_tokens": completion_tokens,
                            "total_tokens": prompt_tokens + completion_tokens
                        }
                    }
                    data_queue.put("DONE")
            collect_thread = threading.Thread(target=collect_data)
            collect_thread.start()
            def generate():
                last_send_time = time.time()
                while True:
                    current_time = time.time()
                    if current_time - last_send_time >= KEEP_ALIVE_TIMEOUT:

                        yield ''
                        last_send_time = current_time
                    if not collect_thread.is_alive() and result is not None:
                        yield json.dumps(result)
                        break
                    time.sleep(0.1)
            return StreamingResponse(generate(), media_type="application/json")
    except HTTPException as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
    except Exception as exc:
        logger.error(f"[chat_completions] 未知异常: {exc}")
        return JSONResponse(status_code=500, content={"error": "Internal Server Error"})
    finally:
        if getattr(request.state, "use_config_token", False) and hasattr(request.state, "account"):
            release_account(request.state.account)

# ----------------------------------------------------------------------
# (11) 路由：/
# ----------------------------------------------------------------------
@app.get("/")
def index(request: Request):
    return templates.TemplateResponse("welcome.html", {"request": request})

# ----------------------------------------------------------------------
# 启动 FastAPI 应用（可用 uvicorn 启动）
# ----------------------------------------------------------------------
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=5001)