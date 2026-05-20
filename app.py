from flask import Flask, render_template, request, jsonify, send_file, Response, stream_with_context
from openai import OpenAI
import os
from datetime import datetime, timedelta
import json
import re
import html
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError


def _is_meaningful(text):
    """判断字段值是否为有意义的真实内容（而非占位符或空值）。"""
    if not text or not isinstance(text, str):
        return False
    if text.strip() in ('...', '..', '.', '—', '……', '无', '暂无', '待补充', '未提供', '暂无分析', ''):
        return False
    stripped = re.sub(r'[\s,，.。;；、:：\-–—]+', '', text)
    return len(stripped) >= 10


class IncompleteJSONError(ValueError):
    """AI 响应被截断导致 JSON 不完整时抛出，包含已接收的部分内容"""
    def __init__(self, message, partial_content):
        super().__init__(message)
        self.partial_content = partial_content

app = Flask(__name__)

# 存储会话数据
sessions = {}

CACHE_DIR = 'cache'
CACHE_TTL_HOURS = 24


def save_session_cache(session_id):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, f'{session_id}.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(sessions[session_id], f, ensure_ascii=False)
    print(f'[缓存] 已保存 {session_id}，句子数={len(sessions[session_id]["sentences"])}', flush=True)


def delete_session_cache(session_id):
    path = os.path.join(CACHE_DIR, f'{session_id}.json')
    if os.path.exists(path):
        os.remove(path)


def load_sessions_cache():
    if not os.path.exists(CACHE_DIR):
        return
    cutoff = datetime.now() - timedelta(hours=CACHE_TTL_HOURS)
    for fname in os.listdir(CACHE_DIR):
        if not fname.endswith('.json'):
            continue
        path = os.path.join(CACHE_DIR, fname)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            start_time = datetime.fromisoformat(data['start_time'])
            if start_time < cutoff:
                os.remove(path)
            else:
                session_id = fname[:-5]
                sessions[session_id] = data
        except Exception as e:
            print(f'[缓存] 加载 {fname} 失败: {e}')
    print(f'[缓存] 共加载 {len(sessions)} 个 session', flush=True)
    for sid, d in sessions.items():
        print(f'[缓存]   {sid}: {len(d.get("sentences", []))} 条句子', flush=True)


# ── gunicorn / 优雅退出：启动时加载缓存，退出时保存所有 session ──────────────────
load_sessions_cache()


def _graceful_shutdown(signum, frame):
    """收到 SIGTERM/SIGINT 时保存所有 session 再退出"""
    print(f'\n[退出] 收到信号 {signum}，正在保存 {len(sessions)} 个 session...', flush=True)
    for sid in list(sessions.keys()):
        try:
            save_session_cache(sid)
        except Exception as e:
            print(f'[退出] 保存 {sid} 失败: {e}', flush=True)
    print('[退出] 缓存已保存，程序退出', flush=True)
    import sys
    sys.exit(0)


import signal
signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT, _graceful_shutdown)


def load_api_key():
    """从config.txt文件读取API密钥"""
    config_file = 'config.txt'

    # 如果配置文件不存在，创建示例文件
    if not os.path.exists(config_file):
        with open(config_file, 'w', encoding='utf-8') as f:
            f.write('# 请在下面一行填入你的OpenAI API密钥\n')
            f.write('# 格式：OPENAI_API_KEY=sk-your-api-key-here\n')
            f.write('OPENAI_API_KEY=\n')
        print(f'已创建配置文件 {config_file}，请填入你的API密钥')
        return None

    # 读取配置文件
    config = {}
    with open(config_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                if '=' in line:
                    key, value = line.split('=', 1)
                    config[key.strip()] = value.strip()

    if not config.get('OPENAI_API_KEY'):
        print('错误：未在config.txt中找到有效的API密钥')
        return None

    return config

# 初始化OpenAI客户端
config = load_api_key()
client = None
backup_client = None
backup_model = 'gpt-5.1'
# 允许配置更大的 max_tokens，默认为 3000（该代理实际稳定上限约 2000-3000 token）
DEFAULT_MAX_TOKENS = int(config.get('MAX_TOKENS', '6000')) if config else 6000

if not config:
    print('警告：未设置API密钥，应用将无法正常工作')
    print('请在config.txt文件中设置OPENAI_API_KEY')
else:
    client = OpenAI(
        api_key=config['OPENAI_API_KEY'],
        base_url=config.get('OPENAI_BASE_URL', 'https://api.xuancat.cn/v1')
    )
    # 初始化备用客户端
    if config.get('BACKUP_API_KEY') and config.get('BACKUP_BASE_URL'):
        backup_client = OpenAI(api_key=config['BACKUP_API_KEY'], base_url=config['BACKUP_BASE_URL'])
        backup_model = config.get('BACKUP_MODEL', 'gpt-4o-mini')
        print(f'已配置备用API: {config["BACKUP_BASE_URL"]}')

# 主 API 故障状态
FALLBACK_DURATION = 30 * 60  # 30分钟（秒）
FALLBACK_STATE_FILE = 'cache/api_fallback_state.json'

def load_fallback_state():
    """加载故障状态"""
    if os.path.exists(FALLBACK_STATE_FILE):
        try:
            with open(FALLBACK_STATE_FILE, 'r') as f:
                data = json.load(f)
                return data.get('failed_at')
        except:
            pass
    return None

def save_fallback_state(failed_at):
    """保存故障状态"""
    os.makedirs('cache', exist_ok=True)
    with open(FALLBACK_STATE_FILE, 'w') as f:
        json.dump({'failed_at': failed_at}, f)

def clear_fallback_state():
    """清除故障状态"""
    if os.path.exists(FALLBACK_STATE_FILE):
        os.remove(FALLBACK_STATE_FILE)

primary_api_failed_at = load_fallback_state()

def call_api_with_fallback(messages, model, temperature=0.3, stream=False, timeout=90, max_tokens=None):
    """调用API：先试主API，失败再试备用，两边都失败才报错"""
    print(f'[API调用] model={model}, stream={stream}, timeout={timeout}s')

    # ── 主API ──────────────────────────────────────────────────────
    if client:
        try:
            print(f'[主API] 请求中...')
            result = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=stream,
                timeout=timeout,  # 使用传入的超时参数
                max_tokens=max_tokens,
            )
            print(f'[主API] 成功')
            return result
        except Exception as e:
            print(f'[主API] 失败: {type(e).__name__}: {e}')
    else:
        print(f'[主API] 未配置，跳过')

    # ── 备用API ──────────────────────────────────────────────────
    if backup_client:
        try:
            print(f'[备用API] 请求中...')
            actual_model = backup_model if model in ['gpt-5-mini', 'gpt-5.2', 'gpt-4o-mini', 'gpt-4o'] else model
            print(f'[备用API] 使用模型: {actual_model}')
            result = backup_client.chat.completions.create(
                model=actual_model,
                messages=messages,
                temperature=temperature,
                stream=stream,
                timeout=timeout,
                max_tokens=max_tokens,
            )
            print(f'[备用API] 成功')
            return result
        except Exception as e:
            print(f'[备用API] 失败: {type(e).__name__}: {e}')

    raise Exception('主API和备用API均不可用')


VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _repair_json_strings(content):
    """
    将 JSON 字符串值中的真实换行、未转义双引号、尾部反斜杠等修复为合法 JSON。

    状态机：
      outside(0) → 遇 " + 前导空白/分隔符 → key(1) 或 value(2)
      key(1)     → 遇 " → outside(0)
      value(2)   → 遇未转义 " → 转义; 遇合法闭合 → outside(0)
      任何状态 → 遇 : → 下一个 " 必为 value(2)，不在 key(1)
    """
    result = []
    i = 0
    n = len(content)
    # 0=outside, 1=key, 2=value
    state = 0
    # 遇到 : 后，下一个引号是 value
    next_is_value = False

    def _ends_with_odd_backslashes():
        count = sum(1 for ch in reversed(result) if ch == '\\')
        return count % 2 == 1

    while i < n:
        ch = content[i]

        # 冒号：下一个引号是 value 字符串
        if ch == ':':
            result.append(ch)
            next_is_value = True
            i += 1
            continue

        # 反斜杠处理（仅在 value 字符串内）
        if ch == '\\' and state == 2 and i + 1 < n:
            next_ch = content[i + 1]
            if next_ch in VALID_JSON_ESCAPES:
                result.append(ch); result.append(next_ch)
                i += 2
                continue
            elif next_ch == '"':
                result.append('\\"')
                i += 2
                continue
            elif next_ch == '\\':
                result.append('\\\\')
                i += 2
                continue
            elif next_ch in ' \n\r':
                result.append('\\' + next_ch)
                i += 2
                continue
            else:
                result.append('\\\\')
                i += 1
                continue

        # 引号处理
        if ch == '"':
            if state == 2:
                # 在 value 字符串内
                j = i + 1
                while j < n and content[j] in ' \t':
                    j += 1
                next_ch = content[j] if j < n else ''
                if next_ch in ',}':
                    # 字符串正常结束
                    result.append('"')
                    state = 0
                else:
                    # 值内部的未转义引号 → 转义
                    result.append('\\"')
            elif next_is_value:
                # 冒号后的第一个引号 → value 字符串开始
                result.append('"')
                state = 2
                next_is_value = False
            else:
                # key 字符串
                result.append('"')
                state = 1
            i += 1
            continue

        # 换行处理（仅在 value 内）
        if ch == '\n' and state == 2:
            result.append('\\n')
            i += 1
            continue
        if ch == '\r' and state == 2:
            i += 1
            continue

        # 遇到 , 或 } → 退出 key/value，重置状态
        if ch in ',}' and state in (1, 2):
            state = 0
            next_is_value = False
        # 遇到 [ 或 { → 重置
        if ch in '{[' and state != 0:
            state = 0

        result.append(ch)
        i += 1

    return ''.join(result)


def _smart_fix_truncated_json(content):
    """
    当 AI 响应被截断导致 JSON 不完整时，找到最后一条完整字段并智能补全。
    """
    # 先对原始 content 做一次修复，后续策略都在此基础上操作
    repaired = _repair_json_strings(content)

    # 策略1: append 简单补全（字段本身已完整，只是少了末尾的 }]
    for suffix in [']"}', '"}}', '"}', '"]}']:
        try:
            return json.loads(repaired + suffix)
        except json.JSONDecodeError:
            continue

    # 策略2: 找到 grammar_analysis 字段，将其值置为空字符串
    m = re.search(r'"grammar_analysis":\s*"[^"]*$', repaired)
    if m:
        truncated = repaired[:m.start()] + '"grammar_analysis": ""}'
        try:
            return json.loads(truncated)
        except json.JSONDecodeError:
            pass

    # 策略3: 找到最后一个完整字段（字段值以 ", 结尾）并截断
    # 逐字段从后向前搜索，找到值以 ", 结尾（字段完整）的位置
    for field_name in ('vocabulary_analysis', 'literal_translation', 'free_translation',
                       'error_reasons', 'synonyms_common', 'word_choice_reason',
                       'synonyms_mnemonic', 'highlighted_fragments'):
        idx = repaired.rfind(f'"{field_name}": "')
        if idx < 0:
            continue
        # 从字段名位置往后，用状态机找到第一个配对的未转义引号对（value 结尾）
        pos = idx + len(field_name) + 4  # 跳过字段名 + '": "'
        depth = 0
        in_string = False
        i = pos
        value_end = -1
        while i < len(repaired):
            ch = repaired[i]
            if not in_string:
                if ch == '"':
                    in_string = True
                elif ch == '[':
                    depth += 1
                elif ch == ']':
                    depth -= 1
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                elif ch == ',' and depth == 0:
                    value_end = i
                    break
                elif ch == '}' and depth == 0:
                    value_end = i
                    break
            else:
                if ch == '\\':
                    i += 2
                    continue
                elif ch == '"':
                    in_string = False
            i += 1

        if value_end >= 0:
            truncated = repaired[:value_end + 1] + '}'
            try:
                return json.loads(truncated)
            except json.JSONDecodeError:
                pass

    # 策略4: 处理不完整的【/H】标记（JSON截断时】可能被切掉）
    m_incomplete = re.search(r'\【[^】]*$', repaired)
    if m_incomplete:
        repaired = repaired[:m_incomplete.start()] + '"}'
        try:
            return json.loads(repaired)
        except json.JSONDecodeError:
            pass

    return None


def _extract_sentence_results_from_raw(content):
    """
    当 JSON 格式损坏无法解析时，从原始内容中正则提取每条句子的结果。
    适用于 generate_sentences 端点的 {"results": [...]} 格式。
    """
    import html
    results = []
    # 去除 markdown 代码块
    content = re.sub(r'^```(?:json)?\s*\n?', '', content, flags=re.MULTILINE)
    content = re.sub(r'\n?```\s*$', '', content, flags=re.MULTILINE)

    # 匹配每条结果块：包含 word, chinese, valid, sentence 等字段
    # 策略：找每个 "word": "..." 的位置，然后向前找到 { 向后找到 },
    pattern = re.compile(
        r'\{[^}]*?"word"\s*:\s*"((?:[^"\\]|\\.)*)"[^}]*?"chinese"\s*:\s*"((?:[^"\\]|\\.)*)"[^}]*?"valid"\s*:\s*(true|false)[^}]*?"sentence"\s*:\s*(null|"((?:[^"\\]|\\.)*)")',
        re.DOTALL
    )
    for m in pattern.finditer(content):
        word, chinese, valid_str, sentence_raw, sentence = m.groups()
        word = html.unescape(word.replace('\\"', '"').replace('\\n', '\n'))
        chinese = html.unescape(chinese.replace('\\"', '"').replace('\\n', '\n'))
        valid = valid_str == 'true'
        sentence_val = None
        if sentence:
            sentence_val = html.unescape(sentence.replace('\\"', '"').replace('\\n', '\n'))
        results.append({
            'word': word,
            'chinese': chinese,
            'valid': valid,
            'sentence': sentence_val,
            'meanings': None
        })
    return results if results else None


def _extract_fields_one_by_one(content):
    """
    当 JSON 格式损坏但所有字段都已存在于内容中时，逐字段正则提取。
    处理字符串值内含换行、未转义双引号等格式问题。
    """
    ALL_FIELDS = [
        'has_errors', 'errors', 'error_reasons', 'suggested_sentence',
        'vocabulary_analysis', 'grammar_analysis', 'literal_translation',
        'free_translation', 'synonyms_common', 'word_choice_reason',
        'synonyms_mnemonic', 'highlighted_fragments', 'translation_highlights',
    ]
    result = {}
    i = 0
    n = len(content)

    while i < n:
        # 找下一个字段名
        field_match = re.search(r'"(\w+)":\s*', content[i:])
        if not field_match:
            break
        field_name = field_match.group(1)
        if field_name not in ALL_FIELDS:
            i += field_match.end()
            continue
        i += field_match.end()

        # 跳过冒号后的空格
        while i < n and content[i] in ' \t\n':
            i += 1
        if i >= n:
            break

        ch = content[i]

        if ch == '{':
            # 对象：找配对 }
            depth = 0
            start = i
            for j, c in enumerate(content[i:], i):
                if c == '{':
                    depth += 1
                elif c == '}':
                    depth -= 1
                    if depth == 0:
                        raw = content[start:j+1]
                        try:
                            result[field_name] = json.loads(raw)
                        except Exception:
                            return None
                        i = j + 1
                        break
        elif ch == '[':
            # 数组：找配对 ]
            depth = 0
            start = i
            for j, c in enumerate(content[i:], i):
                if c == '[':
                    depth += 1
                elif c == ']':
                    depth -= 1
                    if depth == 0:
                        raw = content[start:j+1]
                        try:
                            result[field_name] = json.loads(raw)
                        except Exception:
                            return None
                        i = j + 1
                        break
        elif ch == '"':
            # 字符串：找配对引号（处理转义）
            start = i
            i += 1
            while i < n:
                if content[i] == '\\':
                    i += 2
                    continue
                if content[i] == '"':
                    i += 1
                    break
                i += 1
            raw = content[start:i]
            try:
                result[field_name] = json.loads(raw)
            except Exception:
                return None
        else:
            # 数字 / bool / null
            start = i
            while i < n and content[i] not in ',} \t\n':
                i += 1
            raw = content[start:i].strip()
            if raw == 'true':
                result[field_name] = True
            elif raw == 'false':
                result[field_name] = False
            elif raw == 'null':
                result[field_name] = None
            else:
                try:
                    result[field_name] = json.loads(raw)
                except Exception:
                    return None

        # 跳过逗号
        while i < n and content[i] in ' \t\n':
            i += 1
        if i < n and content[i] == ',':
            i += 1

    return result if result else None


def extract_json(content):
    """从 AI 响应中提取并解析 JSON，兼容常见格式问题"""
    content = content.strip()

    # 去除 markdown 代码块标记
    content = re.sub(r'^```(?:json)?\s*\n?', '', content)
    content = re.sub(r'\n?```\s*$', '', content)
    content = content.strip()

    # 移除 AI 可能生成的 JavaScript 风格字符串拼接
    content = re.sub(r'"\s*\+\s*\n\s*"', '', content)
    content = re.sub(r'"\s*\+\s*"', '', content)

    # 直接尝试解析
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f'[JSON] 直接解析失败: {e}')

    # 提取 JSON 对象范围：找第一个 { 开始，逐字符计数平衡，找到第一个配对的 }
    start = content.find('{')
    if start == -1:
        print(f'[JSON] 未找到 JSON 开始标记')
        raise ValueError(f"无法解析 AI 返回的 JSON 内容：{content[:500]}")

    depth = 0
    end = -1
    for i, ch in enumerate(content[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i
                break

    extracted = content[start:end + 1] if end != -1 else content[start:]

    # 如果 JSON 对象未闭合（AI 响应被截断），智能找到最后一条完整字段并截断补全
    if end == -1:
        print(f'[JSON] JSON 对象未闭合，进行智能修复...')
        repaired = _smart_fix_truncated_json(extracted)
        if repaired:
            print(f'[JSON] 智能修复成功（截断字段补全）')
            return repaired

    # 用字符串修复函数处理
    fixed = _repair_json_strings(extracted)

    try:
        return json.loads(fixed)
    except json.JSONDecodeError as e:
        print(f'[JSON] 字符串修复后仍失败: {e}')

    # 尝试把 \n 全部替换为空格后再解析（极端兜底）
    try:
        return json.loads(re.sub(r'\n', ' ', fixed))
    except json.JSONDecodeError as e:
        print(f'[JSON] 去除换行后仍失败: {e}')

    # 内容格式有误（如字符串内有未转义换行），但可能已经完整。
    # 在抛出截断错误之前，先检查字段是否已全部存在：
    # 若所有核心字段都已出现在内容中，则对 fixed 做最后一次性暴力解析（逐字段提取）。
    CORE_FIELDS = {
        'vocabulary_analysis', 'grammar_analysis',
        'literal_translation', 'free_translation',
    }
    present = {f for f in CORE_FIELDS if f in fixed}
    missing = CORE_FIELDS - present
    if not missing:
        print(f'[JSON] 字段已完整但格式有误，尝试逐字段提取...')
        result = _extract_fields_one_by_one(fixed)
        if result:
            print(f'[JSON] 逐字段提取成功')
            return result

    print(f'[JSON] 无法解析，原始内容（前1000字）: {content[:1000]}')
    raise IncompleteJSONError(
        f"无法解析 AI 返回的 JSON 内容：{content[:500]}",
        partial_content=content
    )


# ── Prompt 管理 ────────────────────────────────────────────────────────────────

class PromptManager:
    """从 prompts/{lang}.txt 加载 prompt 模板，按 {{PLACEHOLDER}} 替换变量。"""

    def __init__(self, prompts_dir='prompts'):
        self.prompts_dir = prompts_dir
        self._cache = {}   # {(lang, section): content}

    def _load(self, lang, section):
        key = (lang, section)
        if key in self._cache:
            return self._cache[key]

        path = os.path.join(self.prompts_dir, f'{lang}.txt')
        try:
            with open(path, encoding='utf-8') as f:
                content = f.read()
        except FileNotFoundError:
            raise RuntimeError(f"Prompt file not found: {path}")

        # 格式：== SECTION_NAME ==\n<body>，用 == 前缀行分割
        parts = re.split(r'\n(?==)', content)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            # 匹配开头的 == NAME ==
            m = re.match(r'^==\s+(.+?)\s+==\s*\n', part)
            if not m:
                continue
            header = m.group(1).strip()
            body = part[m.end():]
            self._cache[(lang, header)] = body.strip()

        if key not in self._cache:
            raise RuntimeError(f"Section '{section}' not found in {path}")
        return self._cache[key]

    def get(self, lang, section, **kwargs):
        """返回填充后的 prompt 字符串，未填占位符自动清空。"""
        template = self._load(lang, section)
        for k, v in kwargs.items():
            template = template.replace('{{' + k + '}}', v)
        template = re.sub(r'\{\{[A-Z_]+\}\}', '', template)
        return template


_prompt_manager = PromptManager()


# ── 句子分析 ─────────────────────────────────────────────────────────────────

def _build_analysis_parts(sentence, language, target_word=None, synonyms_info=None):
    """组装分析 prompt 所需的各片段。"""
    lang = '日语' if language == 'ja' else '英语'
    lang_specific = '助词、活用形' if language == 'ja' else '时态、冠词、介词搭配'

    # 近义词辨析（可选）
    synonyms_block = ''
    synonyms_json_field = ''
    synonyms_instruction = ''
    if synonyms_info:
        words_str = '、'.join(f'"{w}"' for w in synonyms_info['words'])
        current_word = target_word if target_word and target_word in synonyms_info['words'] else synonyms_info['words'][0]
        other_words = [w for w in synonyms_info['words'] if w != current_word]
        other_words_str = '、'.join(f'"{w}"' for w in other_words)
        synonyms_block = f'\n【近义词组】{words_str}'
        synonyms_json_field = (
            '\n    "synonyms_common": "近义词共同辨析",\n'
            '    "word_choice_reason": "选词理由",\n'
            '    "synonyms_mnemonic": "记忆口诀",')
        synonyms_instruction = f"""
7. synonyms_common：用中文说明 {words_str} 的核心含义区别，并说明在本句中两者能否互换，口语化，不堆术语。格式："两者在本句中[能/不能]互换，因为……"。全部用中文书写
8. word_choice_reason：用中文回答本句为何选用"{current_word}"。要求：
   - 如果"{current_word}"和{other_words_str}在本句中可以互换，语义和语法都成立，则写"【可互换】{current_word}和{other_words_str}在本句中均可使用，均表达XX意思，两者语体差异为XX"
   - 如果两者有明显差异，则说明"{current_word}"在本句中是什么词性/起什么语法作用，说明为什么符合本句的语法结构和语境，说明换成{other_words_str}会破坏什么（语法错误/语义不通/搭配不当）
   - 结合本句具体语境回答，不要只说通用区别。全部用中文书写
9. synonyms_mnemonic：用"词 → 核心含义"格式，每词一行，只写对照表"""

    # 词汇分析
    vocab_instruction = "词汇分析：只分析实词（名词、动词、形容词、副词），跳过代词、冠词、基础介词/连词/助动词等功能词。每个词一行，格式：词语（读音）：词性，释义及用法。保留日语原文和读音，但解释说明（词性、释义、用法）必须全部用中文。不要把一个词写成多个段落。"
    if target_word:
        vocab_instruction += f'\n注意：词汇分析中必须包含对"{target_word}"的分析，若句子中出现了该词则必须将其列在词汇分析第一条，格式与其他词汇一致'

    # 错误修正
    target_req = f'，且必须包含"{target_word}"' if target_word else ''
    error_instruction = f"""若有错误：errors列出错误（仅限词汇搭配错误、语法错误、时态/活用形错误等真正的语言错误），error_reasons说明原因，suggested_sentence给出一个修正句（{lang_specific}正确、地道自然、保留原意{target_req}）
若无错误：省略errors、error_reasons、suggested_sentence三个字段
注意：近义词间的语义差异（如"与XX词相比语义侧重不同"、"换成XX词意思会有细微变化"）不属于错误，不要列入errors。近义词辨析内容只在word_choice_reason字段中说明"""

    # 翻译高亮
    translate_instruction = "直译和意译：必须全部用中文，严禁出现任何英文单词或日文字符（包括原文词汇、专有名词、人名、地名等），一律翻译成中文"
    if target_word:
        translate_instruction += f"""
   ================================================================================
   ★★★【强制高亮要求 - 绝对不得跳过】★★★
   分两步执行：
   第一步：先写出直译(literal_translation)和意译(free_translation)，全部用中文。
   第二步：在写好的直译和意译中，找到目标词「{target_word}」对应的中文词语，
           用【H】...【/H】标记，并在 translation_highlights 中报告。
   规则：
   - 每个翻译中必须至少有一个【H】标记，禁止省略。
   - 例：目标词"明らか"，先写直译"【H】明显【/H】是显而易见的"、意译"这件事【H】显然【/H】是正确的"，
     再报告 translation_highlights={{"literal": ["明显"], "free": ["显然"]}}
   - 例：目标词"赤い"，先写直译"这朵花是【H】红的【/H】"、意译"这朵花呈【H】红色【/H】"，
     再报告 translation_highlights={{"literal": ["红的"], "free": ["红色"]}}
   - 直译必须保留目标词的最直接翻译（不得省略或用其他词绕过）。
   - 意译允许换词表达，但不得完全省略目标词的含义。
   ================================================================================"""


    # 活用形/屈折检测
    inflection_rule = ''
    if target_word:
        if language == 'ja':
            inflection_rule = (
                f'7. 活用形检测：若句子中出现了"{target_word}"的活用变形（如授予动词的て形"付き"、可能形、被动形、被动态、使役形等），'
                f'在词汇分析该条目的行尾追加"【活用形：XXX】"，如"付け加える（つけくわえる）：动词，给予补充。'
                f'用法：～ていく表示逐步…下去。【活用形：付け加え】"\n'
                '   若目标词本身以原形出现无需标记，只需标记活用变形。仅标注，不解释变形规律。'
            )
        elif language == 'en':
            inflection_rule = (
                f'7. Inflection detection: If the sentence contains an inflected form of "{target_word}" '
                f'(e.g., third-person "runs", past "ran", past participle "run", present participle "running", '
                f'comparative/superlative forms, plural forms for nouns), append "【活用形：XXX】" at the end '
                f'of that vocabulary entry. Example: "run (/riːtʃ/)（ran）: verb, reach. '
                f'【活用形：running】"\n'
                '   Only mark inflected forms, not the base form itself. Mark only, do not explain inflection rules.'
            )

    return {
        'lang': lang,
        'lang_specific': lang_specific,
        'synonyms_block': synonyms_block,
        'synonyms_json_field': synonyms_json_field,
        'synonyms_instruction': synonyms_instruction,
        'vocab_instruction': vocab_instruction,
        'error_instruction': error_instruction,
        'translate_instruction': translate_instruction,
        'inflection_rule': inflection_rule,
    }
def build_prompt(sentence, language, target_word=None, synonyms_info=None):
    """生成完整的分析 prompt。"""
    parts = _build_analysis_parts(sentence, language, target_word, synonyms_info)

    system_msg = _prompt_manager.get(language, 'SYSTEM_MSG')

    json_out = f"""输出JSON：
{{
    "has_errors": true/false,
    "errors": ["..."],
    "error_reasons": "...",
    "suggested_sentence": "...",
    "highlighted_fragments": ["乱れている"],
    "translation_highlights": {{"literal": ["红的"], "free": ["红色的"]}},
    {parts['synonyms_json_field']}
    "vocabulary_analysis": "...",
    "grammar_analysis": "...",
    "literal_translation": "...",
    "free_translation": "..."
}}"""

    # 动态编号要求（活用形规则插入 #7，正文 #7 → #8）
    reqs = [
        "判断句子是否有词汇、语法或搭配错误",
        parts['error_instruction'],
        parts['vocab_instruction'],
        f"语法分析：简明列出各语法要点，每点一段，段落间空一行。保留{parts['lang']}原文、语法术语、例子，但所有解释说明必须全部用中文。不加编号标题，不展开写长篇解说文章",
        parts['translate_instruction'],
        f"无论是否有错误，都必须完成词汇分析、语法分析、直译和意译{parts['synonyms_instruction']}",
    ]
    if parts['inflection_rule']:
        reqs.insert(6, parts['inflection_rule'])
    # 高亮片段指令（始终插入，保持编号连续）
    if target_word:
        ja_instr = (
            f'【强制要求】高亮标记：必须在 highlighted_fragments 中列出原句中目标词"{target_word}"的精确字符片段（包含其后的格助词/功能词）。'
            f'步骤：①在原句中搜索目标词"{target_word}"；②记录该词及其直接后续成分（如"くて"、"く"、"ません"）；'
            f'③只报告该词相关的片段，禁止报告其他词汇。'
            f'示例：原句"今日は山がくっきり見えます"，目标词"くっきり"，fragments=["くっきり"]；'
            f'原句"彼のミスは明らかでした"，目标词"明らか"，fragments=["明らか"]；'
            f'原句"勘弁してくれ"，目标词"勘弁"，fragments=["勘弁し"]或["勘弁して"]。'
            f'【禁止】highlighted_fragments 不得为空数组，必须包含至少一个片段。'
            f'另：必须同时在 translation_highlights 中报告直译和意译中哪些片段对应当前目标词的意思（用中文）。'
            f'★【绝对强制】translation_highlights.literal 和 translation_highlights.free 不得为空数组，每个至少包含一个片段。'
            f'★ 若意译和直译中目标词的中文翻译相同，也必须分别在 literal 和 free 中各写一次。'
            f'示例：目标词"赤い"，直译"这本书是【H】红的【/H】"，意译"这本书是【H】红色的【/H】"，则 translation_highlights={{"literal": ["红的"], "free": ["红色的"]}}。'
        )
        en_instr = (
            f'[MANDATORY] Highlight: you MUST list the exact fragments of the target word "{target_word}" that appear in the sentence in highlighted_fragments (e.g. "running" for "run", "clearly" for "obvious"). '
            f'Rule: search for "{target_word}" in the sentence, record the word and any directly attached suffixes (e.g. "-ly", "-ed", "-ing"). '
            f'NEVER leave highlighted_fragments as an empty array. '
            f'Do NOT include fragments of other words. '
            f'Also report in translation_highlights which Chinese phrases in literal_translation and free_translation correspond to "{target_word}" meaning, '
            f'e.g. translation_highlights={{"literal": ["红的"], "free": ["红色的"]}}. '
            f'Both literal and free arrays MUST have at least one entry each. '
            f'If the Chinese translation is the same in both, still list it in both arrays.'
        )
        reqs.append(ja_instr if language == 'ja' else en_instr)

    req_lines = '\n'.join(f"{i+1}. {r}" for i, r in enumerate(reqs))

    user_msg = f"""分析以下{parts['lang']}句子：{parts['synonyms_block']}

"{sentence}"

{json_out}
要求：
{req_lines}

【强制禁止】任何字段的值都不得使用"..."、".."、"—"、"……"等占位符。每个字段必须输出真实内容，即使内容简短也不得留空。若不确定某个词的分析，直接写上最常见的解释即可。"""

    return {'system': system_msg, 'user': user_msg}


SYSTEM_MSG = None   # 由 PromptManager 动态获取，保留向后兼容


@app.route('/analyze_stream', methods=['POST'])
def analyze_stream():
    """流式分析句子"""
    data = request.json
    session_id = data.get('session_id')
    sentence = data.get('sentence')
    print(f'[analyze] session_id={session_id!r}  sentence={sentence!r}  sessions_keys={list(sessions.keys())}', flush=True)
    language = data.get('language', 'ja')
    model = data.get('model', 'gpt-5-mini')
    target_word = data.get('target_word')  # 造句时的目标单词（可选）
    synonyms_info = data.get('synonyms_info')  # 近义词信息（可选）
    reuse_synonyms = data.get('reuse_synonyms')  # 复用的近义词辨析（可选）

    if not session_id or session_id not in sessions:
        print(f'[analyze] ERROR: session_id={session_id!r} not in sessions', flush=True)
        return jsonify({'error': '无效的会话ID'}), 400
    if not sentence:
        return jsonify({'error': '句子不能为空'}), 400

    if not client:
        def gen_err():
            yield f"data: {json.dumps({'type': 'error', 'message': '未配置API密钥，请在config.txt中设置OPENAI_API_KEY'}, ensure_ascii=False)}\n\n"
        return Response(gen_err(), content_type='text/event-stream',
                       headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    # 如果有可复用的辨析，synonyms_common 和 synonyms_mnemonic 从缓存注入，
    # 但 word_choice_reason 仍需每个句子单独生成
    prompt_synonyms = synonyms_info  # 始终传递，让 AI 生成 word_choice_reason
    prompt_data = build_prompt(sentence, language, target_word, prompt_synonyms)

    def generate():
        full_content = ""
        yield f"data: {json.dumps({'type': 'start'}, ensure_ascii=False)}\n\n"
        try:
            print(f'[分析] 开始调用API，model={model}')
            stream = call_api_with_fallback(
                messages=[
                    {"role": "system", "content": prompt_data['system']},
                    {"role": "user", "content": prompt_data['user']}
                ],
                model=model,
                temperature=0.3,
                stream=True,
                timeout=180,
                max_tokens=DEFAULT_MAX_TOKENS
            )
            print(f'[分析] 获得流对象，开始读取')
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    full_content += delta
                    yield f"data: {json.dumps({'type': 'chunk', 'content': delta}, ensure_ascii=False)}\n\n"

            # 解析完整 JSON
            content = full_content.strip()
            print(f'[分析] 收到响应，长度: {len(content)} 字符')
            if not content:
                yield f"data: {json.dumps({'type': 'error', 'message': 'AI 返回空内容，请重试'}, ensure_ascii=False)}\n\n"
                return

            # 检查是否是完整的 JSON（应该以 } 结尾）
            if not content.endswith('}'):
                print(f'[分析] 响应不以 }} 结尾，尝试修复截断...')
                # 不再直接报错，让 extract_json 尝试修复

            result = extract_json(content)
            print(f'[DEBUG] highlighted_fragments = {result.get("highlighted_fragments", "KEY_MISSING")}')

            # ── 占位符兜底：若 AI 偷懒/截断但 JSON 仍可解析，补全 API 不会自动触发，手动强制触发 ──
            # 智能判断：显式占位符 + 空字符串 + 有效内容不足 5 字符
            def _is_meaningful(text):
                if not text or not isinstance(text, str):
                    return False
                explicit_placeholders = ('...', '..', '.', '—', '……', '无', '暂无', '待补充', '未提供', '暂无分析')
                if text in explicit_placeholders:
                    return False
                stripped = re.sub(r'[\s,，.。;；、:：\-–—]+', '', text)
                return len(stripped) >= 5

            placeholder_keys = ('vocabulary_analysis', 'grammar_analysis', 'literal_translation', 'free_translation')
            bad_fields = [k for k in placeholder_keys if not _is_meaningful(result.get(k))]
            if bad_fields:
                print(f'[分析] 检测到无效字段: {bad_fields}，vocab={result.get("vocabulary_analysis")!r}  grammar={result.get("grammar_analysis")!r}')
                raise IncompleteJSONError(
                    f"AI 输出无效字段: {bad_fields}",
                    partial_content=content
                )

            # 补齐省略的错误字段（prompt 要求无错误时省略）
            result.setdefault('has_errors', False)
            result.setdefault('errors', [])
            result.setdefault('error_reasons', '')
            result.setdefault('suggested_sentence', '')
            result.setdefault('synonyms_common', '')
            result.setdefault('word_choice_reason', '')
            result.setdefault('highlighted_fragments', [])
            result.setdefault('translation_highlights', {})
            for key in ('vocabulary_analysis', 'grammar_analysis', 'literal_translation', 'free_translation', 'error_reasons', 'synonyms_common', 'word_choice_reason', 'synonyms_mnemonic'):
                if key in result and isinstance(result[key], str):
                    result[key] = result[key].replace('\\n', '\n')

            # ── 高亮验证 & 强制修复 ─────────────────────────────────────────────
            # 如果 AI 漏标了直译/意译中的【H】，在后处理阶段强制注入
            if target_word:
                result = _validate_and_fix_translation_highlights(
                    result, target_word, sentence
                )

            # 注入复用的近义词共同辨析（不注入 word_choice_reason，让 AI 为每个句子单独生成）
            if reuse_synonyms:
                result['synonyms_common'] = reuse_synonyms.get('synonyms_common', '')
                result['synonyms_mnemonic'] = reuse_synonyms.get('synonyms_mnemonic', '')

            # 保存统一由前端 showResult() 调用 /save_sentence 完成
            # （后端不再在此处保存，避免双重保存导致重复卡片）

            yield f"data: {json.dumps({'type': 'done', 'result': result}, ensure_ascii=False)}\n\n"

        except GeneratorExit:
            pass
        except IncompleteJSONError as e:
            print(f'[分析] JSON 格式损坏，尝试逐字段提取+智能补全，原始内容长度: {len(e.partial_content)} 字符')
            yield f"data: {json.dumps({'type': 'fixing'}, ensure_ascii=False)}\n\n"

            # 第一步：尝试逐字段提取
            extracted = _extract_fields_one_by_one(e.partial_content)
            if extracted:
                print(f'[分析] 逐字段提取成功: {list(extracted.keys())}')

            # 核心字段集合
            CORE_FIELDS = {'has_errors', 'errors', 'error_reasons', 'suggested_sentence',
                           'vocabulary_analysis', 'grammar_analysis', 'literal_translation',
                           'free_translation', 'synonyms_common', 'word_choice_reason',
                           'synonyms_mnemonic', 'highlighted_fragments', 'translation_highlights'}
            extracted_present = set(extracted.keys()) if extracted else set()
            missing_fields = [f for f in CORE_FIELDS if f not in extracted_present]
            print(f'[分析] 已提取字段: {extracted_present}, 缺失字段: {missing_fields}')

            # vocabulary/grammar/literal/free 四个核心分析字段损坏率最高，
            # 不管是否提取到都要求补全模型重新生成；其他字段只补缺失的
            FORCE_FILL = {'vocabulary_analysis', 'grammar_analysis', 'literal_translation', 'free_translation'}
            must_fill = [f for f in FORCE_FILL] + [f for f in missing_fields if f not in FORCE_FILL]

            # ── 补全核心分析字段（支持一次重试）──────────────────────────────────
            CORE_4 = ('vocabulary_analysis', 'grammar_analysis', 'literal_translation', 'free_translation')

            def _do_completion():
                """调用补全 API 并返回解析后的 JSON。"""
                complete_model = 'gpt-4o-mini'
                print(f'[分析] 使用 {complete_model} 补全字段: {must_fill}')

                ctx_lines = []
                for f in sorted(extracted_present):
                    v = extracted[f]
                    snippet = json.dumps(v, ensure_ascii=False)[:300]
                    ctx_lines.append(f'  {f}: {snippet}')
                context_str = '\n'.join(ctx_lines) if ctx_lines else '  （无已提取字段）'

                complete_resp = call_api_with_fallback(
                    messages=[
                        {"role": "system", "content": "你是 AI 语言学习助手。只输出纯 JSON，字段名用双引号，字符串值也用双引号，不输出任何解释。"},
                        {"role": "user", "content": f"""参考以下 AI 生成的句子分析 JSON（格式损坏，需要重新生成）。

【绝对强制要求】每个字段必须有真实、完整的内容，不得使用"..."、"暂无"等占位符。

目标句子：{sentence}
{"目标单词：" + target_word if target_word else ""}

已提取的参考字段：
{context_str}

请为以下字段重新生成完整 JSON。只输出 JSON 对象，不要有任何解释。
要求：
- vocabulary_analysis：详细分析句子中每个实词的词性、释义、用法，不少于80字
- grammar_analysis：详细分析句子语法结构，不少于80字
- literal_translation：逐词直译，目标词用【H】【/H】标出，不少于30字
- free_translation：通顺意译，目标词用【H】【/H】标出，不少于50字
- synonyms_common/word_choice_reason/synonyms_mnemonic 也一并补全

JSON格式（无语法错误）：
{{"vocabulary_analysis": "内容...", "grammar_analysis": "内容...", "literal_translation": "内容...", "free_translation": "内容...", "synonyms_common": "", "word_choice_reason": "", "synonyms_mnemonic": "", "has_errors": false, "errors": [], "error_reasons": "", "suggested_sentence": "", "highlighted_fragments": ["片段"], "translation_highlights": {{"literal": [], "free": []}}}}"""}
                    ],
                    model=complete_model,
                    temperature=0.3,
                    stream=False,
                    timeout=120,
                    max_tokens=4000
                )
                raw = complete_resp.choices[0].message.content.strip()
                raw = re.sub(r'^```(?:json)?\s*\n?', '', raw)
                raw = re.sub(r'\n?```\s*$', '', raw).strip()
                print(f'[补全] 原始返回: {raw[:300]}')
                return json.loads(raw)

            # 先尝试一次，若质量不达标则重试
            try:
                complete_result = _do_completion()
            except json.JSONDecodeError as je:
                print(f'[补全] 解析失败: {je}，重试...')
                try:
                    complete_result = _do_completion()
                except Exception as retry_err:
                    print(f'[补全] 重试也失败: {retry_err}')
                    if extracted:
                        yield f"data: {json.dumps({'type': 'fixed', 'result': extracted}, ensure_ascii=False)}\n\n"
                        return
                    yield f"data: {json.dumps({'type': 'error', 'message': f'补全解析失败: {retry_err}'}, ensure_ascii=False)}\n\n"
                    return

            # 质量检查：4 个核心字段必须全部通过 _is_meaningful
            bad = [k for k in CORE_4 if not _is_meaningful(complete_result.get(k, ''))]
            if bad:
                print(f'[补全] 质量不达标: {bad}，重试一次...')
                try:
                    complete_result = _do_completion()
                except Exception as retry_err:
                    print(f'[补全] 重试也失败: {retry_err}')
                    if extracted:
                        yield f"data: {json.dumps({'type': 'fixed', 'result': extracted}, ensure_ascii=False)}\n\n"
                        return
                    yield f"data: {json.dumps({'type': 'error', 'message': f'补全解析失败: {retry_err}'}, ensure_ascii=False)}\n\n"
                    return
                bad = [k for k in CORE_4 if not _is_meaningful(complete_result.get(k, ''))]
                if bad:
                    print(f'[补全] 重试后仍不达标: {bad}，回退到提取结果')
                    if extracted:
                        yield f"data: {json.dumps({'type': 'fixed', 'result': extracted}, ensure_ascii=False)}\n\n"
                        return
                    yield f"data: {json.dumps({'type': 'error', 'message': f'补全后仍含空字段: {bad}，请重试'}, ensure_ascii=False)}\n\n"
                    return

            # 合并：已提取字段优先，补全结果填充空缺
            merged = dict(extracted) if extracted else {}
            for k, v in complete_result.items():
                existing = merged.get(k, '')
                if not _is_meaningful(str(existing)):
                    merged[k] = v

            print(f'[分析] 合并完成，字段: {list(merged.keys())}')
            yield f"data: {json.dumps({'type': 'fixed', 'result': merged}, ensure_ascii=False)}\n\n"
            return

        except GeneratorExit:
            pass
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
            return

    return Response(
        stream_with_context(generate()),
        content_type='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}
    )

def _get_inflections(word):
    """返回单词的所有活用/屈折形式（含原形），用于锚点匹配的兜底。"""
    INFLECTIONS = {
        'be': {'is', 'am', 'are', 'was', 'were', 'been', 'being', "'s", "'re", "'m"},
        'have': {'has', 'had', 'having'},
        'do': {'does', 'did', 'done', 'doing'},
        'say': {'says', 'said', 'saying'},
        'get': {'gets', 'got', 'getting', 'gotten'},
        'go': {'goes', 'went', 'gone', 'going'},
        'make': {'makes', 'made', 'making'},
        'know': {'knows', 'knew', 'known', 'knowing'},
        'take': {'takes', 'took', 'taken', 'taking'},
        'see': {'sees', 'saw', 'seen', 'seeing'},
        'come': {'comes', 'came', 'coming'},
        'think': {'thinks', 'thought', 'thinking'},
        'look': {'looks', 'looked', 'looking'},
        'want': {'wants', 'wanted', 'wanting'},
        'use': {'uses', 'used', 'using'},
        'find': {'finds', 'found', 'finding'},
        'give': {'gives', 'gave', 'given', 'giving'},
        'tell': {'tells', 'told', 'telling'},
        'work': {'works', 'worked', 'working'},
        'call': {'calls', 'called', 'calling'},
        'try': {'tries', 'tried', 'trying'},
        'ask': {'asks', 'asked', 'asking'},
        'need': {'needs', 'needed', 'needing'},
        'feel': {'feels', 'felt', 'feeling'},
        'become': {'becomes', 'became', 'becoming'},
        'leave': {'leaves', 'left', 'leaving'},
        'put': {'puts', 'putting'},
        'mean': {'means', 'meant', 'meaning'},
        'keep': {'keeps', 'kept', 'keeping'},
        'let': {'lets', 'letting'},
        'begin': {'begins', 'began', 'begun', 'beginning'},
        'seem': {'seems', 'seemed', 'seeming'},
        'help': {'helps', 'helped', 'helping'},
        'show': {'shows', 'showed', 'shown', 'showing'},
        'hear': {'hears', 'heard', 'hearing'},
        'play': {'plays', 'played', 'playing'},
        'run': {'runs', 'ran', 'running'},
        'move': {'moves', 'moved', 'moving'},
        'live': {'lives', 'lived', 'living'},
        'believe': {'believes', 'believed', 'believing'},
        'hold': {'holds', 'held', 'holding'},
        'bring': {'brings', 'brought', 'bringing'},
        'write': {'writes', 'wrote', 'written', 'writing'},
        'sit': {'sits', 'sat', 'sitting'},
        'stand': {'stands', 'stood', 'standing'},
        'lose': {'loses', 'lost', 'losing'},
        'pay': {'pays', 'paid', 'paying'},
        'meet': {'meets', 'met', 'meeting'},
        'include': {'includes', 'included', 'including'},
        'continue': {'continues', 'continued', 'continuing'},
        'set': {'sets', 'setting'},
        'learn': {'learns', 'learned', 'learning'},
        'change': {'changes', 'changed', 'changing'},
        'lead': {'leads', 'led', 'leading'},
        'understand': {'understands', 'understood', 'understanding'},
        'watch': {'watches', 'watched', 'watching'},
        'follow': {'follows', 'followed', 'following'},
        'stop': {'stops', 'stopped', 'stopping'},
        'create': {'creates', 'created', 'creating'},
        'speak': {'speaks', 'spoke', 'spoken', 'speaking'},
        'read': {'reads', 'reading'},
        'spend': {'spends', 'spent', 'spending'},
        'grow': {'grows', 'grew', 'grown', 'growing'},
        'open': {'opens', 'opened', 'opening'},
        'walk': {'walks', 'walked', 'walking'},
        'win': {'wins', 'won', 'winning'},
        'teach': {'teaches', 'taught', 'teaching'},
        'offer': {'offers', 'offered', 'offering'},
        'remember': {'remembers', 'remembered', 'remembering'},
        'love': {'loves', 'loved', 'loving'},
        'consider': {'considers', 'considered', 'considering'},
        'appear': {'appears', 'appeared', 'appearing'},
        'buy': {'buys', 'bought', 'buying'},
        'wait': {'waits', 'waited', 'waiting'},
        'serve': {'serves', 'served', 'serving'},
        'die': {'dies', 'died', 'dying'},
        'send': {'sends', 'sent', 'sending'},
        'expect': {'expects', 'expected', 'expecting'},
        'build': {'builds', 'built', 'building'},
        'stay': {'stays', 'stayed', 'staying'},
        'fall': {'falls', 'fell', 'fallen', 'falling'},
        'cut': {'cuts', 'cutting'},
        'reach': {'reaches', 'reached', 'reaching'},
        'kill': {'kills', 'killed', 'killing'},
        'remain': {'remains', 'remained', 'remaining'},
        'suggest': {'suggests', 'suggested', 'suggesting'},
        'raise': {'raises', 'raised', 'raising'},
        'pass': {'passes', 'passed', 'passing'},
        'sell': {'sells', 'sold', 'selling'},
        'require': {'requires', 'required', 'requiring'},
        'report': {'reports', 'reported', 'reporting'},
        'decide': {'decides', 'decided', 'deciding'},
        'pull': {'pulls', 'pulled', 'pulling'},
    }
    lower = word.lower()
    return {word.lower() for word in INFLECTIONS.get(lower, [lower] + [lower + s for s in ['', 's', 'ed', 'ing', 'er', 'est', 'ly']])}


def _highlight_word(text, word, color_rgb):
    """为 text 中的 word 实例添加颜色标签（HTML span）。
    日语和英语均采用锚点匹配策略：取目标词前缀作为锚点，
    在句子中找到锚点后检查完整词长片段是否属于目标词的活用/屈折形式集合。
    中文输入使用简单子串替换（无活用形概念）。"""
    import re
    lower_text = text.lower()
    lower_word = word.lower()
    span_open = f"<span style='color: rgb{color_rgb};'>"
    span_close = "</span>"

    # 检测是否为日语单词（包含平假名或片假名）
    is_japanese = bool(re.search(r'[\u3040-\u309f\u30a0-\u30ff]', word))
    # 检测是否为英文单词（包含拉丁字母）
    is_english = bool(re.search(r'[a-zA-Z]', word)) and not is_japanese

    if not is_japanese and not is_english:
        # 中文或其他脚本：直接子串替换，无活用形问题
        start = 0
        while True:
            idx = lower_text.find(lower_word, start)
            if idx == -1:
                break
            text = text[:idx] + span_open + text[idx:idx + len(word)] + span_close + text[idx + len(word):]
            start = idx + len(span_open) + len(word) + len(span_close)
            lower_text = text.lower()
        return text

    # 日语或英文：使用锚点 + 活用形匹配
    inflections = _get_inflections(word)

    # 锚点长度：取目标词前缀（留 1 个字符作为活用部分）
    anchor_len = max(2, len(word) - 1)
    anchor_lower = lower_word[:anchor_len]

    search_start = 0
    while True:
        idx = lower_text.find(anchor_lower, search_start)
        if idx == -1:
            break

        # 尝试从锚点位置取出「完整词长」的片段（活用形可能与原形等长）
        for trial_len in {len(word), len(word) + 1}:
            if idx + trial_len > len(text):
                continue
            candidate = text[idx:idx + trial_len]
            if candidate.lower() in inflections:
                text = text[:idx] + span_open + candidate + span_close + text[idx + trial_len:]
                lower_text = text.lower()
                search_start = idx + len(span_open) + trial_len + len(span_close)
                break
        else:
            search_start = idx + 1

    return text


def _highlight_fragments(text, fragments, color_rgb):
    """用 AI 报告的 highlighted_fragments 直接在句子中匹配并上色（最准确，活用形无忧）。"""
    if not text or not fragments:
        return text
    span_open = f"<span style='color: rgb{color_rgb};'>"
    span_close = "</span>"
    matches = []
    for frag in fragments:
        if not frag:
            continue
        pos = 0
        while True:
            idx = text.lower().find(frag.lower(), pos)
            if idx == -1:
                break
            matches.append((idx, frag))
            pos = idx + 1
    if not matches:
        return html.escape(text)
    result = ''
    cur = 0
    for idx, frag in matches:
        result += html.escape(text[cur:idx])
        result += span_open + html.escape(frag) + span_close
        cur = idx + len(frag)
    result += html.escape(text[cur:])
    return result


def _full_text_color(text, color_rgb):
    """把整段 text 包进一个颜色 span（用于整段内容统一上色）。"""
    return f"<span style='color: rgb{color_rgb};'>{text}</span>"




def _highlight_vocab_word(vocab, target_word):
    """对 vocab 全文中所有目标单词的出现处添加橙色高亮（与原句一致）。
    大小写不敏感匹配，保留原始大小写。"""
    return _highlight_word(vocab, target_word, (255, 170, 0))


def _extract_target_meaning_from_vocab(vocab, target_word):
    """从词汇解析文本中提取目标词的中文含义，作为翻译高亮的最终兜底。
    vocab 格式：<span>target</span>（kekax）：词性，含义。/n...
    优先取目标词的词条；若 vocab 为空或目标词未出现，返回空字符串。"""
    import re
    if not vocab or not target_word:
        return ''

    def _extract_one(text):
        # 优先匹配高亮格式，再匹配普通格式
        for pattern in [
            rf'<span[^>]*>[^<]*?{re.escape(target_word)}[^<]*?</span>（[^）]+）：([^，,。]+)',
            rf'{re.escape(target_word)}（[^）]+）：([^，,。]+)',
        ]:
            m = re.search(pattern, text)
            if m:
                meaning = m.group(1).strip()
                if meaning:
                    return meaning
        return None

    return _extract_one(vocab) or ''


def _apply_translation_highlight(text, target_word=None):
    """将直译/意译中的【H】...【/H】标记替换为橙色 span。"""
    import re

    def replacer(m):
        inner = m.group(1).strip()
        return f"<span style='color: rgb(255, 170, 0);'>{inner}</span>"

    return re.sub(r'【H】(.*?)【/H】', replacer, text)


def _validate_and_fix_translation_highlights(result, target_word, sentence):
    """
    检查直译和意译中的【H】标记，若漏标则尝试从 translation_highlights 补充。

    仅依赖 AI 报告的 translation_highlights，不使用任何硬编码。
    若 AI 仍未提供有效片段，本次不做注入（交由 AI 改进 prompt 根治问题）。
    """
    import re

    literal = result.get('literal_translation', '')
    free    = result.get('free_translation',    '')
    th      = result.get('translation_highlights') or {}

    lit_phrases  = th.get('literal', [])
    free_phrases = th.get('free',    [])

    def _first_valid(phrases):
        for p in phrases:
            if p and p.strip():
                return p.strip()
        return None

    lit_word = _first_valid(lit_phrases)
    free_word = _first_valid(free_phrases) or lit_word

    fixed = False

    # 直译漏标 → 注入
    if not re.search(r'【H】', literal) and lit_word:
        literal = literal.rstrip()
        literal = (literal[:-1] + f'【H】{lit_word}【/H】。') if literal.endswith('。') \
                  else (literal + f'【H】{lit_word}【/H】')
        result['literal_translation'] = literal
        fixed = True

    # 意译漏标 → 注入
    if not re.search(r'【H】', free) and free_word:
        free = free.rstrip()
        free = (free[:-1] + f'【H】{free_word}【/H】。') if free.endswith('。') \
               else (free + f'【H】{free_word}【/H】')
        result['free_translation'] = free
        fixed = True

    if fixed:
        print(f'[高亮修复] 翻译漏标，已从 translation_highlights 注入')
        result['translation_highlights'] = {
            'literal': [lit_word] if lit_word else [],
            'free':    [free_word] if free_word else [],
        }
    else:
        lit_missing = not re.search(r'【H】', literal)
        free_missing = not re.search(r'【H】', free)
        if lit_missing or free_missing:
            print(f'[高亮警告] 直译漏标: {lit_missing}，意译漏标: {free_missing}，'
                  f'translation_highlights 也无有效片段，不注入')

    return result


def _escape_ts_dq(text):
    """TSV 字段内双引号转义为 ""，同时把 tab 替换为空格、换行替换为 <br>"""
    return text.replace('"', '""').replace('\t', '    ').replace('\n', '<br>')


def format_anki_card(sentence, analysis, target_word=None, synonyms_info=None):
    """格式化为Anki卡片格式"""
    import re
    print(f'[DEBUG format_anki_card] target_word={target_word!r}, fragments={analysis.get("highlighted_fragments")!r}')

    def clean(text, strip_prefix=None):
        """去掉 AI 可能添加的前缀，并将内容双引号替换为单引号（防止破坏 TSV 解析）"""
        text = text.strip()
        if strip_prefix and text.startswith(strip_prefix):
            text = text[len(strip_prefix):].lstrip('：: ')
        # 兜底清理：去掉所有 {{...}} 模板标记（修复 prompt 泄漏问题）
        text = re.sub(r'\{\{[^}]*\}\}', '', text)
        # 过滤掉占位符（AI 偷懒/上下文不足时用 ... 代替真实内容）
        if text in ('...', '..', '.', '*', '—'):
            text = ''
        # 过滤掉纯空白或只有极少字符的垃圾内容
        stripped = re.sub(r'[\s,，.。;；、:：]+', '', text)
        if len(stripped) < 3:
            text = ''
        return text.replace('"', "'")

    vocab    = clean(analysis["vocabulary_analysis"])
    grammar  = clean(analysis["grammar_analysis"])
    literal  = clean(analysis["literal_translation"], strip_prefix="直译")
    free     = clean(analysis["free_translation"],    strip_prefix="意译")
    synonyms_common = clean(analysis["synonyms_common"]) if analysis.get("synonyms_common") else None
    word_choice = clean(analysis["word_choice_reason"]) if analysis.get("word_choice_reason") else None
    mnemonic = clean(analysis["synonyms_mnemonic"]) if analysis.get("synonyms_mnemonic") else None

    # 造句功能：目标单词加橙色标签
    # highlighted_fragments 高亮原句中的日文片段
    # translation_highlights 高亮直译/意译中的中文含义
    fragments = analysis.get('highlighted_fragments', [])
    trans_hl = analysis.get('translation_highlights', {})  # {"literal": [...], "free": [...]}
    effective_target_word = target_word
    if not effective_target_word and fragments:
        effective_target_word = fragments[0]
    if effective_target_word and not fragments:
        fragments = [effective_target_word]
    if effective_target_word and fragments:
        sentence = _highlight_fragments(sentence, fragments, (255, 170, 0))
        vocab = _highlight_vocab_word(vocab, effective_target_word)
        # AI 在 translation_highlights 里报告直译/意译中目标词对应的中文词
        # 翻译文本中本身已有【H】标记（AI 写入），代码负责把【H】替换为实际 HTML 颜色
        trans_hl = analysis.get('translation_highlights', {}) or {}

        def _apply_th(text, key):
            # 清除残留【H】标记，将【H】中文【/H】替换为 HTML 颜色标签
            def replacer(m):
                word = m.group(1)
                return f"<span style='color: rgb(255, 170, 0);'>{word}</span>"
            text = re.sub(r'【H】([^【】]+)【/H】', replacer, text)
            # 同时用 translation_highlights 里报告的词在译文中再次高亮（以防 AI 漏标【H】）
            for phrase in (trans_hl.get(key) or []):
                if phrase and not phrase.startswith('{{') and not re.search(r'[\u3040-\u309f\u30a0-\u30ff]', phrase):
                    text = _highlight_word(text, phrase, (255, 170, 0))
            return text
        literal = _apply_th(literal, 'literal')
        free    = _apply_th(free,    'free')

    # 第二列：词汇和语法分析（含近义词辨析）
    synonyms_part = ''
    if synonyms_common:
        # 近义词共同辨析：蓝色
        synonyms_part = f'\n\n4. 近义词共同辨析：\n{_full_text_color(synonyms_common, (0, 112, 255))}'
        # 选词理由：紫色（两者可互换则跳过，且不计为独立章节）
        if word_choice and '【可互换】' not in word_choice:
            synonyms_part += f'\n\n5. 选词理由：\n{_full_text_color(word_choice, (147, 112, 219))}'
        if mnemonic:
            # 单词对照总结：红色
            synonyms_part += f'\n{_full_text_color(mnemonic, (255, 69, 58))}'
    column2 = f'2. 词汇解析：\n{vocab}\n\n3. 语法解析：\n{grammar}{synonyms_part}'

    # 第三列：翻译（根据实际显示的章节数决定编号）
    has_choice = word_choice and '【可互换】' not in word_choice
    trans_num = '6' if (synonyms_common and has_choice) else ('5' if synonyms_common else '4')
    column3 = f'{trans_num}. 句子的翻译：\n\n• 直译：{literal}\n\n• 意译：{free}'

    # 使用 tab 分隔，字段内双引号转义为 ""，tab 转为空格
    return f"{_escape_ts_dq(sentence)}\t{_escape_ts_dq(column2)}\t{_escape_ts_dq(column3)}"

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/start_session', methods=['POST'])
def start_session():
    """开始新会话"""
    session_id = datetime.now().strftime('%Y%m%d_%H%M%S')
    sessions[session_id] = {
        'sentences': [],
        'start_time': datetime.now().isoformat()
    }
    save_session_cache(session_id)
    return jsonify({'session_id': session_id})

@app.route('/complete_json', methods=['POST'])
def complete_json():
    """
    当主分析返回 JSON 不完整时，用此接口补全缺失字段。
    请求体: {"partial": "...", "missing_fields": ["vocabulary_analysis", ...], "model": "..."}
    """
    data = request.json
    partial = data.get('partial', '')
    missing_fields = data.get('missing_fields', [])
    model = data.get('model', 'gpt-5-mini')

    if not missing_fields:
        return jsonify({'error': '未指定缺失字段'}), 400

    # 找出已存在的完整字段值（用于上下文）
    existing = {}
    for field in ('has_errors', 'errors', 'error_reasons', 'suggested_sentence',
                  'synonyms_common', 'word_choice_reason', 'synonyms_mnemonic',
                  'highlighted_fragments', 'vocabulary_analysis',
                  'grammar_analysis', 'literal_translation', 'free_translation'):
        m = re.search(rf'"{field}"\s*:\s*(\[|\{{)', partial)
        if m:
            existing[field] = f'"{field}": <已提供>'

    prompt = f"""以下是 AI 生成的 JSON 响应，但被截断了。

已完整提供的字段：
{json.dumps(existing, ensure_ascii=False, indent=2)}

缺失/不完整的字段：{missing_fields}

请补全以下字段的值。只输出 JSON，不要有任何解释或其他内容。
JSON格式：
{{
  "has_errors": false,
  "vocabulary_analysis": "...",
  "grammar_analysis": "...",
  "literal_translation": "...",
  "free_translation": "...",
  "error_reasons": "...",
  "suggested_sentence": "...",
  "synonyms_common": "...",
  "word_choice_reason": "...",
  "synonyms_mnemonic": "...",
  "highlighted_fragments": [...]
}}"""

    try:
        resp = call_api_with_fallback(
            messages=[
                {"role": "system", "content": "你是 AI 语言学习助手。你的任务是根据已提供的 JSON 字段，补全缺失/不完整的字段。只输出纯 JSON，不输出任何解释。"},
                {"role": "user", "content": prompt}
            ],
            model=model,
            temperature=0.3,
            stream=False,
            timeout=60,
            max_tokens=DEFAULT_MAX_TOKENS
        )
        content = resp.choices[0].message.content.strip()
        result = json.loads(content)
        # 只保留缺失字段
        filtered = {k: v for k, v in result.items() if k in missing_fields}
        return jsonify(filtered)
    except Exception as e:
        print(f'[补全] 失败: {e}')
        return jsonify({'error': str(e)}), 500


@app.route('/analyze', methods=['POST'])
def analyze():
    """分析句子"""
    data = request.json
    session_id = data.get('session_id')
    sentence = data.get('sentence')
    language = data.get('language', 'ja')
    model = data.get('model', 'gpt-5-mini')

    if not session_id or session_id not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    if not sentence:
        return jsonify({'error': '句子不能为空'}), 400

    # 分析句子
    analysis = analyze_sentence(sentence, language, model)

    # 如果有错误，返回错误信息
    if analysis['has_errors']:
        return jsonify({
            'has_errors': True,
            'errors': analysis['errors']
        })

    # 保存到会话
    sessions[session_id]['sentences'].append({
        'sentence': sentence,
        'analysis': analysis
    })

    return jsonify({
        'has_errors': False,
        'analysis': analysis
    })

@app.route('/save_sentence', methods=['POST'])
def save_sentence():
    """强制保存句子到会话（有错误时用户选择继续原句时使用）"""
    data = request.json
    session_id = data.get('session_id')
    sentence = data.get('sentence')
    analysis = data.get('analysis')

    if not session_id or session_id not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    target_word = data.get('target_word')
    synonyms_info = data.get('synonyms_info')

    sessions[session_id]['sentences'].append({
        'sentence': sentence,
        'analysis': analysis,
        'target_word': target_word,
        'synonyms_info': synonyms_info,
    })
    save_session_cache(session_id)
    return jsonify({'success': True, 'count': len(sessions[session_id]['sentences'])})

def _parse_word_entry(raw):
    """解析单词条目，分离单词/短语和可选的中文意思提示。
    支持格式：'pacific 和平的' 或 'pacific'
    返回 {'word': str, 'chinese': str|None}
    """
    raw = raw.strip()
    m = re.match(r'^([^\u4e00-\u9fff\uff00-\uffef]+?)\s+([\u4e00-\u9fff\uff00-\uffef].*)$', raw)
    if m:
        return {'word': m.group(1).strip(), 'chinese': m.group(2).strip()}
    return {'word': raw, 'chinese': None}


@app.route('/generate_sentences', methods=['POST'])
def generate_sentences():
    """根据单词/短语列表造句，支持指定中文意思"""
    data = request.json
    words = data.get('words', [])
    language = data.get('language', 'ja')
    model = data.get('model', 'gpt-5-mini')
    synonyms_mode = data.get('synonyms_mode', 'auto')  # 'auto' | 'force' | 'none' | 'separate'
    # separate 模式：完全跳过近义词逻辑，每个词独立处理
    _original_synonyms_mode = synonyms_mode
    if synonyms_mode == 'separate':
        synonyms_mode = 'none'

    if not words:
        return jsonify({'error': '单词列表不能为空'}), 400
    if not client:
        return jsonify({'error': '未配置API密钥'}), 400

    lang_name = '日语' if language == 'ja' else '英语'
    parsed = [_parse_word_entry(w) for w in words]

    if language == 'ja':
        quality_req = "语法正确（助词、活用形准确），地道自然（JLPT N3～N2），避免直译中文思维"
    else:
        quality_req = "语法正确（时态、冠词、介词准确），地道自然（B1～B2），避免中式英语"

    entries = []
    has_chinese = False
    all_chinese = True
    for i, p in enumerate(parsed):
        if p['chinese']:
            has_chinese = True
            entries.append(f'{i+1}. {p["word"]}（指定意思：{p["chinese"]}）')
        else:
            all_chinese = False
            entries.append(f'{i+1}. {p["word"]}')
    word_list = '\n'.join(entries)

    # 动态裁剪规则
    if not has_chinese:
        meaning_rule = "用每个词最常用的意思造一个地道例句"
    elif all_chinese:
        meaning_rule = ("对每个词判断是否有指定的中文意思：\n"
                        "- 有：用该意思造句\n"
                        "- 没有：不造句，给出2～3个推荐中文意思")
    else:
        meaning_rule = ("- 未指定中文意思的：用最常用意思造句\n"
                        "- 指定了中文意思的：判断是否有该意思，有则用该意思造句，无则给出2～3个推荐意思")

    # ---- 预检测：force 模式下先判断词间关系 ----
    force_synonyms_info = None
    if synonyms_mode == 'force' and len(parsed) >= 2:
        relation_prompt = _prompt_manager.get(language, 'WORD_RELATION',
                                              WORD_LIST=word_list,
                                              QUALITY=quality_req)
        relation_system = _prompt_manager.get(language, 'COMPOSE_SYSTEM_MSG')
        rel_resp = call_api_with_fallback(
            messages=[
                {"role": "system", "content": relation_system},
                {"role": "user", "content": relation_prompt}
            ],
            model=model,
            temperature=0.3,
            timeout=60,
            max_tokens=DEFAULT_MAX_TOKENS
        )
        rel_content = rel_resp.choices[0].message.content.strip()
        rel_result = extract_json(rel_content)
        relation_type = rel_result.get('relation_type', 'no_relation')
        relation_note = rel_result.get('relation_note', '')
        print(f"[造句] 词间关系检测：type={relation_type}, note={relation_note[:60] if relation_note else ''}")

        # force 模式下无论如何都继续造句（前端会显示关系提示）
        force_synonyms_info = {
            'words': [p['word'] for p in parsed],
            'relation_type': relation_type,
            'relation_note': relation_note,
        }
        if relation_type == 'no_relation':
            print(f"[造句] force 模式：AI 判断为无关，但用户仍要求强行造句分析")

    # ---- 构造造句 prompt ----
    # separate 模式也需要和 auto/force 保持相同的约束，防止 AI 输出过长导致截断
    synonyms_detect_instruction = ''
    if _original_synonyms_mode in ('auto', 'separate') and len(parsed) >= 2:
        synonyms_detect_instruction = ('\n另外判断这些词之间是否存在近义词或有重叠的使用场景，'
                                       '若存在，在返回JSON中增加 "synonyms_detected": ["词1", "词2", ...]')

    prompt = _prompt_manager.get(language, 'COMPOSE_USER',
                                  WORD_LIST=word_list,
                                  MEANING_RULE=meaning_rule,
                                  QUALITY=quality_req,
                                  SYNONYMS_DETECT=synonyms_detect_instruction,
                                  JSON_OUT_SENTENCES=f"""JSON格式：
{{"results": [
  {{"word": "词", "chinese": "中文意思", "valid": true, "sentence": "例句", "meanings": null}},
  {{"word": "词", "chinese": "指定意思", "valid": false, "sentence": null, "meanings": ["意思1", "意思2"]}}
]}}""")

    try:
        print(f"[造句] 开始请求，model={model}, words={words}")
        compose_system_msg = _prompt_manager.get(language, 'COMPOSE_SYSTEM_MSG')
        response = call_api_with_fallback(
            messages=[
                {"role": "system", "content": compose_system_msg},
                {"role": "user", "content": prompt}
            ],
            model=model,
            temperature=0.5,
            timeout=90,
            max_tokens=DEFAULT_MAX_TOKENS
        )
        print(f"[造句] API 响应成功")
        content = response.choices[0].message.content.strip()
        result = extract_json(content)

        # 确定 synonyms_info
        if force_synonyms_info:
            synonyms_info = force_synonyms_info
        elif _original_synonyms_mode == 'auto' and result.get('synonyms_detected'):
            synonyms_info = {'words': result.pop('synonyms_detected')}
        else:
            synonyms_info = None  # none / separate 模式

        if synonyms_info:
            result['synonyms_info'] = synonyms_info

        return jsonify(result)
    except IncompleteJSONError as e:
        print(f"[造句] JSON 不完整/截断，尝试逐条提取: {e}")
        # 尝试从原始内容中逐条提取
        fallback = _extract_sentence_results_from_raw(e.partial_content)
        if fallback:
            print(f"[造句] 逐条提取成功: {len(fallback)} 条")
            return jsonify({'results': fallback})
        return jsonify({'error': f'AI 返回格式不完整，请重试：{e}'}), 500
    except json.JSONDecodeError as e:
        print(f"[造句] JSON解析失败: {e}")
        # 尝试从原始内容中逐条提取
        fallback = _extract_sentence_results_from_raw(response.choices[0].message.content.strip() if 'response' in dir() else str(e))
        if fallback:
            print(f"[造句] 逐条提取成功: {len(fallback)} 条")
            return jsonify({'results': fallback})
        return jsonify({'error': f'JSON解析失败：{e}'}), 500
    except Exception as e:
        print(f"[造句] 异常: {type(e).__name__}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/undo', methods=['POST'])
def undo():
    """撤销最后一条句子"""
    data = request.json
    session_id = data.get('session_id')

    if not session_id or session_id not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    sentences = sessions[session_id]['sentences']
    if not sentences:
        return jsonify({'error': '没有可撤销的记录'}), 400

    sentences.pop()
    save_session_cache(session_id)
    return jsonify({'success': True, 'remaining': len(sentences)})

@app.route('/end_session', methods=['POST'])
def end_session():
    """结束会话并导出文件"""
    data = request.json
    session_id = data.get('session_id')

    if not session_id or session_id not in sessions:
        return jsonify({'error': '无效的会话ID'}), 400

    session = sessions[session_id]

    # 生成Anki格式文件
    output_lines = [
        '#separator:tab',
        '#html:true'
    ]

    for item in session['sentences']:
        print(f'[导出 DEBUG] sentence={item["sentence"]!r}  target_word={item.get("target_word")!r}  fragments={item["analysis"].get("highlighted_fragments")!r}', flush=True)
        try:
            card = format_anki_card(
                item['sentence'],
                item['analysis'],
                target_word=item.get('target_word'),
                synonyms_info=item.get('synonyms_info'),
            )
        except Exception as e:
            print(f'[导出错误] 句子: {item["sentence"]!r}  错误: {e}')
            import traceback; traceback.print_exc()
            import html as _html_mod
            card = f"{_html_mod.escape(item['sentence'])}\t{_html_mod.escape(str(e))}\t导出失败"
        output_lines.append(card)

    # 保存文件
    filename = f'anki_export_{session_id}.txt'
    filepath = os.path.join('exports', filename)

    # 确保exports目录存在
    os.makedirs('exports', exist_ok=True)

    with open(filepath, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines))

    # 清理会话
    del sessions[session_id]
    delete_session_cache(session_id)

    return jsonify({
        'filename': filename,
        'filepath': filepath
    })

@app.route('/download/<filename>')
def download(filename):
    """下载导出的文件"""
    filepath = os.path.join('exports', filename)
    return send_file(filepath, as_attachment=True, download_name=filename)


@app.route('/recover_sessions', methods=['GET'])
def recover_sessions():
    """返回可恢复的会话列表"""
    result = []
    for session_id, data in sessions.items():
        result.append({
            'session_id': session_id,
            'start_time': data['start_time'],
            'sentence_count': len(data['sentences'])
        })
    return jsonify(result)


@app.route('/dismiss_session', methods=['POST'])
def dismiss_session():
    """丢弃指定会话（忽略恢复）"""
    data = request.get_json()
    session_id = data.get('session_id')
    if session_id and session_id in sessions:
        del sessions[session_id]
    return jsonify({"ok": True})


if __name__ == '__main__':
    # 开发模式直接用 Flask 内置服务器（线程池有限，仅供调试）
    # 生产环境请用 gunicorn 启动：
    #   cd /Users/jing/Project/Anki && /Users/jing/Project/Anki/venv/bin/gunicorn \
    #     --bind 127.0.0.1:5001 --workers 2 --threads 4 \
    #     --timeout 300 --keep-alive 60 --chdir /Users/jing/Project/Anki \
    #     --access-logfile server.log --error-logfile server.log \
    #     app:app
    app.run(port=5003, threaded=True)
