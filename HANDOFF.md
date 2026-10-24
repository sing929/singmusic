# Sing Studio v1.8.0 · 2026-10-24

## 本次交付

- 用户确认需求（2026-10-24，三轮问答 + 认可 Ditto 流程）：作品编辑完成后发行到音乐平台。歌曲都是用户本人原创，只发行用户本人声音的版本；平台为 **OFFstep 与 Ditto Music，每个 EP 只走其中一个**（用户最初想两个平台发同一个 EP，告知重复发行会被 Content ID / 平台判重下架后改为“不同 EP 各走一个”）；发行形式是同一首歌几个版本组成 EP / 专辑；“发布”只指提交到商店（不发社交媒体）；自动命名 + 自动生成封面、上传前由用户确认；做成 Sing Studio 里的“发行上架”页；歌曲在应用电脑上；每次由用户选发行日期。用户要求“全自动上传”：已说明两个平台都没有公开上传 API，只能在用户已登录的浏览器里由 Claude 按清单代填；登录、验证码、同意条款、付款与最后的 Submit 必须由用户本人完成。
- Ditto 规则（2026-10-24 读取帮助中心）：音频 WAV / MP3、至少 16-bit / 44.1 kHz 立体声、避免长段静音；封面 JPG、正方形、建议 3000 × 3000、RGB、≤ 10 MB，只能有艺人名与发行名称且须与资料一致；歌词每行一句、段落间空行、行尾无标点、重复段落写全；版本写在 Track mix version；Release Builder 第 2 步声明 AI（No / Partially / Entirely AI，Partially 时逐首 Track Credits 设 No / Partly / Fully AI，署名填本人而不是 AI 工具）；建议至少提前两周；ISRC / UPC 可由平台生成。Ditto 禁止声音克隆与模仿，不接受刷量式重复内容。OFFstep 的上传页面没有核对。
- `release_pack.py`（新）：命名（`title_case`、`song_title`、`style_version`、`version_text`）、`credits` / `release_ai`、`format_lyrics`、`release_type`、`apply`（字段校验）、`review`（问题 / 提醒 / 发行类型 / AI 声明）、`make_cover`（Pillow，Windows 字体 msyhbd / msyh，找不到时 Arial 或默认字体）、`export_wav`（FFmpeg `aresample=44100:dither_method=triangular`、`pcm_s16le`、去标签）、`checklist` 与 `build`（写入 `pack.new` 后整体替换）。
- `service.py`：发行存在 `data/releases/<id>/release.json`；`root_jobs`（作品来自哪些 YuE2 生成：变调变速 / DJ 版追溯父作品，串烧与组曲取全部部分）、`work_lyrics`、`release_entries`、`public_release`；新建 / 加入作品 / 修改（曲目只能重排、改名、移出，新作品走 `tracks` 路由）/ 后台生成（令牌防止旧的生成覆盖新状态）/ 打开文件夹（`os.startfile`）/ 已提交 / 重新编辑 / 删除 / 恢复；封面预览按 `cover_key` 缓存为 `cover-<key>.jpg`；`send_file` 支持 `image/jpeg`；启动时把中断的生成标为失败。
- 界面 `App.jsx` / `styles.css`：侧栏“发行上架”（未提交发行数）、发行标签列表、资料表单（平台、名称、艺人、署名真名、厂牌、主 / 副风格带建议列表、语言、版权、日期、UPC）、封面预览与“换一张封面”、曲目（曲名、版本、露骨歌词、上移 / 下移 / 移出、商店显示名、来源作品、AI 声明标签、歌词来源）、问题与提醒、发行包（状态、路径、文件与检查结果、生成 / 打开文件夹 / 已在平台提交 / 重新编辑 / 删除）、备注；“加入作品”对话框（按歌曲分组，已在其他发行的作品不可选）与“已提交”对话框（可填 UPC）。改动即时显示、停顿 0.45 秒后保存，生成前先保存。
- `deploy-release.ps1` 文件清单加入 `release_pack.py`；`.gitignore` 允许 `release_pack.py`、`test_release.py`、`VALIDATION-v1.8.0.json`；版本源、package / lock、README、CHANGELOG 同步为 1.8.0；`sing-studio-batch-preview/AGENTS.md` 记录本次确认的需求。

- n8n 自动上传（同日第二轮问答）：用户比较 ComfyUI 与 n8n 后选 n8n，平台仍用 Ditto / OFFstep，自动化做到 Submit 前、由用户本人点 Submit；Sing Studio 自动把编辑好的歌曲按 EP 名放进文件夹，n8n 读这个文件夹上传；只用 n8n 表单（不发 Telegram / 邮件）；n8n 装在应用电脑。说明过：Ditto / OFFstep 没有上传 API，n8n 只能驱动网页；完全无人值守需要有 API 的平台（如 LabelGrid，约 119 美元 / 月起，未核实）。
- EP 导出文件夹：`service.py` 的 `release_settings` / `save_release_settings`（存 `data/release-settings.json`），`build_release` 生成后调用 `release_pack.export`，失败只记 `export_error` 不影响发行包。界面在“发行上架”顶部加导出文件夹输入框，发行包区域显示 EP 文件夹路径。
- `automation/`（新）：见其中 README 的安装步骤。设计要点：n8n 2.x 默认禁用 Execute Command，须设 `NODES_EXCLUDE="[]"`；助手脚本每次输出一行 JSON、正常结束（不用 `process.exit`，否则 Windows 上 Node 会以 `UV_HANDLE_CLOSING` 断言崩溃，n8n 报错）；上传用的 Chrome 用 `--remote-debugging-port=9333 --user-data-dir=%LOCALAPPDATA%\SingStudio\upload-chrome` 独立启动，脚本只连接、结束时断开，表单保持打开；附件用 CDP `DOM.setFileInputFiles` 交路径（Playwright 的 `setInputFiles` 经 CDP 连接时超过 50 MB 会拒绝）。

## 本次验证（开发电脑 Xeon + GTX 1070，无 YuE2 / Seed-VC）

- 前端生产构建与 4 项打包测试通过。后端 86 项回归通过：原有 78 项与新增 `test_release.py` 8 项（真实 FFmpeg 导出、Pillow 封面、EP 导出文件夹）。`automation` 3 项 Node 测试通过（无界面 Chrome）。
- n8n 端到端（n8n 2.44.6、Node 24，临时安装在开发电脑，`import:workflow` + `publish:workflow`，未建账户）：替代安装设置导出文件夹并重新生成测试发行 → 表单第 1 页 → 第 2 页列出 “夏天的风 Summer Wind · Ditto Music · 3 首 · 2026-11-20” → 助手在无界面的上传 Chrome 里用 Ditto 映射填写模拟上传页（22 项完成，模拟页没有的 5 项列为没找到，含一个 70.6 MB 的 WAV）→ 第 3 页显示结果 → 选“已点 Submit”并填 UPC → 结束页“已记录提交”，EP 文件夹有 `submitted.json`，替代安装的发行变为已提交、UPC 已存。另走一遍“先保留”分支。助手结束后 Chrome 里的表单仍打开、已填写、状态仍为未提交。测试用的 n8n、Chrome 与配置已清理；替代安装里的测试发行现为“已提交”。仍须设 `PYTHONUTF8=1`。
- 隔离浏览器实际操作（替代安装 `rt-voice`，队列暂停）：新建发行（Ditto Music）→ 加入作品对话框按歌曲分组 → 加入 3 个作品，自动名称为 Demo Song (Lo-fi Version) / (Lo-fi Version, Slowed) / (Jazz Version)，显示 AI 声明标签；填写前列出缺少的资料，输入发行名称与艺人名后封面重画；生成发行包（3 个 44.1 kHz / 16-bit WAV、封面、metadata、上传清单），显示文件、峰值与试听起点；375 px 宽单列、无横向滚动；无控制台错误。替代安装的 `data/releases/` 里留有这个测试发行。
- **未完成：** 用发行包在 Ditto / OFFstep 网站真正上传一次（Release Builder 实际字段顺序、OFFstep 表单；`maps/ditto.json` 仍是 `verified: false`）；应用电脑上安装 n8n 并导入流程；应用电脑上的真实生成与试听。`VALIDATION-v1.8.0.json` 为 `passed: false`，所需基准仍为 `adjust`、`voice`、`variety`。

## 在应用电脑（i7-14700F + RTX 5070）上完成

1. 与 v1.7.0 相同：Seed-VC（如未安装）、`npm ci` / `npm run build` / `npm run test:sites`、全部 13 个 `test_*.py`（应用电脑的 ace-env 须有 Pillow，`test_release.py` 会用到）。
2. 三项实测都加 `--validation dual-version\VALIDATION-v1.8.0.json`，都通过后确认队列空闲，用 `deploy-release.ps1` 更新应用。
3. n8n：按 `automation/README.md` 安装依赖、登录上传用的 Chrome、安装 n8n（`NODES_EXCLUDE="[]"`）、导入并激活 `automation/n8n/sing-studio-upload.json`，在“发行上架”设置 EP 导出文件夹。
4. 第一次发行：登录 Ditto 后让 Claude 在上传用的 Chrome 里看一次 Release Builder，按实际页面修好 `automation/maps/ditto.json` 并设 `verified: true`；之后用 n8n 表单上传，用户本人核对并点 Submit。把实际字段顺序、被平台退回的原因记到本文件。

## 已知限制

- 平台没有公开上传 API，上传须在网站上完成；规则按 2026-10-24 的 Ditto 帮助中心，平台改版后清单可能需要更新。OFFstep 的字段没有核对。
- 版本说明（如 `Cinematic Orchestra Version`、`Lo-fi Version, Slowed`）是否被商店接受以平台审核为准；Ditto 只举例了 Remix、Live、Demo、Acoustic、Instrumental 等。
- 封面是程序生成的抽象图案；如想用自己的图片，目前需在上传时替换 `cover.jpg`（只能写发行名称与艺人名）。
- 发行包在应用电脑本机，“打开文件夹”只在 Windows 上可用。
- n8n 提示“在容器外运行已弃用，将来版本须用官方 Docker 镜像”：放进 Docker 后 Execute Command 无法启动本机 Chrome、读不到本机路径。目前用 npm 安装（已测 2.44.6）并固定版本；将来若必须用 Docker，需要把助手改成本机的小服务，由 n8n 通过 HTTP 调用。
- 网站改版、两步验证或验证码出现时填写会在那一步停下，其余按《上传清单》手动完成。

## Git

- 本分支 `feature/release-tab-v1.8.0` 基于 main（v1.7.0 合并提交 `394be2e`），以 main 为目标开 PR；合并后打 `v1.8.0` 标签。不得强制推送或覆盖标签。

---

# Sing Studio v1.7.0 · 2026-10-22

## 本次交付

- 用户确认需求（2026-10-22，两轮问答）：要“一次下载全部类型的风格”——确认含义是**一次生成全部风格**（不是打包下载；ZIP 下载按钮用户明确不要）；做成“全部风格”开关（统一 + 逐首），全部风格时自动不生成变速版本。说唱“缺少”的原因是用户本机应用仍是 v1.5.0（v1.6.0 已合并但未部署）；另外要更多说唱风格：选了 Rap、Melodic Rap、Drill、Emo Rap（没选中文 / 粤语说唱与马来说唱）。发布方式：v1.7.0 分支 + PR，只推送 Git，本次不做应用电脑实测与部署。
- 风格目录 `styles.json`：说唱组改为 Rap、Hip-hop、Trap、Boom Bap、Melodic Rap、Drill、Emo Rap、Phonk，新增 4 种都带“全新演绎 + 自由旋律”智能默认，共 55 种。默认 20 种的第 14、17、20 项改为 Rap、Melodic Rap、Drill。
- `generation_controls.py`：`ALL_STYLES`（按组顺序）、`MAX_SONG_JOBS`（全部风格 + 组曲 = 58，用于种子上限）、`version_letter`（A–Z 后接 AA、AB …）。`LETTERS` / `MAX_VERSIONS` 不变（换声开关仍是 A–T 20 个）。
- `voice_swap.for_all_styles`：全部风格的歌曲只有全部 20 个版本开关都打开时才换声（界面显示一个开关，同时打开或关闭 20 个）。
- `service.py`：设置 `all_styles`（布尔，默认 false，旧值无效回退）、逐首 `track.all_styles`（布尔或 null 跟随）；`expand_item` 在 `all_styles` 时按 `ALL_STYLES` 展开（忽略请求里的 variants）、`speed_variants=False`、换声用 `for_all_styles`、不受 20 版本与“组曲后超过 20”的限制；版本字母改用 `version_letter`。
- 界面 `App.jsx` / `styles.css`：统一设置“全部风格”开关、分组风格清单与用时估算（开启时版本数停用，各版本风格、按组填充与各版本补充要求隐藏）；换声面板单开关（`VoiceControls` 的 `every`）；变速面板注明不生成；逐首“全部风格”下拉，开启时版本数停用并显示 A–BC 说明；底部、确认对话框（“全部风格”一行与用时提醒）、使用说明；“我的作品”版本排序改为先按字母长度（A … Z、AA …）。顺带把“每批最多 100 首”提示里的队列容量 500 改为 2000。
- `benchmark_variety.py`：新增 4 次有词说唱生成（Rap、Melodic Rap、Drill、Emo Rap，占位歌词、自由旋律，检查歌词与 `cot=off` 送到引擎），约多 4 分钟。
- 版本源、package / lock、README、CHANGELOG 同步为 1.7.0；`.gitignore` 允许清单加入 `test_all_styles.py` 与 `VALIDATION-v1.7.0.json`。`deploy-release.ps1` 文件清单无需变化（没有新模块）。

## 本次验证（开发电脑 Xeon + GTX 1070，无 YuE2 / Seed-VC）

- 前端生产构建与 4 项打包测试通过。后端 78 项回归通过：原有 70 项（`test_versions.py` 的第 20 个默认风格改为 Drill）与新增 `test_all_styles.py` 8 项。仍须设 `PYTHONUTF8=1`。
- 隔离浏览器实际操作（替代安装 `rt-voice`，队列暂停）：开启“全部风格”后服务端保存且刷新后保留；55 种风格分组清单（说唱 8 种）；换声单开关；确认对话框“1 首 → 58 个完整版本 + 1 首管弦组曲”；入队 58 个任务（A R&B … BC Music Box，组曲 BD–BF），Rap / Melodic Rap / Drill / Emo Rap 为 W / AA / AB / AC、全新演绎 + 自由旋律，变速版本 0 个，测试任务已移除；逐首改为“按版本数”后恢复该曲 2 个版本；375 px 宽无横向滚动；无控制台错误；替代安装的设置已恢复。
- **未完成：应用电脑上的真实生成与试听**（尤其新说唱风格是否真的“说唱”而不是唱，以及全部风格一整批的实际用时）。`VALIDATION-v1.7.0.json` 为 `passed: false`，所需基准仍为 `adjust`、`voice`、`variety`，三项都通过后才能用 `deploy-release.ps1` 部署。v1.3.0–v1.6.0 的应用电脑实测同样尚未完成；部署 v1.7.0 会一并带上 v1.6.0 的全部内容。

## 在应用电脑（i7-14700F + RTX 5070）上完成

1. 先按 v1.4.0 一节完成 Seed-VC 的下载与安装（如尚未完成）。
2. 拉取合并后的 main（或本分支），在 `sing-studio-batch-preview/` 运行 `npm ci`、`npm run build`、`npm run test:sites`。
3. 回归：用 ace-env 的 Python、设置 `SING_LOCAL_TEST` 与 `PYTHONUTF8=1` 后运行全部 12 个 `test_*.py`。
4. 实测（输出目录放在 Git 外）：`benchmark_adjust.py`、`benchmark_voice.py`（参数同 v1.4.0 一节）与 `benchmark_variety.py`（参数同 v1.6.0 一节，约 20 分钟），都加 `--validation dual-version\VALIDATION-v1.7.0.json`。
5. 三项都通过后（`passed: true`），确认队列空闲，用 `dual-version\deploy-release.ps1 -AppRoot <现有 sing-studio 目录>` 更新应用。
6. 试听：4 种新说唱风格的有词结果（是否说唱原歌词、节奏感），再选一首短歌开启“全部风格”跑一整批，记录实际用时与听感到本文件。

## 已知限制

- 全部风格每首 55 个完整生成，按约 3.5 分钟生成一个 3 分钟版本估算约 3 小时；队列容量 2000 约容纳 36 首。全部风格只用统一要求，没有各版本补充要求，换声只能全部开或全部关。
- 说唱风格仍是给 YuE2 的文字要求，模型不一定真的说唱，须试听确认。

## Git

- 本分支 `feature/all-styles-rap-v1.7.0` 基于 main（v1.6.0 合并提交 `6b3ac11`），以 main 为目标开 PR；合并后打 `v1.7.0` 标签。不得强制推送或覆盖标签。

---

# Sing Studio v1.6.0 · 2026-10-21

## 本次交付

- 用户确认需求（2026-10-21，三轮问答）：要更多音乐变化——童声指“小朋友在唱”，用“YuE2 童声要求 + 本机变声处理”实现；说唱用原歌词；DJ 四项都要（亚洲 DJ 版、Club EDM、DJ 串烧、DJ 效果）；管弦与经典四项都要（电影管弦、古典、经典老歌、组曲按“分段切换”）；新增演唱者合唱团、男女对唱、歌剧；风格组亚洲、乐队、世界与拉丁、轻松与趣味全部加入；风格智能默认；一次发布全部功能；每首最多 20 个版本。另一项是我替用户定的：童声不用于保留原唱——`AGENTS.md` 规定原唱模式不改变录音的人声。
- 风格目录 `dual-version/styles.json`（界面与后端共用）：9 组 51 种风格，每种有英文编曲描述、中文名与可选智能默认；7 种演唱者（含 kid 的 +2 半音默认）；组曲三部分（风格、Suite I/II/III 标签、乐章描述）。`generation_controls.py` 从中读取 `STYLE_TAGS`、`GENDERS`、`DEFAULT_STYLES`（20 种，前 10 种与 v1.5.0 相同）、`SUITE`；新增 `style_info`、`apply_smart_defaults`、`validate_suite`；`MAX_VERSIONS` 20、`LETTERS` A–T；`text_list` 与 `voice_swap.validate` 把 v1.5.0 的 10 项列表补齐到 20 项。
- `service.py`：批次逐首拆成 `expand_item`——智能默认按版本风格应用（`smart_defaults` 开关、`explicit` 逐首字段，记录在任务 `smart_defaults`），组曲另加三个部分（参考旋律、无换声、无变速、`suite_part`，名称附 Suite I/II/III）；请求里的 `smart_applied` / `suite_part` 会被丢弃。原曲分析只在模式与变调变速相同的版本间复用。演唱者 kid / duet / choir 不换声；kid 任务带 `kid: true`，名称附 `Kid Voice`。新增 `finished_work`、`queue_derived`（派生任务通用，名称累加父任务的所有说明）、`create_dj`、`create_mix`、`queue_suite`（三部分都完成时只派生一次，`data/suites/<id>.json` 标记）；效果队列处理 adjust / dj / mix / suite；队列容量 `QUEUE_LIMIT` 2000；设置新增 `smart_defaults`、`suite`，逐首新增 `suite`；新路由 `POST /api/jobs/<id>/dj`、`POST /api/mix`。
- `engine.py`：kid 任务生成后 `separate-generated` → `kid_voice.convert`；`run_effect` 处理 dj / mix / suite；`validate` 分块读取音频（长串烧不占满内存），派生作品长度以实际渲染为准，MP3 超时随长度增加；结果说明附童声与各派生说明。
- 新模块 `kid_voice.py`（八度判断按 pYIN 中位音高 < A3；Rubber Band 只用于升八度且 `formant=shifted`，共振峰全部由频谱包络变形决定——实测 FFmpeg 8.1.1 的 `formant=preserved` 向上只部分有效、向下无效）、`dj_tools.py`（节拍与 Krumhansl 调性、Build-up 与 Drop、滤波渐入、回声结尾、自动排序、统一速度的串烧与低音交换、软限幅流式写出）、`suite.py`（安静点、起音包络对齐且在重复节拍中取最小位移、等功率交叉淡化）。
- 界面 `App.jsx` / `styles.css`：分组下拉风格选择与智能默认提示、按风格组填充 / 随机、智能默认开关、演唱者（含说明）、童声等不换声提示、管弦组曲面板与逐首开关、20 个版本；“我的作品”新增“DJ 效果”对话框、“DJ 串烧”勾选条与对话框（排序、自动排序、过渡拍数、名称）；作品卡片、队列行、详情与确认对话框显示新信息。修正：新对话框的复选框在状态更新函数里读取 `e.target` 导致点击不生效，改为先取值。
- 版本源、package / lock、README、CHANGELOG 同步为 1.6.0；`deploy-release.ps1` 文件清单加入 `kid_voice.py`、`dj_tools.py`、`suite.py`、`styles.json`；`.gitignore` 允许清单加入新模块、测试、`benchmark_variety.py` 与 `VALIDATION-v1.6.0.json`。

## 本次验证（开发电脑 Xeon + GTX 1070，无 YuE2 / Seed-VC）

- 前端生产构建与 4 项打包测试通过。后端 70 项回归通过：原有 48 项（`test_versions.py` 改为 20 个版本并新增旧设置补齐，共 5 项）与新增 `test_styles.py` 7、`test_kid_voice.py` 4、`test_dj.py` 6、`test_suite.py` 4。中文 Windows 控制台须设 `PYTHONUTF8=1`，否则 `test_voice.py` 的自检按 GBK 读取中文输出而报错（v1.6.0 之前就存在的环境问题）。
- 真实 FFmpeg（含 Rubber Band）处理合成音频：童声共振峰上移且音高不变、低声部升八度；Build-up 的 Drop 落在能量跳升处、前半拍静音、其余不变；回声结尾在结尾前 8 拍的拍点截断并延续 8 拍；三首 120 / 126 / 62 BPM 的串烧统一到同一速度、过渡正好 8 拍、音量拉平、无削波；组曲在安静处切换并对齐 150 ms 的偏移。
- 隔离浏览器实际操作（替代安装 `rt-voice`，队列暂停）：20 个版本、按“亚洲 DJ 版”填充并保存 20 种不同风格（v1.5.0 的 10 项设置自动补齐）；智能默认提示；童声提示与“童声不换声”；组曲开关与三部分；确认对话框“5 个完整版本 + 1 首管弦组曲”；入队后 A/B 为保留原唱 + 速度 105% / 115%，C–E 为童声组曲部分且 D、E 复用 C 的分析（测试任务已移除）；DJ 效果三项勾选与名称预览、手动渲染为 29.7 秒新作品；串烧勾选、排序、8 拍、中文名称，手动渲染为 79.4 秒并单独成组；375 px 宽无横向滚动；无控制台错误。
- **未完成：应用电脑上的真实生成、童声处理与组曲的实测、主观试听。** `VALIDATION-v1.6.0.json` 为 `passed: false`，所需基准为 `adjust`、`voice` 与新增的 `variety`，三项都通过后才能用 `deploy-release.ps1` 部署。v1.3.0–v1.5.0 的应用电脑实测同样尚未完成。

## 在应用电脑（i7-14700F + RTX 5070）上完成

1. 先按 v1.4.0 一节完成 Seed-VC 的下载与安装（如尚未完成）。
2. 拉取本分支（或合并后的 main），在 `sing-studio-batch-preview/` 运行 `npm ci`、`npm run build`、`npm run test:sites`。
3. 回归：用 ace-env 的 Python、设置 `SING_LOCAL_TEST` 与 `PYTHONUTF8=1` 后运行全部 11 个 `test_*.py`。
4. 实测（输出目录放在 Git 外）：`benchmark_adjust.py`、`benchmark_voice.py`（参数同 v1.4.0 一节），以及 `dual-version\benchmark_variety.py --runtime <local-test> --output <Git 外目录> --source <本机歌曲或纯伴奏> [--start <秒>] --validation dual-version\VALIDATION-v1.6.0.json`。variety 用同一段 30 秒原曲：8 种代表风格（每组一种）、童声 / 对唱 / 合唱 / 歌剧（自由重创、占位歌词）、三个组曲部分与组曲、DJ 效果与两首串烧；约 15 分钟。
5. 三项都通过后（`passed: true`），确认队列空闲，用 `dual-version\deploy-release.ps1 -AppRoot <现有 sing-studio 目录>` 更新应用。
6. 试听：新风格是否像所选风格（尤其亚洲 DJ 版与说唱）、童声 / 对唱 / 合唱 / 歌剧是否被遵从、童声处理是否自然（`kid-voice.json` 的八度与共振峰，对比 `generated.wav`）、组曲转换点、DJ 效果的拍点、串烧过渡。把听感与用时记录到本文件。

## 已知限制

- 风格、演唱者与说唱是给 YuE2 的文字要求，模型不一定遵从；童声、对唱、合唱尤其需要试听确认。智能默认的速度是固定比例，不按原曲 BPM 计算。
- 童声处理只作用于新生成的人声：UVR 未分干净的伴奏里若残留生成人声，会听到未处理的成人声影子；大幅升八度可能有颗粒感。
- 节拍与调性由 librosa 估计；节奏不明显或变速的歌曲，Drop 位置、回声拍点、串烧过渡和组曲对齐可能不准。速度差超过约 16% 的歌曲在串烧中不强行对齐。
- 每首 20 个版本 + 变速版本最多 80 个作品；按之前约 3.5 分钟生成一个 3 分钟版本估算，20 个版本需要一个多小时。队列容量 2000 包含派生任务。

## Git

- 本分支 `feature/music-variety-v1.6.0` 基于 main（v1.5.0，`fc15ab8`），以 main 为目标开 PR；合并后打 `v1.6.0` 标签。不得强制推送或覆盖标签。
- Git 只包含维护源码、文档及脱敏验证报告；替代运行环境、测试音频与作品均在 Git 外。

---

# Sing Studio v1.5.0 · 2026-10-19

## 本次交付

- 用户确认需求（2026-10-19）：在每首 1–10 个版本之上，每个版本完成后再自动得到 3 个变速版本，一次最多 40 个作品。三个预设固定为 Speed Up（速度 1.12 ×、升 1 个半音）、Slowed（0.85 ×、降 2 个半音）、Super Slowed（0.75 ×、降 3 个半音）；速度与音高**分别设定**（不是唱片式音高随速度），**不加混响**；由已完成的版本在本机后处理，不用 YuE2 重新生成；适用于全新演绎、保留原唱与纯音乐；统一设置一个开关，默认开启；名称用英文预设名；基于 PR #2（v1.4.0）另开分支与 PR。
- `dual-version/audio_adjust.py`：新增 `PRESETS` 与 `preset(name)`；`validate()` 接受可选 `preset` 字段，且只在调整与预设完全一致时保留（名称不会与实际声音不符）；`label()` 对带预设的调整返回预设名，作品名因此为 `Lo-fi 歌名 (Speed Up)`，换声作品为 `(Original Voice, Speed Up)`，再变调变速时继续累加。
- `dual-version/service.py`：设置新增 `speed_variants`（布尔，默认 `true`，旧值非布尔时回退为开启；`/api/settings` 校验）；逐首 `track.speed_variants`（布尔，空值跟随统一，`/api/tracks` 校验，旧值无效时按跟随处理）；`prepare_job` 把每个任务的 `speed_variants` 写入 config 与 job（排队后改设置不影响）；`create_adjustment` 拆出 `queue_adjustment(identifier, adjust, auto)`（需持有 LOCK）；新增 `queue_speed_variants(meta)`：父任务完成且已保存后按 `PRESETS` 顺序各派生一个 `kind: adjust` 任务（`auto: true`、同 `group_id` / `variant`），派生的 id 记入父任务 `speed_variant_ids`，失败记入 `speed_variant_errors`，只派生一次（重启恢复或重复调用不会重复）；手动变调变速另存的作品（`kind: adjust`）不再派生。`finish_attempt` 成功时先保存完成状态再派生。
- 界面 `App.jsx` / `styles.css`：统一设置新增“加速与慢速版本”面板（开关、三张预设卡、说明）；设置随其他统一设置自动保存并在刷新后恢复；批次每项带 `speed_variants`（逐首优先于统一）；逐首“单独调整”新增“加速与慢速版本”下拉（跟随统一 / 生成 3 个变速版本 / 只生成原速版本），计数按每首实际设置累计；底部“共 N 个版本，另加 3N 个变速版本”、确认对话框“N 个完整版本 + 3N 个变速版本”、入队提示作品总数；作品卡片对自动派生作品显示预设名与版本字母及“自动变速版本 · 本机处理”。
- 版本源、package / lock、README、CHANGELOG 同步为 1.5.0；`.gitignore` 允许清单加入 `test_speed_variants.py` 与 `VALIDATION-v1.5.0.json`。`deploy-release.ps1` 文件清单无需变化（没有新模块）。

## 本次验证（开发电脑 Xeon + GTX 1070，无 YuE2 / Seed-VC）

- 前端生产构建与 4 项打包测试通过。
- 后端：新增 `test_speed_variants.py` 7 项通过（含逐首开关）；原有 8 项 HTTP / 队列、6 项控制、6 项纯音乐、8 项变调变速、4 项多版本、9 项换声回归通过（共 48 项）。其中一项用真实 FFmpeg 渲染 Speed Up：时长为 1/1.12、音高升 1 个半音、文件名带 `(Speed Up)`、父作品 WAV 不变。
- 隔离浏览器实际操作（替代安装、暂停的队列）：面板与三张预设卡显示、默认开启；关闭后标题、底部计数与保存的设置同步改变，重新开启恢复；确认对话框显示“1 首 → 2 个完整版本 + 6 个变速版本”；逐首改为“只生成原速版本”后底部与对话框的变速计数随之减少，刷新后保留；对一个已完成的换声作品按工作线程方式派生并渲染三个变速版本后，“我的作品”在同一首歌下显示 `Speed Up · A` / `Slowed · A` / `Super Slowed · A` 卡片，名称为 `Lo-fi Demo Song (Original Voice, Speed Up)` 等；无应用控制台错误。
- **未完成：应用电脑上的真实生成后自动派生、三种变速的主观试听、处理用时。** `VALIDATION-v1.5.0.json` 为 `passed: false`，所需基准仍是 `adjust` 与 `voice`（变速版本走同一条变调变速管线），两项通过后才能用 `deploy-release.ps1` 部署。v1.4.0 的 Seed-VC 安装与实测同样尚未完成，见下一节。

## 在应用电脑（i7-14700F + RTX 5070）上完成

1. 先按 v1.4.0 一节完成 Seed-VC 的下载与安装（如尚未完成）。
2. 拉取本分支（或合并后的 main），在 `sing-studio-batch-preview/` 运行 `npm ci`、`npm run build`、`npm run test:sites`。
3. 回归：用 ace-env 的 Python 并设置 `SING_LOCAL_TEST` 后运行 `test_controls.py`、`test_instrumental.py`、`test_adjust.py`、`test_versions.py`、`test_voice.py`、`test_speed_variants.py`、`test_backend.py`。
4. 实测：`benchmark_adjust.py` 与 `benchmark_voice.py` 均加 `--validation dual-version\VALIDATION-v1.5.0.json`（参数同 v1.4.0 一节）。
5. 两项都通过后（`passed: true`），确认队列空闲，用 `dual-version\deploy-release.ps1 -AppRoot <现有 sing-studio 目录>` 更新应用。
6. 试听：生成一首 2–3 个版本的歌，等每个版本完成后在“我的作品”对比原版本与 Speed Up / Slowed / Super Slowed：重点听变速后的音色是否自然、人声是否发虚、Rubber Band 与 atempo 的差别（`adjust.json` 的 `method`）。把听感与三个派生任务的实际用时记录到本文件。

## 已知限制

- 变速版本只改速度与音高，不改旋律与编曲；大幅变速（0.75 ×）在 atempo 方式下可能有颗粒感，Rubber Band 更好。
- 队列容量 500 包含派生任务；队列满时派生失败只记录在父任务的 `speed_variant_errors`，不自动补派生。
- 派生任务使用“效果”队列，CPU 处理；与 YuE2 生成并行时会占用 CPU，实际影响未在应用电脑测量。

## Git

- 本分支 `feature/speed-variants-v1.5.0` 基于尚未合并的 `feature/voice-swap-v1.4.0`（PR #2），后者基于 `feature/key-tempo-v1.3.0`（PR #1）。新 PR 以 PR #2 的分支为目标；合并顺序 PR #1 → `v1.3.0`，PR #2 → `v1.4.0`，本 PR → `v1.5.0`。不得强制推送或覆盖标签。
- Git 只包含维护源码、文档及脱敏验证报告；替代运行环境、测试音频与作品均在 Git 外。

# Sing Studio v1.4.0 · 2026-10-16

## 本次交付

- 用户确认需求（2026-10-16）：换声的目标音色是**原曲演唱者**，即从导入的原曲用 UVR 分离出的原唱，不另给音色片段、不用 RVC 模型、不训练。只在**生成前**设置（统一 + 逐首，每个版本分别选择；合并 v1.3.0 的 1–10 个版本后为 A–J 每版一个开关），已完成作品不提供换声再处理。换声引擎在 RTX 5070 应用电脑运行；用户在另一台联网的 i5 电脑下载安装包后拷贝到应用电脑。开发电脑不得下载或安装引擎。只对有权使用的声音换声。
- 引擎选择：Seed-VC v1 歌唱模型（`DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth`，44.1 kHz，F0 条件，零样本），代码固定在 `Plachtaa/seed-vc@51383efd921027683c89e5348211d93ff12ac2a8`（GPL-3.0，以独立程序调用）。开发前已在 GitHub 阅读该提交的 `inference.py`、`hf_utils.py`、配置与依赖，未下载或运行。
- 新模块 `dual-version/voice_swap.py`（应用 Python 中运行）：设置校验（统一 / 逐首 `voice_swap = {a, b, octave}`，任务 `voice = {target: original, octave}`）、`available()`（以 `local-test/seed-vc/install.json` 自检通过为准）、乐句切分（10 ms 能量门限，长于 30 秒在最安静处切开）、原唱参考片段（去掉停顿，取最稳定的 20 秒）、pYIN 中位音高与整八度规则（相差 ≥ 7 个半音时移整八度，限 ±12）、调用换声、按原位置放回（10 ms 淡入淡出）、按原人声响度匹配增益、与新伴奏重新混音并防削波，写 `voice-swap.json`。
- 新脚本 `dual-version/seedvc_runner.py`（在 Seed-VC 独立环境中运行）：切换到 Seed-VC 目录导入其 `inference`，模型只加载一次后逐乐句调用其 `main()`；不是 CUDA 时拒绝（测试可用 `SING_VOICE_ALLOW_CPU=1`）；用引擎自带 CAM++ 与 RMVPE 计算换声前后与原唱的音色相似度和音高误差；`--self-test` 供安装脚本检查显卡。PyTorch 2.6 起 `torch.load` 默认 `weights_only=True`，固定版本的检查点按旧方式加载（`TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`，只针对固定的官方文件）。
- 管线（`engine.run_pipeline`）：有换声时先检查引擎（未安装直接失败，不耗 GPU）；原曲变调变速时先保存 `source-unshifted.wav`，另行分离到 `reference-stems`，否则使用（或补做）`original-stems`；生成前做参考片段。生成后 `separate-generated` → `voice_swap.convert` → 混响 → 验证。`generated.wav` 保留供对比。各版本复用分析时同时复制 `reference-stems`。结果说明附换声提示。
- 服务（`service.py`）：设置与逐首保存 `voice_swap = {versions: [10 个开关], octave}`；批次按版本字母映射为任务 `voice`；保留原唱与纯音乐忽略换声；请求换声而引擎未安装时整批拒绝（不创建任务）；`/api/state` 的 `engines.voice`；作品名附 `Original Voice`（派生的变调变速作品继承）；完成作品返回 `voice_info` 摘要。
- 界面：统一设置新增“换成原唱音色”面板（按版本数量显示 A–J 开关、全部换声 / 全部不换声、音域、未安装提示与版权提醒）；逐首“单独调整”可跟随统一或单独设置；队列确认显示换声行，引擎未安装时显示警告并禁用确认；队列步骤、队列行、作品卡片、详情、使用说明与更新记录。沿用森林绿 / 米白。
- 安装：`seedvc-requirements.txt`（torch / torchaudio 2.7.1+cu128 等固定版本）、`seedvc-download.ps1`（联网电脑：`pip wheel` 生成全部 wheel——其中 `julius`、`randomname` 只有源码包，在此构建——下载固定提交源码与 Hugging Face 模型，把符号链接转为普通文件，记录 SHA256）、`seedvc-install.ps1`（应用电脑离线：校验、用 ace-env 同一 Python 建独立 venv、离线安装、显卡自检，自检通过后最后写 `install.json`）。
- 版本源、package / lock、README、CHANGELOG 同步为 1.4.0；`deploy-release.ps1` 文件清单加入 `voice_swap.py`、`seedvc_runner.py`；`.gitignore` 允许清单加入新文件。`benchmark_adjust.py` 与新 `benchmark_voice.py` 共用验证记录：`VALIDATION-v1.4.0.json` 的 `required_benchmarks` 为 adjust 与 voice，两者都通过才 `passed`。

## 本次验证（开发电脑 Xeon + GTX 1070，无 YuE2 / Seed-VC）

- 前端生产构建与 4 项打包测试通过（`node_modules` 链接到同机 v1.3.0 克隆，锁文件除版本号外相同，未重新下载）。
- 后端：原有 8 项 HTTP / 队列、6 项控制、6 项纯音乐、8 项变调变速、4 项多版本回归通过；新增 `test_voice.py` 9 项通过（共 41 项）。换声测试使用与 Seed-VC 接口一致的替代 `inference.py`，运行真实的 `seedvc_runner.py` 子进程：模型只加载一次、每乐句一次调用及参数（F0 条件、不自动调音高、八度、步数、CFG、fp16、参考片段）、静音段保持静音、乐句位置与音高、响度匹配、混音、生成原声不变、失败信息、无人声时跳过、自检与无显卡拒绝；管线顺序（未调整用 original-stems，变调时用未调整原唱的 reference-stems）、引擎缺失时零阶段失败、A / B 复用；HTTP 设置与逐首、按版本字母映射（含 3 个版本只换 C）、旧格式设置回退为默认、命名、保留原唱 / 纯音乐忽略、未安装时整批拒绝；派生作品命名、作品信息与结果说明；验证记录需全部基准通过。
- 安装脚本：两份 PowerShell 解析无误；用假安装包在短路径做离线演练，校验、Python 识别、独立 venv、离线 pip、自检调用均按预期，自检失败时不写 `install.json`；已存在需 `-Replace`、文件损坏会被拒绝。固定的依赖版本已在 PyPI 与 PyTorch cu128 索引核对存在 cp311 Windows wheel（只查询，未下载）。
- 隔离浏览器实际操作（替代安装、暂停的队列；合并多版本后复测：3 个版本只勾 C，入队 3 个任务仅 C 带换声并命名 `Lo-fi Demo Song (Original Voice)`，逐首开关随该曲版本数 3→2 显示，旧格式设置自动回退）：未安装时面板提示且开关不可用；安装后 A 开关、音域保存与刷新保持；逐首单独设置 A + B；队列确认的换声行；入队的 A / B 任务都带 `voice`（逐首降八度），名称 `R&B Demo Song (Original Voice)` / `Jazz Demo Song (Original Voice)`；作品卡片与详情的换声信息；换声作品的 Slowed 80% 预览名与后端名称一致 `Lo-fi Demo Song (Original Voice, Slowed 80%)`；未安装时确认对话框警告并禁用确认；375 px 宽无横向滚动；无应用报错（仅重启服务期间的连接拒绝）。
- **未完成：应用电脑上的引擎安装与显卡自检、YuE2 + UVR + Seed-VC 实测、主观试听。** `VALIDATION-v1.4.0.json` 为 `passed: false`，部署脚本会拒绝安装，直到下面两个基准都通过。v1.3.0 的变调变速实测同样尚未在应用电脑完成，本次一并要求。

## 在联网的 i5 电脑上下载（约 6 GB）

1. 先在应用电脑查看 Python 版本：`local-test\ace-env\Scripts\python.exe --version`（例如 3.11.x）。i5 电脑需要安装同一小版本的 64 位 Python（python.org，含 py 启动器）。
2. 在 i5 电脑拉取本分支（或合并后的 main），运行：`powershell -ExecutionPolicy Bypass -File dual-version\seedvc-download.ps1 -Bundle D:\seedvc-bundle -PythonVersion 3.11`（版本按第 1 步）。只下载公开的代码、软件包与模型，不上传任何东西。
3. 把整个 `D:\seedvc-bundle` 拷到应用电脑（U 盘或局域网均可，安装时会核对校验值）。

## 在应用电脑（i7-14700F + RTX 5070）上完成

1. 拉取分支，在 `sing-studio-batch-preview/` 运行 `npm ci`、`npm run build`、`npm run test:sites`。
2. 确认队列空闲后安装引擎：`powershell -ExecutionPolicy Bypass -File dual-version\seedvc-install.ps1 -Bundle <拷来的安装包> -Runtime <现有 local-test 目录>`。离线安装到 `<local-test>\seed-vc`，最后在显卡上自检；成功时显示显卡名称、torch 与 CUDA 版本。重装用 `-Replace`（旧目录改名保留）。
3. 回归：用 ace-env 的 Python 并设置 `SING_LOCAL_TEST` 后运行 `test_controls.py`、`test_instrumental.py`、`test_adjust.py`、`test_versions.py`、`test_voice.py`、`test_backend.py`。
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

- 本分支 `feature/voice-swap-v1.4.0` 基于尚未合并的 `feature/key-tempo-v1.3.0`（PR #1），并已合并其后加入的“每首 1–10 个版本”提交（2361540）；新 PR 以它为目标分支以便只审阅 v1.4.0 改动；先合并 PR #1，再合并本 PR，依次打 `v1.3.0`、`v1.4.0` 标签。禁止覆盖历史标签或 force push。
- Git 只包含维护源码、文档及脱敏验证报告；替代运行环境、安装包、模型、测试音频与作品均在 Git 外。

---

# Sing Studio v1.3.0 · 2026-10-16

## 本次交付 · 多版本（同一 v1.3.0）

- 用户要求“一首歌改编成不同版本，可以选择数量”。确认：每首 1–10 个生成版本，各版本不同风格；统一数量并允许逐首覆盖；放进 v1.3.0 同一个 PR，人声替换仍为之后的 v1.4.0。
- 设置新增 `version_count`（1–10，默认 2）、`styles`（10 项）、`version_instructions`（10 项）；歌曲新增 `styles`（10 项，空值跟随统一）与 `version_count`（`null` 跟随统一）。`style` / `style_b`、`instructions_a` / `instructions_b` 始终与第 1、2 项同步保存，旧设置读入时自动补齐其余版本（未使用的预设）。
- `create_batch` 接受 1–10 个 variants，字母 A–J，风格须不同（忽略大小写和首尾空格），第一个版本分析、其余复用；种子上限修正为不超过 2147483647。新增 Soul、Funk 预设。
- 界面：每首版本数量选择、按数量显示的版本风格按钮组与补充要求、重复风格提示；逐首版本数与各版本风格下拉；底部、确认对话框、作品分组（“N 个版本”“N 版总处理用时”）与说明文字不再写死 A / B。
- 验证：新增 `test_versions.py` 4 项（默认与旧设置迁移、列表与 A/B 同步、逐首覆盖、1/10 个版本展开、重复与超量拒绝、种子范围）；全部 32 项后端回归与 4 项打包测试通过。浏览器：旧设置迁移、4 个版本按钮组、重复提示与恢复、逐首 3 个版本并改 C 为 Phonk，实际入队 A/B/C 三个任务且名称带变调说明。

## 本次交付 · 变调变速

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
2. 用现有音乐环境的 Python 运行 `test_controls.py`、`test_instrumental.py`、`test_adjust.py`、`test_versions.py`，以及设置 `SING_LOCAL_TEST` 后的 `test_backend.py`。
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
