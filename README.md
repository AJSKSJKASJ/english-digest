# 每日外刊精读 · GitHub Actions 版

每天早上 8 点，GitHub 的服务器自己去抓《卫报》，让 DeepSeek 挑 3 个长难句写微讲解，生成一个手机上好看的网页，发布到 GitHub Pages，再用 PushPlus 给你发一条微信——点开就是排版好的页面。

- 不需要服务器，不需要电脑开机
- 不需要装 Python、不用装任何软件
- 一个月 0 元（GitHub Actions 免费额度足够，每天只跑 1 分钟）

```
每天的流程
  GitHub Actions（UTC 00:00 = 北京 08:00）
    → 抓 RSS（卫报 world/international/business/science/technology）
    → DeepSeek 挑 3 句 + 写拆解/短语/防坑/造句（返回 JSON）
    → 套模板生成网页 → 提交到 docs/
    → GitHub Pages 自动发布
    → PushPlus 推一条微信，里面是 3 句原句 + 【阅读全文】链接
```

---

## 一、准备三样东西

| 东西 | 说明 |
|---|---|
| GitHub 账号 | 你已经有 |
| DeepSeek API Key | `sk-` 开头，在 https://platform.deepseek.com 的「API keys」里创建 |
| 微信 | 用来收推送，关注 PushPlus 的服务号即可 |

---

## 二、建仓库

1. 打开 https://github.com/new
2. **Repository name** 填 `english-digest`
3. 选 **Public**（免费开 GitHub Pages 必须公开；内容是英文句子和讲解，不涉及隐私）
4. 勾选 **Add a README file**
5. 点 **Create repository**

> 想用私有仓库也行，但 GitHub Pages 对私有仓库要付费（Pro），后面会讲怎么换。

---

## 三、上传文件

本地这个文件夹里就是要上传的东西：

```
C:\Users\JY\Documents\deepseek-harness\default-workspace\english-digest\
├── scripts\digest.py        ← 主程序（不用 pip 装任何库）
├── tests\smoke_test.py      ← 自检（可选，不影响运行）
├── .gitignore
└── README.md                ← 会被仓库里原来的 README 覆盖
```

1. 在仓库页点 **Add file → Upload files**
2. 把 `scripts` 文件夹、`tests` 文件夹、`.gitignore`、`README.md` 一起拖进去
   （Windows 资源管理器里看不到 `.gitignore`，可以在地址栏输入路径后按回车，再拖）
3. 下面填 commit 说明 `init`，点 **Commit changes**

> `.gitignore` 拖不进去也没关系，不影响运行。

---

## 四、创建定时任务文件

GitHub 网页上传不方便处理以点开头的文件夹，所以这个文件用「新建文件」的方式做：

1. 仓库页点 **Add file → Create new file**
2. 文件名输入：

```
.github/workflows/digest.yml
```

（输入斜杠会自动变成文件夹）

3. 把 [.github/workflows/digest.yml](.github/workflows/digest.yml) 的内容整个粘进去
4. 点 **Commit changes**

---

## 五、打开 GitHub Pages

1. 仓库页点 **Settings**（顶部菜单最后一个）
2. 左侧点 **Pages**
3. **Source** 选 **Deploy from a branch**
4. **Branch** 选 `main`，右边的文件夹选 `/docs`，点 **Save**
5. 等 1–2 分钟，页面上方会出现你的网址：

```
https://你的用户名.github.io/english-digest/
```

现在打开是 404，因为 `docs/` 还是空的——跑完第一次就有了。

---

## 六、拿 PushPlus 的 token

1. 微信搜索公众号 **pushplus 推送加**，关注
2. 按公众号里的提示，用微信扫码登录 https://www.pushplus.plus
3. 登录后在首页能看到一串 **token**（32 位字母数字），复制它
4. 建议顺手完成实名认证：免费额度（每天 200 条）需要实名后才能正常用

---

## 七、填两个密钥

1. 仓库页 **Settings → Secrets and variables → Actions**
2. 点 **New repository secret**，加两个：

| Name | Secret |
|---|---|
| `DEEPSEEK_API_KEY` | 你的 `sk-...` |
| `PUSHPLUS_TOKEN` | 上一步复制的 token |

> 名字要一模一样，不能有空格。密钥存在 GitHub 侧，写不进代码，也不会出现在日志里。

---

## 八、手动跑第一次

1. 仓库页点 **Actions** 标签
2. 左侧选 **每日外刊精读** → 右边 **Run workflow** → 绿色按钮 **Run workflow**
3. 等 30–60 秒刷新，会看到一条运行记录，点进去看每一步
4. 三条都变绿之后：
   - 打开 `https://你的用户名.github.io/english-digest/` 看网页
   - 微信应该收到一条「☕ 外刊精读 · 11月21日」

如果哪一步红了，点开那一步看报错，对照下面「常见问题」。

---

## 九、之后就自动了

每天北京时间 8:00 自动跑（GitHub 的排队机制可能延迟几分钟到半小时）。

- 想改时间：编辑 `.github/workflows/digest.yml` 里的 `cron: "0 0 * * *"`（UTC 时间，减 8 小时就是北京时间）
- 想看往期：`https://你的用户名.github.io/english-digest/archive/2026-11-21.html`
- 想手动补跑：Actions → Run workflow

---

## 常见问题

**打开网址 404**
Pages 第一次发布要 1–2 分钟；确认 Settings → Pages 里 Source 是 `main` + `/docs`。另外仓库必须是 Public。

**收不到微信**
看 Actions 最后一步日志有没有 `PushPlus 成功`。失败常见原因：token 复制错了、没关注公众号、没实名认证。

**Actions 报 `DEEPSEEK_API_KEY` 缺失**
Secret 名字拼错，或者加到了 Environment 而不是 Repository。重新加一次。

**报 401 / Insufficient Balance**
DeepSeek 账户没余额，去 platform.deepseek.com 充值（一天 3 句大概花几分钱）。

**页面内容没更新**
看日志里「提交并发布」那步；如果是 `没有变化`，说明当天抓到的文章和昨天一样（节假日 RSS 更新少）。

**微信里点开链接很慢**
`github.io` 在国内偶尔慢。换成自定义域名或 Cloudflare 代理会好，需要时再说。

**想改风格**
`scripts/digest.py` 顶部的 `CSS` 常量就是页面样式，改颜色改字号都在那里；`render_card()` 决定每张卡片的结构。

**想换文章源**
`DEFAULT_FEEDS` 改成别的 RSS 地址即可（经济学人 RSS 只有一两句摘要，会被长度过滤掉，所以默认只用卫报）。

---

## 本机自检（可选）

装过 Python 的话，可以在本机不联网验证渲染：

```powershell
cd C:\Users\JY\Documents\deepseek-harness\default-workspace\english-digest
python tests\smoke_test.py          # 8 项检查
python scripts\digest.py --dry-run  # 用示例数据生成 docs\index.html，浏览器直接打开看
```

要真跑一次（会消耗 API 额度）：

```powershell
$env:DEEPSEEK_API_KEY="sk-你的key"
python scripts\digest.py
```
