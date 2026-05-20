#!/usr/bin/env python3
from openai import OpenAI

# 备用 API 配置
client = OpenAI(
    api_key="sk-DxPKlKpuoTPneVh11243pfrJT8FF679izb8XWGkhu2gSEomr",
    base_url="https://xiongapi.top/v1"
)

# 测试造句
prompt = """为以下英语单词造句：

1. showcase

规则：用最常用的意思造一个地道例句
质量：语法正确（时态、冠词、介词准确），地道自然（B1～B2），避免中式英语。句子10～20词，必须包含该词。

JSON格式：
{"results": [
  {"word": "词", "chinese": "中文意思", "valid": true, "sentence": "例句", "meanings": null}
]}"""

try:
    print("正在调用备用 API...")
    response = client.chat.completions.create(
        model="【N站】gpt-5.1",
        messages=[
            {"role": "system", "content": "你是精通英语的语言专家。直接输出纯JSON，不要markdown标记。"},
            {"role": "user", "content": prompt}
        ],
        temperature=0.5,
        timeout=60
    )
    print("\n成功！返回内容：")
    print(response.choices[0].message.content)
except Exception as e:
    print(f"\n失败：{e}")
