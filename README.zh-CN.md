<p align="center">
  <img src="assets/ccpick-icon.png" alt="ccpick：账号卡片切换图标" width="128" height="128">
</p>

# ccpick

> [!IMPORTANT]
> **请让 coding agent 帮你安装和配置，不要自己照抄命令设置。** 把下面这段话交给能操作终端的 agent，让它检查环境、完成配置并验证结果。需要登录或系统权限时，你再配合操作即可。
>
> **[给 agent 的安装配置指南 →](AGENT_SETUP.md)**

直接复制给你的 coding agent：

```text
请阅读 https://raw.githubusercontent.com/tykisgod/ccpick/main/AGENT_SETUP.md ，在这台电脑上安装并配置 ccpick。Windows/macOS 请配置浏览器钩子和带自动切号功能的托盘/菜单栏，Linux 请配置命令行。优先使用我的现有账号。请实际完成设置和验证，不要只给我一串命令。只有缺少账号选择、需要我登录或授予系统权限，或要替换我尚未同意替换的旧安装时，才向我提问。
```

[English](README.md) · [版本下载](https://github.com/tykisgod/ccpick/releases)

给持有多个本人账号的 Claude Code 用户用的工具：登录时选择 Chrome 配置文件、查看额度与恢复时间、按消耗速度预测并切换账号，配有 Windows 托盘和 macOS 菜单栏。

原有模式使用 [claude-swap](https://github.com/realiti4/claude-swap)。新的可选本地运行层会分别保存账号认证和原生设备标识，让受控 Claude 对话在切号后继续使用原进程。日常操作仍通过 `ccpick` 完成。

这是 alpha 版本。Windows、macOS、Linux 在 CI 中验证包、决策和本地运行层的合成测试；实际登录、自启和系统权限仍需在使用者机器上验证。独立运行层和桌面服务支持 Windows/macOS，Linux 保留原有命令行功能。

## 安装参考（供 agent 使用）

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后执行：

```sh
uv tool install --python 3.12 git+https://github.com/tykisgod/ccpick.git@v0.2.0
ccpick --help
ccpick doctor
```

需要另外安装 Claude Code 和 Chrome。弹窗选号需要 Python 的 Tk 支持；如果当前解释器没有 Tk，可以通过 `uv tool install --python /path/to/python ...` 指定带 Tk 的解释器，或设置 `CCPICK_PROFILE` 直接指定配置文件。

当前通过 GitHub 分发，尚未发布到 PyPI。

## 可选：独立账号运行层

需要独立账号标识，并希望长任务在切号后继续时，可以显式启用。依赖 Node.js 22+、原生 Claude Code、OpenSSL（Git for Windows 自带），以及自行配置的本地 HTTP CONNECT 代理。

```sh
ccpick runtime setup --upstream-proxy http://127.0.0.1:8080 --dry-run
ccpick runtime setup --upstream-proxy http://127.0.0.1:8080
ccpick runtime add account@example.com
ccpick run
ccpick run -- --resume
ccpick runtime status
ccpick runtime doctor
```

示例代理地址要换成自己的。安装不会覆盖全局 Claude 启动器或旧账号库。使用 `ccpick run` 打开受控对话；普通 `claude` 进程仍沿用原来的配置。启用后，账号选择、额度和自动决策接入新运行层。`disable` 会显示紫色“仅手动”标记，仍可手动切过去。

每个受支持的请求使用同一账号的认证和设备标识。已经发出的请求按原账号完成，后续请求使用新选择；对话保留原进程、工具结果和流式响应，不重启或重复任务。正文与输入历史共享，Resume 和 ↑ 历史可以继续使用。受控对话里的原生 `/login` 通过浏览器配置文件选择器授权，可识别已有或新账号。

API、OAuth、刷新认证和额度请求都走配置的代理，失败时不会回退直连。项目不自带代理、VPN、家宽出口或浏览器网络配置；Chrome 登录页面仍按 Chrome 自身设置联网，需要相同出口时要另外配置并核验。生成的本地证书仅供受控进程信任，不修改系统或浏览器证书库。

详见 [运行层说明](RUNTIME.md)。托管 MCP、语音、云端会话、远程控制不在新运行层支持范围内。CI 使用合成上游，真实授权与原生切号还需要在使用者机器上验证。

## 日常使用

```sh
ccpick add                    # 保存 Claude Code 当前登录的账号
ccpick accounts               # 查看已保存账号
ccpick list                   # 查看 Chrome 配置文件
ccpick usage                  # 查看缓存额度及恢复时间
ccpick usage --json
ccpick auto --dry-run          # 看建议，不切换
ccpick auto                   # 选择账号并核对切换结果
ccpick switch 2               # 指定账号
ccpick disable 2              # 仅手动，仍可指定切换
ccpick enable 2
```

添加下一个账号：在 Claude Code 执行 `/login`，亲自完成官方页面上的授权，再执行 `ccpick add`。装好浏览器钩子后，`/login` 会弹出 Chrome 配置文件选择器。

公开版包含自动点击授权、批量入库、无头登录和自定义 User-Agent，通过下面的命令显式启用：

```sh
ccpick auto-enroll --profile "Profile 1" --email account@example.com
ccpick auto-enroll-all --dry-run
ccpick auto-enroll-all --profiles "Profile 1" "Profile 2"
ccpick auto-enroll --profile "Profile 1" --email account@example.com --headless
ccpick auto-enroll --profile "Profile 1" --email account@example.com --headless --user-agent "YOUR_USER_AGENT"
```

这些命令会操作官方登录/授权页面，并可能更换 Claude 当前账号；请选择本人控制的 Chrome 配置文件，批量操作前先看 `--dry-run`。普通命令不会自动开启无头模式或修改 User-Agent。

**浏览器自动化属于实验功能。** 原 CDP 实现需要先完全退出 Chrome；Chrome 136+ 禁止默认用户数据目录的远程调试。使用 CDP 时，把 `CCPICK_CHROME_USER_DATA_DIR` 指向自己创建并登录过的专用非默认 Chrome 数据目录，具体原因见 [Chrome 官方说明](https://developer.chrome.com/blog/remote-debugging-port)。无头 OAuth 和自定义 User-Agent 虽然可用，但尚未完成真实登录兼容性验证，也不保证登录提供方接受该流程。CI 不执行真实授权。

macOS 的 AppleScript 回退需要逐配置文件启用 Chrome 的 JavaScript 设置：`ccpick enable-js-gate --profiles "Profile 1"` 先预览，退出 Chrome 后加 `--apply` 才修改。开启后，已获 Chrome 自动化权限的应用能在该配置文件中运行 JavaScript；用 `--disable --apply` 可以关闭。Windows 保留 UI Automation 回退。自动化不适用时，可继续用上面的手动 `/login`。

## 托盘、菜单栏与浏览器钩子

```sh
ccpick setup --autoswitch --dry-run  # 先查看将执行的操作
ccpick setup --autoswitch
ccpick autoswitch tick --dry-run
```

`setup` 设置浏览器钩子和 `/best-account`；加 `--autoswitch` 后，Windows 注册托盘及定时兜底任务，macOS 编译菜单栏并注册 launchd 任务。macOS 需要先安装 Xcode Command Line Tools：`xcode-select --install`。不加 `--autoswitch` 只安装手动操作的接入部分。装完后新开终端，让环境变量生效。

如果已有旧版 ccpick 在运行，安装器会阻止两套自动切换器并行。只有确实要替换时，才使用提示中的显式替换选项。仅安装 Python 包不会自动改浏览器设置或启动服务。

卸载：

```sh
ccpick uninstall --dry-run
ccpick uninstall
uv tool uninstall ccpick
```

卸载会移除本工具注册的常驻任务，并在浏览器钩子仍属于本工具时恢复原设置。账号凭据和历史数据保留。

## 判据和数据

决策同时考虑用量与最近的消耗速度；接近耗尽时缩短检查间隔，离开耗尽账号后等其恢复再重新考虑。取数失败不当成零用量，停用或已删除的账号不会成为自动切换目标。

默认只用账号级 5 小时、7 天窗口决定切换。每模型额度照样展示，需要时用 `ccpick auto --model MODEL_NAME` 纳入判据；后台服务使用 `CCSWITCH_MODELS` 指定窗口名或 `all`，设置后重新运行 setup。`CCSWITCH_WATCH_ONLY=1` 让后台只检查。新运行层会拒绝已过期的自动决策，避免覆盖后来的人为选择。

ccpick 状态目录：Windows 为 `%LOCALAPPDATA%/ccpick/`，macOS 为 `~/Library/Application Support/ccpick/`，Linux 为 `${XDG_DATA_HOME:-~/.local/share}/ccpick/`。可用 `CCPICK_DATA_DIR` 覆盖。

claude-swap 的账号库仍用它原来的路径，与已有 claude-swap 共用；依赖版本在独立环境里，账号数据不是另建一份。状态与日志可能包含邮箱，发 issue 前请脱敏，不要上传凭据或账号导出文件。程序没有遥测或托管服务。

启用可选运行层后，其账号库保存在 `<ccpick 状态目录>/profile-runtime/`，与旧账号库分开。安装不会导入旧认证；请在新运行层完成各账号的官方授权。

本项目是独立的非官方工具，与 Anthropic 无关联。请只管理本人有权访问的账号，并遵守所用服务的条款。工具不会改变订阅额度。

采用 MIT 许可证；依赖声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
