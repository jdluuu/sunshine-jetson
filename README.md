# Sunshine for Jetson Orin NX

为 **Orin NX + JetPack 5** 增加原生 H.264 硬件编码的 Sunshine 社区分支。
基于 [LizardByte/Sunshine v0.20.0](https://github.com/LizardByte/Sunshine/tree/v0.20.0)，
修改始于 2026-10-20。保留上游历史及许可证；这是社区维护版本。

编码路径：**X11 → BGRx → VIC / nvvidconv → NVMM NV12 → NVENC / nvv4l2h264enc → Moonlight**。
使用 JetPack 已提供的 GStreamer 插件，不依赖桌面显卡的 NVENC 初始化路径。

## 支持范围

| 项目 | 首版范围 |
| --- | --- |
| 实测设备 | Orin NX 16GB |
| 实测系统 | Ubuntu 20.04、L4T 35.6.0（JetPack 5 系列） |
| 安装脚本接受 | Orin NX、aarch64、L4T R35、Ubuntu 20.04；其他 R35 小版本仍需自行验证 |
| 图形会话 | 已登录的 Xorg 桌面，有活动显示器或显示器模拟器 |
| 编码 | H.264、8-bit SDR |
| 实测串流 | 1920×1080、30 fps、10 Mbps，客户端确认画面正常、接近 30 fps |
| 未验证 | 60 fps、其他分辨率、其他 JetPack / Jetson 型号 |

本版不提供 HEVC、HDR 或 Wayland 捕获。输入与串流画面需保持相同宽高比，
输出宽高需为偶数。**Orin Nano 没有本方案所需的 NVENC，不能使用此脚本。**
这是基于旧版 Sunshine 的专用适配，尚未合入后续上游版本的修复；请用于可信局域网。

## 一键安装

在 **NX 的 Xorg 桌面终端**，用当前桌面用户执行。不要用 root 或 `sudo bash`。
需要网络和 sudo 权限；安装系统依赖时可能要求输入本机密码。

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/jdluuu/sunshine-jetson/v0.20.0-jetson.1/scripts/jetson/bootstrap.sh -o /tmp/install-sunshine-jetson.sh && bash /tmp/install-sunshine-jetson.sh
```

脚本将：

1. 检查 Orin NX、L4T R35、NVENC 设备及 GStreamer 插件。
2. 通过 apt 安装构建依赖，在独立 Python 环境中准备 CMake 3.27.9；
   按锁文件安装网页依赖，跳过 npm 生命周期脚本。
3. 获取固定版本子模块，以 `nice -n 15`、**`-j16`** 编译。
4. 编码 90 帧测试画面，并检查第 0、30、60 帧的 IDR / SPS / PPS。
5. 安装完整程序和网页资源，配置当前用户对 `/dev/uinput` 的访问。
6. 从活动显示器中选择主屏，启动 Sunshine，并设置桌面登录后自动启动。

编译需要至少 8 GiB 可用磁盘，以及约 15 GiB 内存与交换空间总量。
开始编译前，可用内存与空闲交换空间合计需至少 12 GiB。
8GB 型号通常需要增加交换空间；脚本不会自动修改交换空间。
不会升级 JetPack 或安装新的 NVIDIA 驱动。

如果已有其他 Sunshine 在运行，安装完成后会提示先关闭旧实例，
并跳过立即启动和创建自动启动项。脚本不会停止其他 Sunshine 或禁用其服务。

首次启动后，打开 **https://localhost:47990** 创建账号。
从其他设备访问时将 `localhost` 换成 NX 的 IP；浏览器会提示自签名证书。
在 Moonlight 中添加 NX，完成 PIN 配对，选择 **Desktop**。
建议先使用 **1080p / 30 fps / 10 Mbps / H.264**。

## 从源码自行编译

```bash
git clone --branch v0.20.0-jetson.1 https://github.com/jdluuu/sunshine-jetson.git
cd sunshine-jetson
python3 scripts/jetson/install.py
```

脚本只初始化 Linux ARM64 所需的子模块，无需下载其他平台的 FFmpeg。
固定版本信息由 Git 子模块记录；请勿使用 `git submodule update --remote`。
GitHub 自动生成的源码 ZIP 不含子模块内容，建议使用 Git clone。

```bash
# 只检查硬件和插件，不安装任何东西
python3 scripts/jetson/install.py --check

# 只构建和测试，不部署、不修改输入权限、不启动服务
# 仍会准备依赖；已有依赖时加 --skip-deps
python3 scripts/jetson/install.py --build-only

# 选择显示器（默认自动选择活动主屏），不设置自动启动
python3 scripts/jetson/install.py --output DP-1 --no-autostart

# 其他参数
python3 scripts/jetson/install.py --help
```

支持 `--jobs N`，其中 N 至少为 16。安装目录可通过 `--prefix` 指定，
但必须位于当前用户主目录中。通过 SSH 安装时如未设置图形会话环境，
脚本会完成安装，并提示回到桌面终端启动。

## 启动、日志与回退

默认安装目录为 `~/.local/opt/sunshine-jetson/`：

```bash
python3 ~/.local/opt/sunshine-jetson/manage.py start
python3 ~/.local/opt/sunshine-jetson/manage.py status
tail -n 100 ~/.local/opt/sunshine-jetson/console.log
python3 ~/.local/opt/sunshine-jetson/manage.py stop
```

日志应包含 `Found encoder jetson: [nvv4l2h264enc]`。
启动器每次启动都会设置 `encoder = jetson`、`capture = x11`、
`hevc_mode = 1`，并重新计算 `output_name`，包含未连接接口在内的编号。
手动选择显示器保存在 `preferences.json` 的 `output` 字段；删除该字段恢复自动选择。

配置、账号、证书及配对保存在安装目录的 `config/` 内，与原有
`~/.config/sunshine/` 分开，所以首次使用此安装包需要重新配对。
程序通过 `SUNSHINE_JETSON_CONFIG_DIR` 使用独立配置；直接运行二进制且不设置
这个环境变量时，仍沿用上游的默认配置目录。

更新会保留配置、备份到 `backups/`，并保存前一版程序：

```bash
python3 ~/.local/opt/sunshine-jetson/manage.py rollback
python3 ~/.local/opt/sunshine-jetson/manage.py start
```

首次安装没有前一版，停用时执行：

```bash
python3 ~/.local/opt/sunshine-jetson/manage.py disable
```

这会停止本安装包的进程并移除其登录自动启动项，保留程序和配对信息。
原版 Sunshine 的程序、配置及服务不受影响，可以重新启动原版。
系统依赖和 `/etc/udev/rules.d/70-sunshine-jetson-uinput.rules` 会保留。

## 实现和验证

- `src/jetson_encoder.*`：VIC 色彩转换、NVENC 编码、原生 `force-IDR`。
- `src/video.cpp`：接入 Sunshine 的编码会话与视频包队列。
- `src/platform/linux/misc.cpp`：可选的独立配置目录。
- `scripts/jetson/`：构建、安装、显示器选择、启动及回退。
- `tests/jetson_probe.cpp`：在真实 NX 上运行的硬件编码测试。
- `tests/test_jetson_install.py`：显示器编号、配置保留、升级及回退回归测试。

```bash
python3 -m unittest discover -s tests -p 'test_jetson_install.py' -v
```

首次实测中，软件编码约 14–15 fps；使用本后端后接近 30 fps。
Sunshine CPU 采样从约 125–128% 降至 51–57%（100% 为一个核心）。
该数据来自一台设备，随画面和负载变化。编码器独立测试的吞吐不等同于
Moonlight 端到端帧率。GitHub CI 不包含真实 Jetson 硬件测试。

## 许可证与来源

本分支按 **GPL-3.0** 发布，见 [LICENSE](LICENSE)。
上游 Sunshine 与各第三方组件的版权及许可证保持不变。
上游基础提交：`31e8b798dabf47d5847a1b485f57cf850a15fcae`（v0.20.0）。
完整上游说明保存在 [README.rst](README.rst)。
NVIDIA 驱动和专有 GStreamer 插件由用户的 JetPack 安装提供，本仓库不重新分发。

参考：[NVIDIA R35.6 Accelerated GStreamer](https://docs.nvidia.com/jetson/archives/r35.6.0/DeveloperGuide/SD/Multimedia/AcceleratedGstreamer.html)。
