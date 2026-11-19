# Google Antigravity

通过本机官方 `agy` 客户端的 ACP（Agent Client Protocol）stdio 协议，把 Antigravity 模型接入 Sakura，用于对话、多模态识图和工具调用。

## 前置准备

1. 在运行 Sakura 的同一系统用户下安装官方 `agy` 客户端，并确保终端可执行：
   ```bash
   agy --version
   agy acp --help
   ```
   Windows 需使用原生 `agy.exe`（不支持 `.cmd` / `.bat` 包装脚本）。
2. 保持“OpenAI 兼容模型”基础插件启用，在“设置 → 插件”中安装并启用本插件。

## 添加与使用

1. 打开“设置 → 模型服务 → 添加模型服务”，选择 **Google Antigravity**。
2. 如 `agy` 不在默认 PATH 中，可在右侧“CLI 路径”填写原生可执行文件的绝对路径；按需设置思考档位与代理模式。
3. 若本机 `agy` 已登录，直接点击 **检测并同步模型**；否则点击 **登录 Antigravity**，在浏览器完成 Google 账号授权后复制单行授权码提交。
4. 到“设置 → 模型”为对话模型或视觉对话模型选择同步出的 Antigravity 模型即可使用。

## 权限与安全边界

- 所有模型请求仅通过独立隔离目录中的 `agy acp` stdio 进程执行，插件不提取、读取或保存 OAuth Token。
- `agy` 内置的本地文件、终端、网页搜索与浏览器工具权限一律拒绝；开启工具调用时，仅放行 Sakura 本轮显式暴露的本地 MCP 桥接工具（`mcp_sakura_*`）。
- 429 限流或账号额度不足会触发短期冷却守卫，避免重复请求消耗额度。
