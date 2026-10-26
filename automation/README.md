# Sing Studio · n8n 自动上传（填到 Submit 前）

流程：Sing Studio“发行上架”生成发行包 → 自动另存到 **EP 导出文件夹\<发行名称>\** → 打开 n8n 表单，选一个 EP → n8n 在**上传用的 Chrome** 里打开 Ditto Music / OFFstep 并逐项填写（音频、封面、资料、歌词、AI 声明、发行日期）→ **你本人核对并点 Submit** → 回到表单选“已点 Submit”（可填 UPC）→ EP 文件夹写入 `submitted.json`，Sing Studio 里的发行标记为“已提交”，以后不会再出现在列表里。

自动化不会做的事：登录、验证码、同意条款、付款、点 Submit。填写程序遇到名字像 Submit / Distribute / Publish / Pay / Checkout / 提交 / 付款 / 发布 的按钮会拒绝点击并停下。

## 一次性设置（应用电脑）

1. **Sing Studio**：打开“发行上架”，在“EP 导出文件夹”填一个完整路径（如 `D:\Sing Studio EP`）并保存。以后每次生成发行包都会另存一份到这里，每个 EP 一个文件夹；文件夹写完才出现，不会读到一半。
2. **本文件夹的依赖**：在仓库的 `automation` 文件夹运行

   ```
   npm install
   ```

   只装 `playwright-core`（不下载浏览器，用电脑上已有的 Chrome）。
3. **（可选）`config.json`**：复制 `config.example.json` 为 `config.json` 再改。默认会自动找 Chrome，上传用的 Chrome 资料放在 `%LOCALAPPDATA%\SingStudio\upload-chrome`（和你平时用的 Chrome 分开），调试端口 9333 只监听本机。
4. **登录一次**：运行

   ```
   node release-helper.mjs login ditto
   ```

   在弹出的 Chrome 里登录 Ditto Music（OFFstep 用 `login offstep`）。之后这个 Chrome 会保持登录。
5. **安装 n8n**（Node.js 20–24）：

   ```
   npm install -g n8n
   ```

   n8n 2.x 默认关闭“Execute Command”节点，这个流程需要它；另外不需要读写文件节点。启动前设置（PowerShell）：

   ```
   $env:NODES_EXCLUDE = "[]"
   n8n start
   ```

   之后打开 http://localhost:5678 ，第一次需要建一个本机 n8n 账户。
6. **导入流程**：n8n 里 Workflows → Import from File → 选 `automation/n8n/sing-studio-upload.json`。打开 **Settings** 节点，把 `helper` 改成本机 `release-helper.mjs` 的完整路径。保存并 **Activate**（激活）。
7. **表单网址**：打开 **Start** 节点复制 Production URL（形如 `http://localhost:5678/form/sing-studio-upload`），加到浏览器书签。

## 每次上传

1. Sing Studio“发行上架”：加入作品、填资料、选发行日期、生成发行包。
2. 打开表单书签 → “检查待上传的 EP” → 选 EP，勾选已核对 → “在 Chrome 里填写”。
3. 切到上传用的 Chrome：核对每一项（尤其是表单结果页提到“没找到”的项目，按 EP 文件夹里的《上传清单 Upload checklist.md》补上），然后**你本人点 Submit**。
4. 回到表单页：选“我已在平台核对并点了 Submit”，有 UPC 就填上 → 保存。选“还没有，先保留”则 EP 留在列表里，下次再来。

每次填写都会在 EP 文件夹里写 `upload-log.json`（填了哪些、跳过哪些）。

## 命令行（不需要 n8n）

在仓库的 `automation` 文件夹运行。编号（release id）只需写前 6 位以上，用 `releases` 查看。

| 命令 | 作用 |
| --- | --- |
| `node release-helper.mjs releases` | 所有发行：编号、标题、平台、状态、曲目数、日期、还缺什么 |
| `node release-helper.mjs build <编号>` | 生成发行包并等它复制到 EP 导出文件夹 |
| `node release-helper.mjs upload <编号>` | 一步完成：需要时先生成，再在上传用的 Chrome 里填好表单 |
| `node release-helper.mjs upload <编号> --rebuild` | 同上，但一定重新生成发行包 |
| `node release-helper.mjs list` | 已生成、还没提交的 EP |
| `node release-helper.mjs login ditto` | 打开上传用的 Chrome 登录（只需一次） |
| `node release-helper.mjs fill <编号>` | 只填表单（发行包已生成时） |
| `node release-helper.mjs submitted <编号> [UPC]` | 你点了 Submit 之后记录提交 |

例：先 `releases` 找到编号 `18e09664…`，资料都填好后 `upload 18e09664`，在 Chrome 里核对并点 Submit，再 `submitted 18e09664 123456789012`。每个命令输出一行 JSON，`ok` 为 `false` 时看 `error`，成功时看 `message`。命令行同样不会点 Submit、付款或发布类按钮。

## 字段映射（maps/）

`maps/ditto.json` 按 Ditto 帮助中心的字段名写成（2026-10-24），**还没有在真实的 Ditto Release Builder 上核对**（`"verified": false`）：找不到的字段会跳过并在结果页列出，不会乱填。第一次登录后，请让 Claude 打开这个 Chrome 看一次 Release Builder，按实际页面改好映射再把 `verified` 改成 `true`。`maps/offstep.json` 还没有步骤：只会打开网站，表单按清单手动填。

映射格式：每一步有 `action`（`fill` / `select` / `check` / `upload` / `click` / `wait` / `goto`）、`target`（`label`、`role` + `name`、`placeholder`、`text` 或 `css`，可加 `nth`）、`value`（如 `{{release.title}}`、`{{track.version}}`）；`for_each: "tracks"` 按曲目重复；`optional: true` 表示找不到就跳过。可用的数据见 `release-helper.mjs` 的 `formData`。

## 测试

```
npm test
```

用本机 Chrome（无界面）在 `test/mock-builder.html` 这个模拟上传页上跑 Ditto 映射，检查填写、附件、AI 声明与“拒绝点 Submit”，并用模拟的 Sing Studio 检查列表、编号前缀、`build`、`upload` 与提交记录。

## 注意

- 平台条款：这是在你本人已登录的浏览器里代填表单，最后由你提交；平台改版后映射需要更新。请不要用它批量发大量重复版本，Ditto 会把刷量内容下架。
- 歌曲与 n8n 都在应用电脑本机；n8n 只通过 `127.0.0.1:18765` 访问 Sing Studio，不需要对外开放端口。
