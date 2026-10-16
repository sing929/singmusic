# Sing Studio v1.4.0 · 2026-10-16

## 本次交付

- 用户确认需求（2026-10-16）：换声的目标音色是**原曲演唱者**，即从导入的原曲用 UVR 分离出的原唱，不另给音色片段、不用 RVC 模型、不训练。只在**生成前**设置（统一 + 逐首，A / B 分别选择），已完成作品不提供换声再处理。换声引擎在 RTX 5070 应用电脑运行；用户在另一台联网的 i5 电脑下载安装包后拷贝到应用电脑。开发电脑不得下载或安装引擎。只对有权使用的声音换声。
- 引擎选择：Seed-VC v1 歌唱模型（`DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth`，44.1 kHz，F0 条件，零样本），代码固定在 `Plachtaa/seed-vc@51383efd921027683c89e5348211d93ff12ac2a8`（GPL-3.0，以独立程序调用）。开发前已在 GitHub 阅读该提交的 `inference.py`、`hf_utils.py`、配置与依赖，未下载或运行。
- 新模块 `dual-version/voice_swap.py`（应用 Python 中运行）：设置校验（统一 / 逐首 `voice_swap = {a, b, octave}`，任务 `voice = {target: original, octave}`）、`available()`（以 `local-test/seed-vc/install.json` 自检通过为准）、乐句切分（10 ms 能量门限，长于 30 秒在最安静处切开）、原唱参考片段（去掉停顿，取最稳定的 20 秒）、pYIN 中位音高与整八度规则（相差 ≥ 7 个半音时移整八度，限 ±12）、调用换声、按原位置放回（10 ms 淡入淡出）、按原人声响度匹配增益、与新伴奏重新混音并防削波，写 `voice-swap.json`。
- 新脚本 `dual-version/seedvc_runner.py`（在 Seed-VC 独立环境中运行）：切换到 Seed-VC 目录导入其 `inference`，模型只加载一次后逐乐句调用其 `main()`；不是 CUDA 时拒绝（测试可用 `SING_VOICE_ALLOW_CPU=1`）；用引擎自带 CAM++ 与 RMVPE 计算换声前后与原唱的音色相似度和音高误差；`--self-test` 供安装脚本检查显卡。PyTorch 2.6 起 `torch.load` 默认 `weights_only=True`，固定版本的检查点按旧方式加载（`TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`，只针对固定的官方文件）。
- 管线（`engine.run_pipeline`）：有换声时先检查引擎（未安装直接失败，不耗 GPU）；原曲变调变速时先保存 `source-unshifted.wav`，另行分离到 `reference-stems`，否则使用（或补做）`original-stems`；生成前做参考片段。生成后 `separate-generated` → `voice_swap.convert` → 混响 → 验证。`generated.wav` 保留供对比。A / B 复用分析时同时复制 `reference-stems`。结果说明附换声提示。
- 服务（`service.py`）：设置与逐首保存 `voice_swap`；批次按 A / B 映射为任务 `voice`；保留原唱与纯音乐忽略换声；请求换声而引擎未安装时整批拒绝（不创建任务）；`/api/state` 的 `engines.voice`；作品名附 `Original Voice`（派生的变调变速作品继承）；完成作品返回 `voice_info` 摘要。
- 界面：统一设置新增“换成原唱音色”面板（A / B 开关、音域、未安装提示与版权提醒）；逐首“单独调整”可跟随统一或单独设置；队列确认显示换声行，引擎未安装时显示警告并禁用确认；队列步骤、队列行、作品卡片、详情、使用说明与更新记录。沿用森林绿 / 米白。
- 安装：`seedvc-requirements.txt`（torch / torchaudio 2.7.1+cu128 等固定版本）、`seedvc-download.ps1`（联网电脑：`pip wheel` 生成全部 wheel——其中 `julius`、`randomname` 只有源码包，在此构建——下载固定提交源码与 Hugging Face 模型，把符号链接转为普通文件，记录 SHA256）、`seedvc-install.ps1`（应用电脑离线：校验、用 ace-env 同一 Python 建独立 venv、离线安装、显卡自检，自检通过后最后写 `install.json`）。
- 版本源、package / lock、README、CHANGELOG 同步为 1.4.0；`deploy-release.ps1` 文件清单加入 `voice_swap.py`、`seedvc_runner.py`；`.gitignore` 允许清单加入新文件。`benchmark_adjust.py` 与新 `benchmark_voice.py` 共用验证记录：`VALIDATION-v1.4.0.json` 的 `required_benchmarks` 为 adjust 与 voice，两者都通过才 `passed`。

## 本次验证（开发电脑 Xeon + GTX 1070，无 YuE2 / Seed-VC）

- 前端生产构建与 4 项打包测试通过（`node_modules` 链接到同机 v1.3.0 克隆，锁文件除版本号外相同，未重新下载）。
- 后端：原有 8 项 HTTP / 队列、6 项控制、6 项纯音乐、8 项变调变速回归通过；新增 `test_voice.py` 9 项通过。换声测试使用与 Seed-VC 接口一致的替代 `inference.py`，运行真实的 `seedvc_runner.py` 子进程：模型只加载一次、每乐句一次调用及参数（F0 条件、不自动调音高、八度、步数、CFG、fp16、参考片段）、静音段保持静音、乐句位置与音高、响度匹配、混音、生成原声不变、失败信息、无人声时跳过、自检与无显卡拒绝；管线顺序（未调整用 original-stems，变调时用未调整原唱的 reference-stems）、引擎缺失时零阶段失败、A / B 复用；HTTP 设置与逐首、A / B 映射、命名、保留原唱 / 纯音乐忽略、未安装时整批拒绝；派生作品命名、作品信息与结果说明；验证记录需全部基准通过。
- 安装脚本：两份 PowerShell 解析无误；用假安装包在短路径做离线演练，校验、Python 识别、独立 venv、离线 pip、自检调用均按预期，自检失败时不写 `install.json`；已存在需 `-Replace`、文件损坏会被拒绝。固定的依赖版本已在 PyPI 与 PyTorch cu128 索引核对存在 cp311 Windows wheel（只查询，未下载）。
- 隔离浏览器实际操作（替代安装、暂停的队列）：未安装时面板提示且开关不可用；安装后 A 开关、音域保存与刷新保持；逐首单独设置 A + B；队列确认的换声行；入队的 A / B 任务都带 `voice`（逐首降八度），名称 `R&B Demo Song (Original Voice)` / `Jazz Demo Song (Original Voice)`；作品卡片与详情的换声信息；换声作品的 Slowed 80% 预览名与后端名称一致 `Lo-fi Demo Song (Original Voice, Slowed 80%)`；未安装时确认对话框警告并禁用确认；375 px 宽无横向滚动；无应用报错（仅重启服务期间的连接拒绝）。
- **未完成：应用电脑上的引擎安装与显卡自检、YuE2 + UVR + Seed-VC 实测、主观试听。** `VALIDATION-v1.4.0.json` 为 `passed: false`，部署脚本会拒绝安装，直到下面两个基准都通过。v1.3.0 的变调变速实测同样尚未在应用电脑完成，本次一并要求。

## 在联网的 i5 电脑上下载（约 6 GB）

1. 先在应用电脑查看 Python 版本：`local-test\ace-env\Scripts\python.exe --version`（例如 3.11.x）。i5 电脑需要安装同一小版本的 64 位 Python（python.org，含 py 启动器）。
2. 在 i5 电脑拉取本分支（或合并后的 main），运行：`powershell -ExecutionPolicy Bypass -File dual-version\seedvc-download.ps1 -Bundle D:\seedvc-bundle -PythonVersion 3.11`（版本按第 1 步）。只下载公开的代码、软件包与模型，不上传任何东西。
3. 把整个 `D:\seedvc-bundle` 拷到应用电脑（U 盘或局域网均可，安装时会核对校验值）。

## 在应用电脑（i7-14700F + RTX 5070）上完成

1. 拉取分支，在 `sing-studio-batch-preview/` 运行 `npm ci`、`npm run build`、`npm run test:sites`。
2. 确认队列空闲后安装引擎：`powershell -ExecutionPolicy Bypass -File dual-version\seedvc-install.ps1 -Bundle <拷来的安装包> -Runtime <现有 local-test 目录>`。离线安装到 `<local-test>\seed-vc`，最后在显卡上自检；成功时显示显卡名称、torch 与 CUDA 版本。重装用 `-Replace`（旧目录改名保留）。
3. 回归：用 ace-env 的 Python 并设置 `SING_LOCAL_TEST` 后运行 `test_controls.py`、`test_instrumental.py`、`test_adjust.py`、`test_voice.py`、`test_backend.py`。
4. 实测（输出目录放在 Git 外）：
   - `dual-version\benchmark_adjust.py --runtime <local-test> --output <Git 外目录> --validation dual-version\VALIDATION-v1.4.0.json`（可加 `--source <纯伴奏 WAV>`）。
   - `dual-version\benchmark_voice.py --runtime <local-test> --output <Git 外目录> --source <本机有清楚人声的歌曲> --start <演唱段落秒数> --validation dual-version\VALIDATION-v1.4.0.json`。歌词用 `--lyrics-file <该段歌词>`，或把已安装应用的 `asr-paths.json` 复制到 `dual-version\` 由 Whisper 识别。两次运行：普通换声；Key -2 时目标音色取自未调整的原唱。检查显卡运行、换声后与原唱相似度提高、音高误差中位数 ≤ 50 音分、时长与命名。
5. 两项都通过后（`passed: true`），确认队列空闲，用 `dual-version\deploy-release.ps1 -AppRoot <现有 sing-studio 目录>` 更新应用；刷新界面后“换成原唱音色”可用。
6. 试听：对比任务目录中的 `generated.wav` 与换声结果，重点听音色是否接近原唱、咬字、高音、气声与伴奏中残留的生成人声。把实测用时与听感记录到本文件。

## 已知限制

- 换声只换音色；旋律、歌词与唱法来自 YuE2 新演唱。音域相差大的旋律即使整八度移动也可能吃力。
- 新歌声按单声道换声后居中放回；新伴奏里若残留生成人声（UVR 未分干净），可能听到原声的影子。原曲有大量和声或伴唱时，参考片段的音色可能混杂。
- Seed-VC 建议 Python 3.10；本次固定依赖面向应用电脑现有 Python（预计 3.11），尚未在真实安装中验证，以安装自检为准。
- 处理时间、显存占用与听感均未在应用电脑实测。

## Git

- 本分支 `feature/voice-swap-v1.4.0` 基于尚未合并的 `feature/key-tempo-v1.3.0`（PR #1），新 PR 以它为目标分支以便只审阅 v1.4.0 改动；先合并 PR #1，再合并本 PR，依次打 `v1.3.0`、`v1.4.0` 标签。禁止覆盖历史标签或 force push。
- Git 只包含维护源码、文档及脱敏验证报告；替代运行环境、安装包、模型、测试音频与作品均在 Git 外。

---

# Sing Studio v1.3.0 · 2026-10-16

## 本次交付

- 用户确认需求：变调与变速“两处都要”（生成前设置 + 已完成作品再处理），包含“独立调整”和“Slowed / Sped up”两种方式，并要可选混响。人声替换（voice swap）明确留到以后的单独更新，本次不做。
- 新模块 `dual-version/audio_adjust.py`：校验（半音 -12～+12 整数、速度 50～150 整数、混响 0～100 整数、`independent` / `linked`）、作品名称说明、FFmpeg 滤镜（有 Rubber Band 时用 `rubberband`，否则 `asetrate` + `aresample` + `atempo` 链），以及基于固定种子的立体声卷积混响。输出 48 kHz 双声道并精确对齐目标帧数。
- 生成前：设置 `adjust` 保存在统一设置与逐首 `track.json`（`null` 表示跟随统一）。任务配置记录 `source_duration`（原曲）与 `duration`（调整后）。`engine.run_pipeline` 截取原曲后先变调变速 `source.wav`（保持 PCM_16），之后分离、歌词、旋律和 YuE2 全部使用调整后的音频；混响在结果完成后、验证前加入。A/B 复用分析时增加比较 `adjust`。
- 已完成作品：`POST /api/jobs/<id>/adjust` 新建 `kind: adjust` 任务，来源是原作品声明的 WAV（同一路径保护）。由第二个 worker 线程处理，与 YuE2 队列互不等待；暂停设置对两者都生效；取消、重试、移除、恢复与原任务一致。新作品保留原作品的 `variant`，`group_id` 指向原作品所在组，`adjust_history` 记录逐次调整，用于名称。
- 界面：统一设置新增“变调与变速”面板；逐首“单独调整”中可选择跟随统一或单独设置；队列确认、队列行、作品卡片、详情和使用说明显示调整内容；作品卡片新增“变调 / 变速”按钮与对话框（名称与时长预览）。沿用森林绿 / 米白设计。
- 部署脚本 `deploy-release.ps1` 的文件清单已加入 `audio_adjust.py`；`.gitignore` 允许清单已加入新模块、测试、基准脚本和 v1.3.0 验证报告。版本源、package / lock、README、CHANGELOG 同步为 1.3.0。

## 本次验证（开发电脑，无 YuE2）

- 开发电脑不是应用电脑，没有安装 YuE2；使用临时替代运行环境（FFmpeg 8.1.1 含 Rubber Band、Python 3.11、占位模型文件）完成以下检查。
- 前端生产构建与 4 项打包测试通过；原有 8 项后端、6 项控制、6 项纯音乐回归通过；新增 `test_adjust.py` 8 项通过，其中用真实 FFmpeg 测量两种方法下的音高与时长、混响尾音，并验证管线顺序、A/B 共用、复用保护、超长拒绝、派生作品命名与下载、原作品不变、错误请求无副作用。
- 隔离浏览器实际操作：统一面板、Slowed 锁定变调、混响开关、刷新持久化；逐首覆盖；队列确认；派生作品对话框与完成后的作品卡片、WAV/MP3 下载名；无 console error。实际管线中原曲 30 秒经 Rubber Band 变为 33.333 秒、-2 半音；派生 Slowed 80% + Reverb 40 作品 4 秒完成，37.5 秒。生成阶段因开发电脑没有 YuE2 而失败，属预期。
- **未完成：应用电脑上的 YuE2 实测与主观试听。** `VALIDATION-v1.3.0.json` 因此为 `passed: false`，部署脚本会拒绝安装，直到在应用电脑运行下面的基准。

## 在应用电脑（i7-14700F + RTX 5070）上完成

1. 拉取分支或合并后的 main，在 `sing-studio-batch-preview/` 运行 `npm ci`、`npm run build`、`npm run test:sites`。
2. 用现有音乐环境的 Python 运行 `test_controls.py`、`test_instrumental.py`、`test_adjust.py`，以及设置 `SING_LOCAL_TEST` 后的 `test_backend.py`。
3. 运行实测并写入验证报告（输出目录放在 Git 外）：`dual-version/benchmark_adjust.py --runtime <本机 local-test> --output <Git 外目录> --validation dual-version/VALIDATION-v1.3.0.json`，可加 `--source <本机纯伴奏 WAV>` 改用参考旋律模式。全部通过后 `passed` 变为 true，并记录本机 FFmpeg 是否含 Rubber Band。
4. 确认队列空闲后使用 `dual-version/deploy-release.ps1 -AppRoot <现有 sing-studio 目录>` 更新；再试听变调、Slowed 与混响结果。
5. 若本机 FFmpeg 不含 Rubber Band，功能仍可用（atempo 方式），较大的变调变速音质会差一些；可考虑换用含 librubberband 的 FFmpeg 构建，需用户批准后再安装。

## Git

- 目标仓库 `https://github.com/sing929/singmusic`。本次在分支 `feature/key-tempo-v1.3.0` 提交并开 PR，由用户审阅后合并；合并后再打 `v1.3.0` 标签。禁止覆盖历史标签或 force push。
- Git 只包含维护源码、文档及脱敏验证报告；替代运行环境、测试音频与作品均在 Git 外。

---

# Sing Studio v1.2.0 · 2026-09-29

## 本次交付

- 用户明确确认逐首“有演唱 / 纯音乐”选择。纯音乐 A/B 跳过歌词识别及用于识别的人声提取，实际向本机 YuE2 发送 `[Instrumental]` 与无演唱要求；生成时不读取旧歌词缓存。
- 纯音乐禁用保留原唱、性别和音色，仍支持风格、力度、参考旋律与自由重创。已有歌词、演唱偏好保留，切回有演唱恢复。歌曲类型不会重写已排队任务或历史作品。
- 用户要求按“风格 + 原曲名”命名，例如 `Lo-fi Justin Bieber - Baby`。作品、队列和 WAV/MP3 下载已采用该格式；新生成的本机成品文件同样命名。历史文件与配置不重命名或改写。
- 文件名支持中文，替换系统禁用字符，过长本机文件名按 Windows 路径长度截短。不同任务保存在不同目录，A/B 按各自风格命名。歌词待自动识别筛选排除纯音乐，新增纯音乐筛选。
- 导入阶段仍可保存内嵌歌词，之后选为纯音乐时不使用；参考模式仍须识别旋律，这是乐谱分析而非歌词识别。
- 维护源码仍为 `dual-version/` 和 `sing-studio-batch-preview/src/`，版本源、package/lock、README、CHANGELOG 同步为 1.2.0；沿用森林绿 / 米白设计。
- 用户已授权更新原本机应用并提交、推送 GitHub。原本机应用已安装 v1.2.0，仍使用原 EXE / http://127.0.0.1:18765/；界面版本和逐首歌曲类型控件已核验，浏览器无 console error。

## 本次验证

- 前端生产构建与 4 项打包测试通过；8 项后端回归、6 项控制回归、6 项新增纯音乐/命名回归通过。新增测试包括识别与人声提取跳过、歌词保留、混合批次、缓存隔离、实际 WAV/MP3 导出、历史下载、范围请求和文件路径保护。
- 隔离浏览器实际操作通过：逐首切换、刷新保持、恢复原歌词、禁用演唱控件、纯音乐仍可自由重创、纯音乐/待识别筛选。UI 混合批次为 2 首 / 4 个任务：纯音乐 A/B 均为自由重创且歌词为空、auto_lyrics=false；演唱 A/B 保留原唱并保留歌词。无真实队列 worker。
- 本机 YuE2 三段 30 秒实测：Lo-fi 参考旋律 44.61 秒、Jazz 参考旋律 33.35 秒（复用安全分析）、Phonk 自由重创 31.74 秒。三组均约 29.999 秒、48 kHz 双声道、WAV/MP3 完整非静音，命名正确；请求与引擎回放确认纯音乐条件，未进行歌词识别或人声提取。
- 最初合成测试旋律触发当前 SheetSage 乐谱解码的 subbeat grid 错误；保留失败日志并改用已有本机纯伴奏样本完成参考模式验证。本次没有修改该转写器；参考模式仍可能受输入影响。
- **尚未完成用户主观试听验收，技术检查不代表无残留类人声或风格听感通过。** 本次未重跑整首原唱保留推理；其回归通过。
- 脱敏报告 `dual-version/VALIDATION-v1.2.0.json`；音频、请求、日志在 Git 忽略的 `.validation/instrumental-v1.2.0-local/`，初次合成样本失败记录在 `.validation/instrumental-v1.2.0/`；隔离 UI 在 `.validation/ui-v1.2.0/`。
- 可复现脚本 `dual-version/benchmark_instrumental.py --runtime <本机 runtime> --source <本机纯伴奏 WAV> --output <Git 外目录>`。未传 source 时使用合成旋律，但该旋律可能不被当前乐谱转写器接受。

## 安装与 Git

- 安装前再次确认原应用没有运行或等待任务，使用维护中的 `dual-version/deploy-release.ps1` 完成备份和更新；没有运行旧一次性脚本。备份为原应用 `backups/before-v1.2.0-20260929-205406`。
- 安装脚本检查 116 个原有歌曲/任务/设置元文件未变；另行核对 288 个元文件（含任务 config 和作品 result）全部哈希未变。原有成品 HEAD 下载返回 200 与新命名响应头。安装源文件与维护源码哈希一致。安装报告 `.validation/deployment-v1.2.0.json` 留在 Git 外。
- 目标仓库仍为 `https://github.com/sing929/singmusic`。本次分支 `codex/instrumental-v1.2.0`，提交基于远端 main 的 b41525f；目标为推送 main 并新增 v1.2.0 标签，提交与推送状态以最终回复为准。禁止覆盖历史标签或 force push。
- Git 只包含维护源码、文档及脱敏验证报告；禁止歌曲、歌词、模型、状态快照、设置、缓存与凭证。

---

# Sing Studio v1.1.1 · 2026-09-29

## 本次交付

- 用户批准新增 Phonk / Hardstyle / Hardtekk，A/B 统一与逐首设置均可选择，并使用各自独立风格描述请求本机 YuE2。
- 统一 A/B 均使用风格按钮；逐首 A/B 使用下拉菜单，提供“跟随统一”和自定义。自定义对话框确认保存后才生效，保留历史自定义值和逐首偏好。
- 明确要求仍替代预设乐器描述；输入小写同名风格也会匹配预设。未改变力度映射、旋律模式或原唱逻辑。
- 当前维护源码仍为 `dual-version/` 与 `sing-studio-batch-preview/src/`。版本源、package/lock、README 和 CHANGELOG 已同步为 1.1.1，沿用森林绿 / 米白设计。
- 用户授权更新原应用、提交并推送 GitHub；另行明确授权暂停等待任务，当前任务结束后验证和更新，再按原设置恢复队列。已等待当前任务结束，保留 9 项等待任务。
- 原本机应用已安装 v1.1.1，入口仍为原 EXE / http://127.0.0.1:18765/。备份位于原应用 backups/before-v1.1.1-20260929-175224；113 个既有元文件哈希未变，9 项等待任务的 config/job 哈希逐一未变。已恢复原先未暂停状态，并确认原等待任务继续生成；历史任务的配置版本不重写。安装详情在 Git 外 .validation/deployment-v1.1.1.json。

## 本次验证

- 前端生产构建与 4 项打包测试通过；8 项后端回归、6 项控制回归、12 种安装队列保护情况通过。
- 隔离浏览器实际操作通过：新预设按钮、逐首 B 下拉、自定义保存和刷新、历史 A/B 自定义保留、空名称禁存、取消不改值、恢复跟随统一。UI 入队结果为 A Phonk / B Hardtekk，逐首 B 覆盖统一 Hardstyle，实际 caption 含专属描述。
- 实际本机 YuE2：三种风格采用同源、同种子、同参考谱、75 力度（CFG 1.6）、fast 6 步。Phonk 43.69 秒、Hardstyle 32.28 秒、Hardtekk 32.83 秒；每个结果约 29.999 秒、48 kHz 双声道，WAV/MP3 完整，数据有限且非静音。请求与引擎回放均确认专属风格描述。
- 本次是三种风格的 30 秒技术验证，未重新验证整首自由重创或原唱保留。**尚未完成用户主观试听验收，不能把参数生效或音频完整当成风格听感通过。**
- 脱敏报告 `dual-version/VALIDATION-v1.1.1.json`。音频、请求及日志在 Git 忽略的 `.validation/styles-v1.1.1/`；隔离 UI 在 `.validation/ui-v1.1.1/`，无真实队列 worker。
- 更新使用维护中的 `dual-version/deploy-release.ps1`，新增显式 `-AllowPausedQueue` 仅允许“用户已授权、队列暂停、无运行任务”的更新；不改等待任务内容，安装后保持暂停供核验，再由本次操作恢复原设置。不要使用旧一次性升级脚本。

## Git

- 目标仓库仍为 `https://github.com/sing929/singmusic`；v1.1.0 已在远端，禁止覆盖或 force push。本次使用 codex/style-presets-v1.1.1 分支提交，推送远端 main 并新建 v1.1.1 标签；提交与推送状态以最终回复为准。
- Git 允许清单只加入维护源码、文档及脱敏验证报告；禁止歌曲、歌词、模型、状态快照、设置、缓存与凭证。

## v1.1.0 历史交接记录

用户要求：不同风格和音色变化要更明显、可以调大、按具体要求改编；引入清楚的版本管理；上传到 https://github.com/sing929/singmusic。

## 当前交付

- 已更新原本机 Sing Studio 应用，入口仍为原 EXE / `http://127.0.0.1:18765/`。不需要启动新的独立应用。
- 当前源代码是本仓库 `dual-version/` 后端与 `sing-studio-batch-preview/src/` 前端；不要使用早期 batch-integration 或一次性升级脚本覆盖。
- 版本源为 `dual-version/release.json`，当前 1.1.0。顶部可打开更新记录，每个新任务记录 app_version、controls_version、change_strength、melody_mode 和有效 CFG。旧作品显示“历史版本 · 未记录”。
- 修复旧强度固定 .65 且 YuE2 忽略它的问题。0–100 力度实际映射 CFG 1.0–1.8，默认 75。声音和风格共享模型的文本引导，不是独立声线转换器。
- 参考模式使用原 melody.abc；自由重创使用 cot=off / 空 ABC，忽略复用的参考谱但保留歌词、原时长。原唱保留强制参考模式，禁止改录音声音。
- 不再把旧默认“温暖电钢琴”附加给所有风格；仅迁移精确匹配的旧内置描述。明确要求覆盖预设乐器描述，支持统一、A/B 补充及逐首设置。用户自行保存的相同文字有 controls_version 标记后不会再次迁移。
- 不同版本仍独立生成语义 token 和音频；只复用安全的歌词、原始分析。新旧任务的配置互不重写。
- 界面和服务版本不一致时禁止开始批量任务，提示刷新 / 更新服务。
- 现有 4 首可见歌曲都带逐首独立设置，统一选择会被覆盖。新增醒目提示和“全部歌曲跟随统一”按钮，由用户决定清除这些覆盖项；不自动改变其逐首偏好，歌词保留。

## 验证与边界

- 原有 8 项后端 HTTP / 队列回归、新增 5 项控制回归、4 项前端打包测试通过。
- 浏览器实际操作：力度调至 100、刷新持久化、逐首 0 覆盖统一 100、逐首参考覆盖统一自由、A/B 独立要求入队、原唱禁用人声及旋律自由、版本记录。测试队列隔离且暂停，不会作为真实成品。
- 实际本机推理：同源、同种子、同参考谱的 30 秒强度 0 / 100 两组成功，回放参数分别确认 CFG 1.0 / 1.8，输出不同。
- 166.53 秒整首自由重创、力度 100、fast 6 步实测 206.41 秒；输出 166.52 秒双声道 48kHz WAV 和 MP3，未用循环或静音补长。
- 机器验证只说明参数生效、生成完成、音频完整有限；尚未通过用户主观听感验收。风格、性别、音色、歌词和旋律遵从不能承诺。高力度可更慢且可能失真；本次未重新测原唱保留整首。
- 脱敏报告：`dual-version/VALIDATION-v1.1.0.json`。本机原始结果、请求及歌词位于 `.validation/controls/`，已被 Git 忽略。
- 安装前备份在原应用 `backups/before-v1.1.0-20260929-120153`；更新后核验 70 个既有歌曲、任务、设置元文件哈希未变。本机安装报告 `.validation/deployment.json`。打开界面后设置可能按正常自动保存发生变化，不应把它误报为更新破坏。

## Git 与后续维护

- 用户明确授权上传维护源码到 sing929/singmusic。版本标签 `v1.1.0`；禁止 force push 和覆盖标签。
- 根 `.gitignore` 使用允许清单，仅源码、文档、脱敏验证报告进入 Git；不提交歌曲、歌词、模型、环境、数据快照或密钥。
- 每次更新同步 release.json、package/lock、CHANGELOG.md 和 handoff，验证后提交并创建新标签。
- 本仓库不包含已安装运行环境或模型。使用 README 的本机环境与更新说明；原 sources/ 仍只读。
