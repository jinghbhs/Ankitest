"""
API 截断测试脚本 - 使用 curl 后端
"""
import subprocess
import json
import time

def load_config():
    config = {}
    with open('config.txt', 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                config[k.strip()] = v.strip()
    return config

config = load_config()

def curl_post(url, api_key, model, content, max_tokens, timeout=30):
    prompt = f'回复JSON：{{"text":"{"测试内容。" * (max_tokens // 10)}"}}，只输出JSON不要其他'
    # 用精确控制的方式：让AI返回固定数量的词
    word_count = max_tokens // 3  # 粗估每词3token
    word_count = min(word_count, 3000)
    prompt = f'请输出恰好 {word_count} 个不同的随机中文词语，格式：[ "词1", "词2", ... ]，只输出JSON数组不要任何其他内容'

    cmd = [
        'curl', '-s', '--max-time', str(timeout),
        '-H', f'Authorization: Bearer {api_key}',
        '-H', 'Content-Type: application/json',
        '-d', json.dumps({
            'model': model,
            'messages': [{'role': 'user', 'content': prompt}],
            'max_tokens': max_tokens,
            'temperature': 0.5
        }),
        url
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
        raw = result.stdout.strip()
        # 解析 SSE 格式（可能有 data: 前缀）
        for line in raw.split('\n'):
            line = line.strip()
            if line.startswith('data: '):
                line = line[6:]
            if line == '[DONE]':
                continue
            if line:
                try:
                    return json.loads(line)
                except:
                    pass
        # 直接尝试解析原始输出
        try:
            return json.loads(raw)
        except:
            return {'error': raw[:200]}
    except subprocess.TimeoutExpired:
        return {'error': 'timeout'}
    except Exception as e:
        return {'error': str(e)}


def test_target(client_name, url, api_key, model, max_tokens):
    resp = curl_post(url, api_key, model, None, max_tokens)
    if 'error' in resp:
        print(f"  [{client_name}] max_tokens={max_tokens:6d} | 错误: {resp['error'][:100]}")
        return 0, "error", -1

    try:
        content = resp['choices'][0]['message']['content']
        finish = resp['choices'][0].get('finish_reason', 'unknown')
        char_count = len(content)

        # 解析词数
        try:
            arr = json.loads(content.strip())
            actual_words = len(arr) if isinstance(arr, list) else -1
        except:
            actual_words = -1

        trunc = "截断 ⚠️" if finish == "length" else "正常 ✅"
        print(f"  [{client_name}] max_tokens={max_tokens:6d} | {char_count:6d} 字符 | 约{char_count//2:5d}token | 词数={actual_words} | {finish} | {trunc}")
        return char_count, finish, actual_words
    except (KeyError, IndexError) as e:
        print(f"  [{client_name}] max_tokens={max_tokens:6d} | 解析错误: {resp}")
        return 0, "error", -1


TOKEN_STEPS = [200, 500, 1000, 2000, 4000, 8000, 16000]

print("=" * 72)
print("API 截断测试")
print("=" * 72)
print(f"主API:   {config['OPENAI_BASE_URL']}  | 模型: {config['OPENAI_MODEL']}")
print(f"备用API: {config['BACKUP_BASE_URL']} | 模型: {config['BACKUP_MODEL']}")
print()

targets = [
    ("主API",   config['OPENAI_BASE_URL'] + '/chat/completions', config['OPENAI_API_KEY'],   config['OPENAI_MODEL']),
    ("备用API", config['BACKUP_BASE_URL'] + '/chat/completions',  config['BACKUP_API_KEY'],  config['BACKUP_MODEL']),
]

for name, url, key, model in targets:
    print(f"\n>>> {name} (模型: {model})")
    prev_words = None
    for mt in TOKEN_STEPS:
        chars, finish, words = test_target(name, url, key, model, mt)
        if finish == "length" and prev_words is not None:
            print(f"       ★ 截断发生！从 {prev_words} 词 → {words} 词")
        prev_words = words
        time.sleep(1)

print("\n" + "=" * 72)
print("测试完成")
