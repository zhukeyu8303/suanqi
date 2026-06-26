# SuanQi（算启）

SuanQi 是一个面向数学建模、科研计算和 CPU 密集型 Python 任务的一键式云算力调度工具。

用户只需在本地输入一条命令，SuanQi 就可以根据 CPU、内存、价格和地域等条件筛选合适的云服务器，并自动完成实例创建、运行环境配置、代码上传、依赖安装、程序执行、日志显示、结果下载和资源释放。

> 让使用云端高性能计算，像运行本地 Python 程序一样简单。

## 适用场景

- 数学建模中的大规模搜索、敏感度分析和启发式算法
- 多随机种子实验、批量仿真和数据处理
- 混合整数规划及其他耗时优化任务
- 科研计算与工程计算
- 机器学习数据预处理和 CPU 密集型任务

SuanQi 不局限于数学建模。目前公开版本首先支持腾讯云 CVM，并默认创建竞价实例，以较低成本运行可中断的计算任务。

## 当前状态

- 当前版本：`0.1.4`
- 开发阶段：Alpha
- 支持平台：Windows、Linux、macOS
- Python 版本：3.10 及以上
- 云服务商：腾讯云
- 远程系统：Ubuntu
- 当前任务入口：单个 `.py` 文件

Alpha 版本已经能够完成完整任务链路，但仍可能存在未覆盖的边界情况。首次使用时建议先运行耗时较短的小任务，并在腾讯云控制台确认实例已经按预期释放。


## 默认竞价实例

SuanQi 默认使用腾讯云竞价实例运行任务。竞价实例与普通按量计费实例使用方式基本相同，但价格通常更低，更适合数学建模、批量仿真、随机搜索和其他能够容忍中断的计算任务。

### 优势

- 相比普通按量计费实例，CPU 和内存价格通常更低。
- 可以用较低成本临时获得更多 CPU 和内存。
- 实例性能与同规格按量计费实例没有本质差别。
- 很适合运行时间有限、结果可保存、任务可以重新执行的计算。

### 风险

- 竞价实例可能因腾讯云资源库存不足被系统主动回收。
- 实例被回收时，正在运行的程序会中断。
- 存放在实例本地磁盘、且尚未下载或上传到 COS 的数据可能丢失。
- 竞价实例不适合数据库、网站主服务、持续在线服务，以及任何不能中断的任务。

腾讯云说明，当前波动型竞价实例通常不会因为市场价格变化而被回收，但可能因资源库存不足被随机回收。系统中断前可能仅提供很短的通知时间，因此不应依赖人工处理。详情见：

- [腾讯云官方文档：竞价实例（波动型）](https://cloud.tencent.com/document/product/213/17816)
- [腾讯云官方文档：竞价实例问题](https://cloud.tencent.com/document/product/213/17817)

使用竞价实例时建议：

- 启用 `suanqi useos`，让 worker 将任务状态、日志和结果上传到 COS。
- 让程序定期保存检查点或阶段性结果。
- 使用 `--return` 明确指定最终需要下载的文件。
- 将任务设计为可以重新运行，或能够从检查点继续。
- 对不能中断的重要任务，不要依赖竞价实例。

即使启用了 COS，也不能保证在实例突然回收前完成最后一次上传。重要数据应由程序主动、定期保存，而不是只在任务结束时保存一次。

## 工作流程

```text
本地执行命令
    ↓
筛选满足 CPU 和内存要求的实例
    ↓
展示价格并由用户选择
    ↓
创建云服务器并等待 SSH
    ↓
上传 Python 文件和依赖清单
    ↓
创建虚拟环境并安装依赖
    ↓
启动服务器端 worker
    ↓
实时显示程序日志
    ↓
下载指定结果文件
    ↓
上传 COS 备份（启用时）
    ↓
自动释放云服务器
```

远程任务由独立的服务器端 worker 管理。即使本地终端关闭或 SSH 临时断开，任务也可以继续运行，之后可使用 `attach` 重新连接。

## 安装

```bash
pip install suanqi
```

升级到最新版：

```bash
pip install --upgrade suanqi
```

检查是否安装成功：

```bash
suanqi -h
```

## 首次使用腾讯云

首次使用腾讯云时，可能需要先登录腾讯云并开通 SuanQi 使用的云产品：

- [开通云服务器 CVM](https://cloud.tencent.com/product/cvm)：用于创建和运行远程计算实例。
- [开通对象存储 COS](https://cloud.tencent.com/product/cos)：用于保存任务状态、日志和结果备份。COS 主要用于断线容灾，建议开通。

开通产品本身不等于已经创建付费资源。实际费用通常在 SuanQi 创建云服务器、使用公网流量或向 COS 写入数据后产生，具体以腾讯云控制台和账单为准。

## 配置腾讯云密钥

SuanQi 从环境变量读取腾讯云 API 密钥：

- `TENCENTCLOUD_SECRET_ID`
- `TENCENTCLOUD_SECRET_KEY`

请按照腾讯云官方文档创建访问密钥：

- [腾讯云官方教程：主账号访问密钥管理](https://cloud.tencent.com/document/product/598/40488)

访问密钥由 `SecretId` 和 `SecretKey` 组成。腾讯云目前只在密钥创建时展示 `SecretKey`，创建后无法再次查询，因此请在创建时立即妥善保存。请勿把密钥直接写进代码、提交到 GitHub 或发送给其他人。

### Windows PowerShell

仅在当前终端中设置：

```powershell
$env:TENCENTCLOUD_SECRET_ID="你的 SecretId"
$env:TENCENTCLOUD_SECRET_KEY="你的 SecretKey"
```

永久写入当前 Windows 用户的环境变量：

```powershell
[Environment]::SetEnvironmentVariable(
    "TENCENTCLOUD_SECRET_ID",
    "你的 SecretId",
    "User"
)

[Environment]::SetEnvironmentVariable(
    "TENCENTCLOUD_SECRET_KEY",
    "你的 SecretKey",
    "User"
)
```

永久设置后，请重新打开终端。

### Windows CMD

```cmd
set TENCENTCLOUD_SECRET_ID=你的 SecretId
set TENCENTCLOUD_SECRET_KEY=你的 SecretKey
```

### Linux / macOS

```bash
export TENCENTCLOUD_SECRET_ID="你的 SecretId"
export TENCENTCLOUD_SECRET_KEY="你的 SecretKey"
```

腾讯云账号需要具有执行相应 CVM、VPC、COS、CAM 和账户查询操作的权限。建议使用单独的子账号和最小必要权限，不要长期使用主账号密钥。

## 第一次运行

准备一个 Python 文件，例如 `main.py`：

```python
from pathlib import Path

result_path = Path("result.txt")  # result_path 表示结果文件路径
result_path.write_text("Hello from SuanQi!", encoding="utf-8")

print("计算完成")
```

运行任务并取回结果：

```bash
suanqi run main.py --return result.txt
```

SuanQi 会展示候选实例及价格。选择实例后，它将自动创建服务器、运行程序、下载 `result.txt`，并在任务结束后释放实例。

## 常用命令

### 运行 Python 任务

```bash
suanqi run main.py
```

指定最低 CPU 和内存：

```bash
suanqi run main.py --cpu 32 --memory 64
```

指定最大运行时间：

```bash
suanqi run main.py --maxusetime 2h
```

下载一个或多个结果文件：

```bash
suanqi run main.py --return result.xlsx,output.txt
```

安装 `requirements.txt` 中的依赖：

```bash
suanqi run main.py -r requirements.txt --return result.xlsx
```

直接指定需要安装的包，`-i` 可以重复使用：

```bash
suanqi run main.py -i numpy -i pandas -i openpyxl
```

同时使用依赖文件和额外依赖：

```bash
suanqi run main.py -r requirements.txt -i openpyxl --return result.xlsx
```

任务完成后保留服务器：

```bash
suanqi run main.py --keep
```

> `--keep` 会阻止任务结束后自动释放实例。服务器将继续计费，使用后必须自行执行 `suanqi release <实例ID>` 或前往腾讯云控制台释放。

### 重新连接任务

```bash
suanqi attach ins-xxxxxxxx
```

`attach` 接收的是腾讯云实例 ID。它会读取本地任务记录，重新连接服务器，并继续显示任务日志。

### 查看 SuanQi 管理的实例

```bash
suanqi --list
```

也可以使用简写：

```bash
suanqi -l
```

### 查看本地历史任务

```bash
suanqi history
```

本地任务记录默认保存在：

```text
~/.suanqi/tasks
```

Windows 通常对应：

```text
C:\Users\你的用户名\.suanqi\tasks
```

### 强制释放实例

```bash
suanqi release ins-xxxxxxxx
```

释放前需要输入 `RELEASE` 二次确认。跳过确认：

```bash
suanqi release ins-xxxxxxxx --yes
```

强制释放可能导致尚未下载或上传的结果永久丢失。

## 资源参数规则

SuanQi 的 `--cpu` 和 `--memory` 表示最低要求，不保证最终实例恰好等于指定配置。

| 参数 | 最终最低要求 |
|---|---|
| 均未指定 | 16 核、16 GB |
| 只指定 `--cpu 32` | 32 核、32 GB |
| 只指定 `--memory 64` | 64 核、64 GB |
| 同时指定 | 分别使用指定值 |

示例：

```bash
suanqi run main.py --cpu 80 --memory 128
```

限制每个地域最多保留的候选机型数量：

```bash
suanqi run main.py --maximum-region-instances 20
```

默认值为 `10`。增大该值可能发现更多候选实例，但查询和询价过程也会更慢。

## 最大运行时间

默认最大运行时间是 `5h`。

支持的格式包括：

```text
30s      30 秒
20m      20 分钟
5h       5 小时
1h30m    1 小时 30 分钟
1d2h     1 天 2 小时
```

例如：

```bash
suanqi run main.py --maxusetime 1h30m
```

最大运行时间只计算用户 Python 程序真正运行的时间，不包含以下阶段：

- 创建云服务器
- 等待 SSH 可用
- 创建 Python 虚拟环境
- 安装依赖

任务超时后，worker 会终止用户程序、整理状态、尝试上传日志和结果，并根据任务设置释放实例。

## COS 断线容灾

SuanQi 可以使用腾讯云对象存储 COS 保存任务状态、日志和结果。当本地断开连接，或者服务器已经释放时，仍可从 COS 拉回已上传的内容。

首次启用：

```bash
suanqi useos
```

默认会：

- 使用南京地域 `ap-nanjing`
- 创建或绑定 SuanQi 专用 Bucket
- 使用 `tasks` 作为对象前缀
- 创建或更新服务器 worker 所需的腾讯云实例角色
- 将配置保存到 `~/.suanqi/config.json`

指定地域：

```bash
suanqi useos --region ap-shanghai
```

绑定已有 Bucket：

```bash
suanqi useos --region ap-shanghai --bucket suanqi-1250000000
```

Bucket 名称需要使用腾讯云完整名称，通常包含 AppID。

从 COS 下载某个任务的结果：

```bash
suanqi fetch task-xxxxxxxx
```

指定下载目录：

```bash
suanqi fetch task-xxxxxxxx --output ./downloaded-results
```

任务 ID 可以通过以下命令查看：

```bash
suanqi history
```

COS 会产生少量存储和请求费用，具体以腾讯云实际计费为准。

## 依赖安装

SuanQi 会在远程服务器上为每个任务创建独立 Python 虚拟环境。

腾讯云中国大陆服务器默认使用腾讯云 PyPI 镜像，以提高依赖下载速度。

推荐把项目依赖写入 `requirements.txt`：

```text
numpy
pandas
scipy
openpyxl
```

然后运行：

```bash
suanqi run main.py -r requirements.txt
```

当前版本只自动上传指定的 Python 文件和可选的 `requirements.txt`，不会自动上传整个本地项目目录。因此，入口文件不应依赖未上传的本地模块或数据文件。

## 返回文件

通过 `--return` 指定需要下载的文件：

```bash
suanqi run main.py --return result.xlsx,logs/output.txt
```

文件路径相对于远程任务的用户目录。请确保程序在退出前已经生成这些文件。

未指定 `--return` 时，程序日志仍会显示，但普通结果文件不会自动下载到本地。启用 COS 后，worker 会按其任务配置尝试上传任务资料。

## 中断、失败与恢复

### 本地终端关闭或网络断开

服务器端 worker 通常会继续运行。重新打开终端后执行：

```bash
suanqi attach <实例ID>
```

### 用户程序报错

SuanQi 会保留用户程序的退出状态和日志，并继续执行任务收尾流程。服务器是否释放取决于是否启用了 `--keep`。

### 服务器已经释放

启用 COS 后，可以尝试：

```bash
suanqi history
suanqi fetch <任务ID>
```

### 实例没有自动释放

先查看实例：

```bash
suanqi --list
```

然后手动释放：

```bash
suanqi release <实例ID>
```

也应登录腾讯云控制台确认不存在遗留的按量计费实例。

## 费用说明

SuanQi 本身是开源工具，但创建的云服务器、公网流量和 COS 资源可能产生费用。

- SuanQi 默认创建竞价实例，价格较低，但实例可能被腾讯云主动回收。
- 创建实例前请仔细检查终端显示的价格和计费单位。
- 实际费用以腾讯云账单为准。
- 按量计费实例在释放前可能持续产生费用。
- `--keep` 会保留服务器，应谨慎使用。
- 程序异常、电脑关机或网络断开后，也应检查实例是否已经释放。
- 首次测试建议选择低配置、短运行时间的小任务。

## 安全说明

- 不要把 `SecretId` 和 `SecretKey` 写入代码或提交到 GitHub。
- 推荐使用腾讯云子账号并配置最小必要权限。
- SuanQi 会在本地 `~/.suanqi/tasks` 保存任务连接信息，请保护好本地用户目录。
- 启用 COS 时，服务器通过腾讯云实例角色获取临时凭据，不需要上传长期 SecretKey。
- 当前版本面向用户自己编写并信任的 Python 程序，不应运行来源不明的代码。

## 命令帮助

查看全部命令：

```bash
suanqi -h
```

查看某个命令的详细参数：

```bash
suanqi run -h
suanqi attach -h
suanqi release -h
suanqi useos -h
suanqi fetch -h
```

## 当前限制

- 当前只支持腾讯云。
- 当前只支持上传并执行单个 `.py` 文件。
- 暂不自动上传整个项目目录、数据集或本地模块。
- 当前主要面向 CPU 计算任务，尚未提供完整 GPU 调度流程。
- 价格、库存和实例可用性由腾讯云实时结果决定。
- Alpha 版本暂未覆盖所有异常场景。

## 从源码安装

克隆仓库后，在项目根目录执行：

```bash
python -m pip install -e .
```

检查 CLI：

```bash
suanqi -h
```

构建发行包：

```bash
python -m pip install --upgrade build
python -m build
```

## 版本 0.1.4

- 完成腾讯云实例筛选、询价、创建和远程初始化流程。
- 支持 Python 虚拟环境、`requirements.txt` 和 `-i` 安装依赖。
- 支持实时日志、断线后 `attach` 和本地任务历史记录。
- 支持指定结果文件下载。
- 支持 COS 状态及结果容灾。
- 支持最大运行时间和超时终止。
- 支持任务结束后自动释放或使用 `--keep` 保留实例。
- 修复 COS 上传完成后服务器端自销毁未触发的问题。
- worker 退出前会同步调用腾讯云 `TerminateInstances`。
- COS 上传失败不再阻止实例释放。

## 参与贡献

SuanQi 仍处于早期开发阶段，欢迎任何形式的反馈与贡献。

如果你在使用过程中遇到问题，或有新的功能建议，欢迎提交 Issue。提交时建议附上：

- 使用的操作系统和 Python 版本
- SuanQi 版本
- 执行的完整命令
- 终端报错信息或相关日志
- 可以复现问题的最小示例

如果你愿意直接参与开发，也欢迎提交 Pull Request。可以从以下方向入手：

- 修复错误和补充异常处理
- 改进文档与使用示例
- 增加自动化测试
- 优化腾讯云实例筛选和任务恢复流程
- 增加新的云服务商支持
- 改进 Windows、Linux 和 macOS 兼容性

提交 Pull Request 前，请尽量确保：

1. 修改内容与本次提交目标相关，避免混入无关改动。
2. 新增或修改的代码能够正常运行。
3. 涉及用户行为变化时，同步更新 README 或命令帮助。
4. 不要在代码、日志和截图中提交 SecretId、SecretKey、实例密码等敏感信息。

即使只是发现了一个错别字、文档表述不清，或者提出一个想法，也非常欢迎提交 Issue 或 Pull Request。

## 开源协议

本项目使用 [MIT License](LICENSE)。

## 免责声明

SuanQi 按“原样”提供，不保证适用于所有环境。使用者应自行确认云资源价格、账号权限、任务数据安全和实例释放状态。因云资源费用、数据丢失、程序错误或账号配置不当造成的损失，由使用者自行承担。
