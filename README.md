# Code Analyzer Agent

> ⚠️ **重要提示：每次分析请求较耗时**
>
> 由于本系统分析逻辑需自动解析代码库、安装依赖、尝试启动服务，并驱动大语言模型逐步“阅读理解代码、生成测试用例并实际执行校验”，所以 _每次请求大约需要3~5分钟_（取决于代码规模及模型响应速度）。请耐心等待结果返回。

本项目是一套 AI 驱动的代码理解与自动化功能验证系统，支持自动分析代码库、功能分类、源码抽象、需求到实现的功能定位、自动根据代码推理端点并为主要功能生成和自修复可执行测试用例。

## 核心功能

### 1. 代码分析工作流（Feature Analysis Workflow）
- **需求解析与功能分类**：自动分解用户输入的 `problem_description`，LLM 提取多类别功能标签及语义关键字。
- **代码库索引**：全量遍历代码库，解析所有文件和函数，建立源码关键抽象。
- **函数摘要生成**：为每个函数自动生成语义关键字和功能摘要。
- **精确匹配功能与实现**：用多轮语义比对（字符串与大模型语义评估结合）完成“需求类别-实现函数”的准确映射。
- **输出结构化分析结果**：如
  ```json
  {
    "feature_analysis": [
      {
        "feature_description": "Create Channel",
        "implementation_location": [
          {"file": "src/modules/channel/channel.resolver.ts", "function": "createChannel", "lines": "13-13"},
          {"file": "src/modules/channel/channel.service.ts", "function": "create", "lines": "28-31"}
        ]
      }
      // ...其它功能
    ],
    "execution_plan_suggestion": "npm install; npm run start:dev; GraphQL API 通常在 http://localhost:3000/graphql"
  }
  ```

### 2. 自动化测试工作流（Functional Verification Workflow）
- **自动筛选可测功能**：跳过类型、配置、纯工具等非业务实现，仅对重要业务端点生成测试。
- **端点/接口自动推断**：通过解析实现源码、schema，生成端点 URL、HTTP 方法、参数与期望响应的完整规范。
- **智能测试用例生成**：LLM 生成轻量可运行的 Node.js 测试代码，并自动加入最佳实践（HTTP 请求、GraphQL、断言等）。
- **测试用例自修复**：如首次自动执行用例失败，智能修复常见代码、参数、上下文错误并重试，确保端到端测试脚本可用。
- **结构化测试输出**：每个功能用例包括代码、运行命令、执行结果，如“Test passed”。
- **支持多种 LLM（DashScope/Anthropic/Ollama）与工具对话能力。**

## 实现流程

如下为端到端处理流程：

1. 用户 POST `/analyze`，提交需求文本与 zip 代码包。
2. 服务端自动解压并索引整个代码库，生成全局语义索引和函数摘要。
3. LLM 对需求做自动分类，生成若干具体业务特性及关键字。
4. 用语义匹配方法将每一类业务特性映射到源码相关函数/文件（可多对多）。
5. 输出所有需求-实现-源码的结构化映射关系与推荐启动命令。
6. 进入自动化验证环节：
   - 分析哪些特性需要生成测试（自动跳过配置/类型/非业务代码）。
   - 推理每个特性的端点、参数及 GraphQL/REST 请求规范。
   - 利用 LLM 自动生成顺手可运行的 Node.js 测试代码。
   - 自动执行，每个用例失败时触发一轮自修复并重试。
   - 最终输出所有测试用例、运行命令及已执行日志。

## 快速使用

### 本地运行

#### 依赖环境
- Python 3.12，推荐 Linux 环境
- 推荐 GPU/LLM 推理服务及 dashscope/anhtropic/ollama 可用
- Node.js（测试用例自动执行用）

#### 安装步骤
1. 克隆本仓库并进入目录
2. 使用 docker 启动服务
   ```bash
   docker build -t code-analyzer-agent .
   docker run -p 8000:8000 code-analyzer-agent
   ```

#### 用法举例
```bash
# 向 localhost:8000/analyze 提交问题描述和代码压缩包，结果输出到 result.json
curl -X POST \
  -F "problem_description=Create a multi-channel forum api. Can use any stack, but must use typescript, be deployable, and of production quality. Try using graphql or grpc for fun, but REST is ok too. Try using docker containers for fun if you want. Show how you would like to write documentation and testing if possible.

Channel Model: { id, name }

Message Model: { id, title, content, channel, createdAt }

The API should have these features.
- create a channel
- write messages in a channel
- list messages in a channel and order by descending (pagination is a extra credit)

Show how a production level project would look. (documentation, testing, error handling, etc ...)

Send the repository link of the project by email when finished." \
  -F "code_zip=@nestjs-channel-messenger-demo-main.zip" \
  http://localhost:8000/analyze > result.json
```
返回结果中：
- `feature_analysis`: 给出需求分解→实现定位
- `execution_plan_suggestion`: 推荐的项目启动方法
- `functional_verification`: 每项业务自动生成的 Node.js 测试代码、run 命令和实际执行结果



## 目录结构说明

- `run.py`      服务启动脚本，默认监听于 8000 端口
- `app/api/`    FastAPI 接口定义，POST `/analyze` 核心入口
- `app/agents/` 智能分析（graph_definition）、测试（verification_workflow）及 LLM 管理（model_driver）逻辑
- `app/utils/`  代码解析、日志等工具
- `requirements.txt`  依赖列表
- `Dockerfile`  部署镜像示例



