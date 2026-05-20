# Anki句子分析工具

这是一个基于Flask和ChatGPT API的句子分析工具，可以分析英语和日语句子，并生成Anki导入格式的文本文件。

## 功能特点

- 🔍 自动检测句子中的词汇、语法和搭配错误
- 📝 详细的词汇分析和语法分析
- 🌐 提供直译和意译两种翻译
- 📚 自动生成Anki导入格式的文本文件
- 🎨 美观的Web界面
- 🇯🇵🇬🇧 支持日语和英语句子分析

## 安装步骤

1. 安装依赖：
```bash
pip install -r requirements.txt
```

2. 配置OpenAI API密钥：
   - 首次运行时会自动创建 `config.txt` 文件
   - 或者手动创建 `config.txt` 文件，内容如下：
   ```
   # 请在下面一行填入你的OpenAI API密钥
   # 格式：OPENAI_API_KEY=sk-your-api-key-here
   OPENAI_API_KEY=sk-your-actual-api-key-here
   ```
   - 将 `sk-your-actual-api-key-here` 替换为你的真实API密钥

## 使用方法

1. 启动应用：
```bash
python app.py
```

2. 在浏览器中打开：
```
http://localhost:5001
```

3. 使用流程：
   - 点击"开始"按钮开始新会话
   - 选择语言（日语或英语）
   - 在文本框中输入句子
   - 点击"分析句子"按钮
   - 如果有错误，系统会提示并要求重新输入
   - 如果没有错误，显示分析结果
   - 重复输入多个句子
   - 点击"结束并导出"按钮，自动下载Anki导入文件

## 导出格式

导出的文本文件格式符合Anki导入标准：
- 使用Tab作为分隔符
- 每行包含三个字段：
  1. 原句
  2. 词汇分析和语法分析
  3. 翻译（直译和意译）

## 文件结构

```
Anki/
├── app.py              # Flask后端应用
├── config.txt          # API密钥配置文件（需要手动创建）
├── templates/
│   └── index.html      # Web前端界面
├── sample/
│   └── n11.txt         # 参考样式文件
├── exports/            # 导出文件目录（自动创建）
├── requirements.txt    # Python依赖
└── README.md          # 说明文档
```

## 注意事项

- 需要有效的OpenAI API密钥，在 `config.txt` 文件中配置
- 使用GPT-4模型进行分析，确保API账户有足够的额度
- 导出的文件保存在`exports`目录中
- 每次会话结束后会自动清理会话数据
- `config.txt` 文件包含敏感信息，请勿提交到版本控制系统

## 技术栈

- 后端：Flask + OpenAI API
- 前端：HTML + CSS + JavaScript
- AI模型：GPT-4
