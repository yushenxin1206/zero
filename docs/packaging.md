# macOS 打包说明

本文对应第一阶段的产物：**可双击启动、功能与命令行一致、未签名**的 `.app`。
签名、公证、自动更新属于第二阶段，这里只说明边界与后续动作。

构建脚本：[scripts/build_macos_app.py](../scripts/build_macos_app.py)
启动器：[packaging/macos/launcher.c](../packaging/macos/launcher.c)

## 1. 前置条件

- macOS（构建脚本只在这上面运行），已安装 Xcode Command Line Tools：
  `clang`、`lipo`、`sips`、`iconutil`、`ditto` 都必须可用。
- 已安装 [uv](https://docs.astral.sh/uv/)。
- 仓库根目录执行过 `uv sync`，存在 `.venv`，且解释器是 **uv 管理的独立 CPython 3.14**。
- 不需要完整 Xcode，也不需要 Apple 开发者账号（第一阶段不签名）。

检查：

```bash
uv --version
xcrun --find clang
.venv/bin/python -V
```

## 2. 构建命令

```bash
# 本机架构
.venv/bin/python scripts/build_macos_app.py

# 显式指定架构
.venv/bin/python scripts/build_macos_app.py --arch x86_64
.venv/bin/python scripts/build_macos_app.py --arch arm64

# 指定一个现成的同架构解释器前缀（必须含 bin/python3.14 与 lib/python3.14）
.venv/bin/python scripts/build_macos_app.py --arch arm64 \
  --python-prefix "/path/to/cpython-3.14-arm64"
```

参数：

| 参数 | 说明 |
|---|---|
| `--arch {x86_64,arm64}` | 目标架构，默认取构建机架构 |
| `--python-prefix PATH` | 手动指定内嵌 CPython 前缀，架构必须与 `--arch` 一致 |
| `--no-compile` | 跳过字节码预编译，只用于排查；正式产物不要用 |

## 3. 架构规则（重要）

- 构建 **不会** 用别的架构解释器凑数。目标架构找不到同架构的独立 CPython 时，
  脚本在复制任何文件之前就失败，不会产出“看起来能跑”的错误包。
- 自动查找顺序：
  1. `--python-prefix`；
  2. uv 解释器目录里匹配 `cpython-3.14.*-macos-<arch>-none` 的版本；
  3. 本机 `.venv` 的基解释器（仅当架构一致）。
- 本机只有 x86_64 的 Python 时，`--arch arm64` 会直接报：

  ```
  构建失败：找不到 arm64 架构的独立 CPython 3.14。
  请在一台 arm64 的 Mac 上执行 `uv python install 3.14`，或用 `--python-prefix` 指向一个 arm64 的解释器前缀。
  ```

- 交叉构建（在 x86 上产出 arm64）是支持的，前提是提供 arm64 解释器前缀。
  依赖通过 `uv pip install --python-platform aarch64-apple-darwin
  --python-version 3.14 --only-binary :all:` 安装，只接受 wheel；
  本机只做静态架构校验，**无法真机运行验证**。
- 构建结束前会扫描包内所有 `.so` / `.dylib` / 解释器 / 启动器，
  任何一个 Mach-O 缺少目标架构切片都会失败。依赖里的 universal2 轮子
  同时含 `arm64/x86_64`，属于正常情况。
- 字节码与架构无关：交叉构建时用宿主 3.14 代编 `.pyc`；
  宿主不是 3.14 时跳过预编译（运行期仍禁止写 pyc）。

验证产物架构：

```bash
lipo -archs dist/x86_64/Zero.app/Contents/Resources/python/bin/python3.14
lipo -archs dist/x86_64/Zero.app/Contents/MacOS/Zero
```

### 3.1 在 Intel 机器上交叉构建 arm64：可行性评估

结论：**可行，但本机只能做静态校验，必须到 Apple Silicon 上真机验收。**

- 依赖解析：45 条运行时依赖全部能按 `aarch64-apple-darwin` 解析。
- 本机实际做过一次 arm64 依赖安装（只装到 `/tmp`，没有构建 .app）：
  扫描 208 个 Mach-O，全部包含 arm64 切片；其中 206 个是 universal2
  （`arm64 + x86_64`），2 个是纯 arm64。
- 唯一没有 wheel 的是 `proxy-tools`（pywebview 依赖），它是**纯 Python
  源码包**，构建产物是 `py3-none-any`，不涉及 C 编译；构建脚本交叉安装时
  已单独放行。
- 因此交叉构建唯一缺的是 **arm64 解释器前缀**。本机只有 x86_64 的
  独立 CPython，`--arch arm64` 会在开始复制前直接报错。
- 交叉构建时依赖安装走 `--only-binary :all:`（除 `proxy-tools` 外），
  产物落地前会扫描所有 Mach-O 是否含目标架构，混入错误架构会失败。

## 4. 产物位置

| 路径 | 内容 |
|---|---|
| `dist/<arch>/Zero.app` | 最终产物，按架构分目录，可并存 |
| `build/macos/<arch>/` | 中间目录：解释器副本、依赖、图标、启动器 |

每次构建都会先清空目标架构的 `build/macos/<arch>` 与 `dist/<arch>`。
同一份源码、同一架构连续构建两次，文件清单与 SHA-256 完全一致
（已验证），可用于校验构建环境是否漂移。

包内结构：

```
Zero.app/Contents/
├── Info.plist
├── MacOS/Zero                 # C 启动器
└── Resources/
    ├── zero.icns
    ├── app/zero/              # 源码（含 static 与 skills）
    ├── python/                # 内嵌独立 CPython
    └── site-packages/         # 按 uv.lock 安装的依赖
```

## 5. 数据根与包内资源分离

- **包内只读**：`Resources/app`、`Resources/python`、`Resources/site-packages`
  都是构建产物；运行期不写入。启动器设置了 `PYTHONDONTWRITEBYTECODE=1`，
  防止 Python 往包里写 `__pycache__` 破坏签名。
- **用户数据根**：`~/.zero`（配置、会话、日志、定时任务、权限策略、主题）。
  首次启动打开引导页，配置写入 `~/.zero/config.json`（权限 `0600`）。
- 会话级工作目录由用户选择，只允许绝对路径；沙箱根跟随会话目录。
- 日志：`~/.zero/logs/debug.log`（滚动 2MB × 3）；
  启动器自身起不来时写 `~/.zero/logs/launcher-error.log`。

## 6. 未签名包怎么运行

本机构建、本机运行（没有 `com.apple.quarantine`）：

```bash
open dist/x86_64/Zero.app
# 或直接跑启动器（便于看 stderr）
dist/x86_64/Zero.app/Contents/MacOS/Zero --help
```

从网络下载/别人传给你的 zip、DMG 会带隔离标记，未签名时会提示
“无法打开”或“已损坏”。临时绕过：

```bash
xattr -dr com.apple.quarantine /path/to/Zero.app
# 然后右键 → 打开，或系统设置 → 隐私与安全性 → 仍要打开
```

注意：

- 这只是开发期办法，**不能作为分发方案**；分发给他人必须走签名 + 公证。
- 从 DMG/下载目录直接运行会触发 App Translocation（随机只读路径）；
  数据仍然写 `~/.zero`，但更新/替换应用时容易混乱，建议先拖到
  `~/Applications` 或 `/Applications`。
- 首次访问桌面、文档、下载目录会触发 TCC 授权；agent 读写这些目录前
  需要你在弹窗里允许，必要时给 Zero 授予“完全磁盘访问权限”。

相关命令：

```bash
xattr -l dist/x86_64/Zero.app
spctl --assess --type execute -vv dist/x86_64/Zero.app   # 未签名会被拒绝，属预期
```

## 7. 签名与公证（第二阶段）

当前产物**没有签名**，`security find-identity -v -p codesigning` 也确认本机
没有可用身份。第二阶段需要：

1. Apple Developer Program 账号 + Developer ID Application 证书；
2. 逐层签名（先 `site-packages` 里所有 `.so`/`.dylib`、内嵌解释器、
   启动器，最后签整个 bundle），开启 hardened runtime 与 timestamp；
3. `notarytool submit --wait` 提交公证，`stapler staple` 装订；
4. 对 DMG 一并签名/公证；
5. 用 `spctl`、`codesign --verify --deep --strict`、`stapler validate`
   在一台干净 Mac 上验收。

不要启用 App Sandbox：应用需要执行 `sandbox-exec` 与用户命令，
App Store 沙箱模型与之冲突；Developer ID 直接分发不需要 App Sandbox。

## 8. 常见失败与排查

| 现象 | 原因 | 处理 |
|---|---|---|
| `找不到 <arch> 架构的独立 CPython 3.14` | 本机没有目标架构解释器 | 换到对应架构的 Mac 执行 `uv python install 3.14`，或用 `--python-prefix` |
| `解释器缺少 arm64 架构切片` | `--python-prefix` 指错架构 | 换正确的解释器前缀，不要用 Rosetta 的 x86 解释器 |
| `产物里存在缺少 <arch> 架构的 Mach-O` | 某个依赖只提供了别的架构（或 sdist 本机编译） | 查列出的文件；交叉构建时确保 `--only-binary :all:` 能装到对应 wheel |
| `uv pip install` 报找不到 wheel | 依赖没有目标架构 wheel | 换依赖版本，或放弃该架构的交叉构建 |
| `iconutil` 失败 | 源 PNG 损坏/尺寸不对 | 确认 `zero/channels/web/static/zero-icon.png` 是 1024×1024 |
| `clang: error` | 缺 Command Line Tools / SDK | `xcode-select --install`，检查 `xcrun --find clang` |
| 双击无反应，无窗口 | 启动器或解释器起不来 | 看 `~/.zero/logs/launcher-error.log`；终端直接跑 `Contents/MacOS/Zero` 看 stderr |
| 打开提示“已损坏” | 下载隔离 + 未签名 | 开发期 `xattr -dr com.apple.quarantine`；正式分发需公证 |
| 终端命令提示 `command not found` | Finder 启动的 PATH 与终端不同 | 启动器已加内嵌解释器、`/opt/homebrew/bin`、`/usr/local/bin`；仍缺就装对应工具 |
| 权限页显示沙箱不可用 | `sandbox-exec` 被系统移除/策略拦截 | `ls -l /usr/bin/sandbox-exec`；这是系统能力问题，不是打包问题 |
| 访问桌面/文档被拒 | TCC 未授权 | 系统设置 → 隐私与安全性，给 Zero 授权或授予完全磁盘访问 |
| 改配置后重启仍是旧配置 | 重启失败或配置未保存 | 看 `debug.log`；确认重启后进程 owner 是 Zero 而不是 python3.14 |

## 9. 构建后冒烟清单

1. `open dist/<arch>/Zero.app`，窗口出现，无终端；
2. 新建会话，发一条消息，能流式回复；
3. 触发一次写包络外路径，审批卡出现，允许后命令生效；
4. 切换主题，`~/.zero/web-theme` 更新，重启后保持；
5. 清空 `HOME` 启动，能进入首次配置引导；
6. 配置页保存 → 重启，新进程 owner 为 Zero（LaunchServices）；
7. 权限页：内核沙箱可用、断网档生效；
8. 内置技能（plan / web-search）可运行；
9. 退出后无残留进程；运行期间包内新增文件数为 0。

## 10. 用 GitHub Actions 打 arm64 包

工作流：[.github/workflows/build-macos-app.yml](../.github/workflows/build-macos-app.yml)

它在 GitHub 托管的 Apple Silicon 机器（`macos-15`）上执行同一份构建脚本，
产出 arm64 包、校验架构、用 `ditto` 压缩后上传为可下载产物。

job 依次做这些事，和本机手工构建等价：

| 步骤 | 做什么 |
|---|---|
| checkout | 拉取仓库 |
| setup-uv | 安装 uv |
| 架构自检 | `uname -m` 必须是 `arm64`，否则直接失败 |
| `uv sync --frozen --python 3.14` | 装依赖，并让 uv 下载 arm64 的 CPython 3.14 |
| 构建 | `uv run python scripts/build_macos_app.py --arch arm64` |
| 架构校验 | 用 `lipo -archs` 确认解释器与启动器都含 arm64 |
| 压缩上传 | `ditto` 打成 zip，再用 `actions/upload-artifact` 上传 |

因为 `pyproject.toml` 只写了 `requires-python >=3.12`，工作流里显式指定 3.14：
内嵌解释器必须是 3.14，宿主也用它代编字节码。

### 10.1 从推代码到拿到产物

1. 把工作流文件提交并推到 GitHub：

   ```bash
   git add .github/workflows/build-macos-app.yml
   git commit -m "ci: build arm64 macOS app"
   git push origin dev
   ```

2. 打开仓库的 **Actions** 页，左侧选 **Build macOS app**。
3. 点 **Run workflow**，选择分支（例如 `dev`），确认运行。
4. 等 job 变绿。首次运行如果卡在排队，先确认仓库的 Actions 权限与
   私有仓库的 macOS runner 额度。
5. 打开这次 run，页面底部 **Artifacts** 区域下载
   `Zero-arm64-<version>`，得到 `Zero-arm64-<version>.zip`。

打 tag 会自动触发同一工作流：

```bash
git tag v0.1.0
git push origin v0.1.0
```

也可以在仓库 **Releases** 里基于该 tag 建发布，把 zip 作为附件；
Actions 的 artifact 默认保留 14 天，过期后只能重跑或从 Release 取。

### 10.2 产物里有什么

- 压缩包内是完整的 `Zero.app`，用 `ditto -c -k --sequesterRsrc --keepParent`
  打包，权限位与符号链接都会保留；不要用会丢权限位的普通 zip 工具重打。
- 产物未签名、未公证，仅供内部验证。
- 解压后校验：

  ```bash
  ditto -x -k Zero-arm64-0.1.0.zip .
  lipo -archs Zero.app/Contents/Resources/python/bin/python3.14   # arm64
  lipo -archs Zero.app/Contents/MacOS/Zero                        # arm64
  ```

### 10.3 公开仓库与私有仓库的计费差别

- **公开仓库**：标准 GitHub 托管 runner（含 `macos-15` arm64）免费。
- **私有仓库**：消耗账户的 Actions 分钟数；macOS 按 **10 倍** 系数扣减
  （runner 跑 1 分钟，计 10 分钟），超出套餐后按分钟计费，3 核 arm64
  大约 $0.062/分钟；`macos-15-xlarge` 等更大机型更贵。
- 私有仓库能否使用标准 arm64 runner 取决于套餐与额度，以 GitHub 当前
  计费文档为准。一次完整构建（含装依赖）约 3–6 分钟，按 10 倍算约
  30–60 分钟额度。

### 10.4 下载回来的未签名包

从浏览器下载的 zip 会带 `com.apple.quarantine`，解压后的 `.app` 直接双击
会被 Gatekeeper 拦住（“无法打开”或“已损坏”）。内部验证时：

```bash
xattr -dr com.apple.quarantine Zero.app
# 或右键 → 打开；必要时到 系统设置 → 隐私与安全性 → 仍要打开
```

这只能用于内部验证。对外分发必须走第二阶段的签名 + 公证，否则每个用户
都要手动去隔离标记。

## 11. 已知限制

- 产物未签名，仅适合本机/内部验证；
- 当前验收环境为 Intel（x86_64），arm64 产物只能做静态校验，需 ARM Mac 真机验收；
- GitHub 工作流已加入，但尚未在真实仓库跑过；首次运行可能需要按仓库
  设置调整 runner 标签或额度；
- 包体积约 274 MB（内嵌解释器 + lark-oapi 全部 API 模型）；
- 不做自动更新；升级方式是替换整个 `.app`。
