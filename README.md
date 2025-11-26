## Code Analyzer Agent

一个基于大模型的**代码库分析小工具**。  
你提供一段需求描述和一个代码仓库（zip），它会自动读代码、调用工具，最后给出「功能分类 + 关键实现位置（文件 / 函数 / 行号范围）」的结构化结果。

---

## 能做什么

- **理解代码库**：自动遍历目录、读取源文件、搜索关键符号，帮助你理解一个不熟悉的仓库在做什么。
- **对照需求做映射**：根据你给的自然语言需求，把相关的函数和文件找出来，并输出结构化 JSON。
- **辅助代码走查**：结果里会标出实现位置（文件路径 + 函数名 + 行号范围）。

---

## 怎么用

- **启动 HTTP 服务**

```bash
python main.py --mode api --host 0.0.0.0 --port 8001
```

然后通过 `POST /analyze` 上传：

- `description`: 文字需求描述
- `file`: 代码仓库 zip 文件

返回值是一个 JSON，其中包含：

- `categories`: 按功能/模块分的分类列表
- `feature_analysis`: 每个功能点对应的实现位置列表

- **本地调试模式（可选）**

`main.py` 里有一个示例的 `analyze_requirements_direct()`，用于在本机对某个 zip 做一次分析，  
你可以根据自己需求修改里面的 `problem_description`、`code_zip_path` 和输出路径。

---

## 依赖与环境

- Python 3.9+
- 安装依赖：

```bash
pip install -r requirements.txt
```

本项目依赖一个已部署好的、兼容 OpenAI 接口的本地或远程大模型（例如 vLLM 服务），  
相关地址和模型名在 `agent/analyzer.py` 中配置，你可以按自己的环境调整。


