# ccpick

[English](README.md) · [版本下载](https://github.com/tykisgod/ccpick/releases)

给持有多个本人账号的 Claude Code 用户用的工具：登录时选择 Chrome 配置文件、查看额度与恢复时间、按消耗速度预测并切换账号，配有 Windows 托盘和 macOS 菜单栏。

底层凭据管理和切换由 [claude-swap](https://github.com/realiti4/claude-swap) 完成。安装 ccpick 时自动装好锁定版本，日常操作都通过 `ccpick` 命令完成。

这是首个 alpha 版本。Windows、macOS、Linux 在 CI 中验证包安装和决策逻辑；实际浏览器登录、自启和系统权限仍需在使用者机器上验证。Linux 第一版只提供命令行。

## 安装

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后执行：

```sh
uv tool install --python 3.12 git+https://github.com/tykisgod/ccpick.git@v0.1.0
ccpick --help
ccpick doctor
```

需要另外安装 Claude Code 和 Chrome。弹窗选号需要 Python 的 Tk 支持；如果当前解释器没有 Tk，可以通过 `uv tool install --python /path/to/python ...` 指定带 Tk 的解释器，或设置 `CCPICK_PROFILE` 直接指定配置文件。

当前通过 GitHub 分发，尚未发布到 PyPI。

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
ccpick disable 2              # 停用该账号的自动选择
ccpick enable 2
```

添加下一个账号：在 Claude Code 执行 `/login`，亲自完成官方页面上的授权，再执行 `ccpick add`。装好浏览器钩子后，`/login` 会弹出 Chrome 配置文件选择器。

公开版不包含自动点击授权、无头登录或修改 User-Agent 的功能。

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

默认只用账号级 5 小时、7 天窗口决定切换。每模型额度照样展示，需要时用 `ccpick auto --model Fable` 纳入判据；后台服务使用用户环境变量 `CCSWITCH_MODELS=Fable` 或 `all`，设置后重新运行 setup。

ccpick 状态目录：Windows 为 `%LOCALAPPDATA%/ccpick/`，macOS 为 `~/Library/Application Support/ccpick/`，Linux 为 `${XDG_DATA_HOME:-~/.local/share}/ccpick/`。可用 `CCPICK_DATA_DIR` 覆盖。

claude-swap 的账号库仍用它原来的路径，与已有 claude-swap 共用；依赖版本在独立环境里，账号数据不是另建一份。状态与日志可能包含邮箱，发 issue 前请脱敏，不要上传凭据或账号导出文件。程序没有遥测或托管服务。

本项目是独立的非官方工具，与 Anthropic 无关联。请只管理本人有权访问的账号，并遵守所用服务的条款。工具不会改变订阅额度。

采用 MIT 许可证；依赖声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
