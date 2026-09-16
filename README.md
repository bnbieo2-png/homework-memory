# Homework Memory / 家庭错题本

A privacy-first, self-hosted notebook for recording homework mistakes, reviewing weak points, and building printable practice sets.

这是一个默认只在本机保存数据的家庭错题本。它可以记录错题、整理薄弱知识点、安排复习、生成练习，并把选中的题目打印或保存为 PDF。

## 主要功能

- 记录错题、不会做的题和需要强化的知识点
- 按科目、单元和时间筛选
- 保存原题、答案、错因和学习笔记
- 安排复习并记录练习结果
- 生成题目版和答案版练习卷
- 导出 Markdown、CSV、JSON 和可打印页面
- 可选接入 OpenClaw，从飞书会话导入已确认记录
- 可选使用 AI 生成学习内容；默认关闭

## 隐私原则

- 默认只监听 `127.0.0.1`，其他设备无法直接访问。
- 数据写入本机 `data/`，该目录已被排除，不会进入公开项目。
- 邮件、飞书导入和 AI 生成功能都不是必需项。
- AI 生成功能默认关闭；开启后，题目文字和相关学习记录可能发送给你配置的 AI 服务。
- 项目不包含统计跟踪、广告或远程数据收集。

公开自己的修改前，请先阅读 [PRIVACY.md](PRIVACY.md)，再运行隐私检查。

## 快速开始

需要 Python 3.12 或更高版本。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-web.txt
python web_app.py
```

然后打开：

```text
http://127.0.0.1:18765
```

第一次体验可以导入不含真实个人信息的示例：

```bash
python scripts/seed_demo.py
```

## 开启登录保护

只在本机使用时可以不开启登录。如果要让同一局域网内的其他设备访问，必须设置至少 16 个字符的访问码和长会话密钥。程序会拒绝没有登录保护的局域网访问：

```bash
export HOMEWORK_NOTEBOOK_USERNAME=student
export HOMEWORK_NOTEBOOK_ACCESS_CODE='replace-with-a-long-random-value'
export HOMEWORK_NOTEBOOK_SESSION_SECRET='replace-with-another-long-random-value'
python web_app.py --host 0.0.0.0
```

首次登录后会要求绑定手机动态验证码。

## Docker

```bash
cp .env.example .env
docker compose up --build
```

请先修改 `.env` 中的访问码和会话密钥。真实 `.env` 不得上传。

## 可选 AI 功能

AI 功能需要本机已经安装并配置 OpenClaw，而且必须明确开启：

```bash
export HOMEWORK_NOTEBOOK_ENABLE_AI=1
export HOMEWORK_NOTEBOOK_OPENCLAW_AGENT=main
python learning_enrichment.py --pending --limit 1
```

开启前请确认你有权把题目内容交给所使用的 AI 服务，并了解该服务的数据政策。

## 可选飞书导入

飞书导入会读取本机 OpenClaw 会话，只处理带有“已确认”标记的错题记录：

```bash
python feishu_mistake_sync.py
```

持续同步默认不会调用 AI。只有显式加入 `--enrich` 并同时设置 `HOMEWORK_NOTEBOOK_ENABLE_AI=1` 才会生成学习内容。

## 公开前检查

```bash
python scripts/privacy_audit.py
python -m unittest -v test_homework_system.py test_public_release.py
```

两个命令都通过后，再检查准备上传的文件列表。不要上传 `data/`、`.env`、照片、数据库、日志、PDF 或邮件文件。

## 开源许可

MIT License，见 [LICENSE](LICENSE)。
