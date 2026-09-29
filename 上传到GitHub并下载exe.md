# 上传到 GitHub，云端自动打出 exe

目标：**本机完全不用装 Python**，在 GitHub 云端把 `共济生图.exe` 打出来，你下载即可。

> ⚠️ exe 只能在 Windows 上打包，所以走云端（GitHub 免费提供 Windows 机器）。

---

## 方式 A：网页上传（不用命令行，推荐第一次用）

1. **注册 / 登录** [github.com](https://github.com)（免费）；

2. 右上角 **+ → New repository**
   - Repository name：`maizi-image`（随便起）
   - 选 **Public**（私有仓库的 Actions 也能用，但公开仓库不消耗额度）
   - **不要**勾 Add README / .gitignore
   - 点 **Create repository**

3. 进入新仓库，点 **Add file → Create new file**，文件名处**直接输入**（斜杠会自动建目录）：
   ```
   .github/workflows/build-windows.yml
   ```
   把本目录 `.github/workflows/build-windows.yml` 的内容**整段复制**进去，拉到底点 **Commit changes**；

4. 回到仓库首页 → **Add file → Upload files**，把这几个文件拖进去（**不要**拖 `.github` 目录）：
   ```
   app.py
   index.html
   README.md
   启动(双击我).bat
   打包exe.bat
   ```
   点 **Commit changes**；

5. 点仓库顶部 **Actions** 标签 → 左侧选 **打包 Windows exe** → 右侧 **Run workflow → Run workflow**；

6. 等约 **1~2 分钟**（刷新页面看进度），跑完后：
   - 点进那次运行，页面底部 **Artifacts** 区域下载 **maizi-image-windows**（是个 zip，解压得到 `共济生图.exe`）；
   - 产物保留 **90 天**，过期就再跑一次。

---

## 方式 B：命令行推送（以后改代码更方便）

本目录**已经初始化成 git 仓库**并做好了首次提交，你只需：

```bash
cd "/Users/a123/Desktop/日常对话/maizi-desktop"

# 1) 在 GitHub 建好空仓库后，把地址填进来（换成你自己的）
git remote add origin https://github.com/你的用户名/maizi-image.git

# 2) 推送（会要求输入 GitHub 用户名 + 访问令牌，不是登录密码）
git push -u origin main
```

> 密码处要填 **Personal Access Token**：GitHub → 右上角头像 → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token，勾选 **repo** 权限，生成后复制（只显示一次）。
>
> 也可以用 SSH：`git remote add origin git@github.com:你的用户名/maizi-image.git`（需先配置 SSH key）。

推送成功后，Actions 会**自动开始构建**，同样在 Actions 页面下载产物。

---

## 想要"永久下载链接"（可选）

产物 90 天会过期。想要一条永久链接，打个 tag 即可 —— 工作流会自动创建 Release：

```bash
git tag v1.0
git push origin v1.0
```

之后在仓库 **Releases** 页面就能看到 `共济生图.exe`，链接长期有效，可以直接发给别人下载。

---

## 排错

| 现象 | 处理 |
| --- | --- |
| Actions 页面看不到工作流 | 确认文件路径精确为 `.github/workflows/build-windows.yml`（注意是 `.github`，前面有点） |
| 构建失败在 PyInstaller 那步 | 点进日志看红字；把日志发我 |
| 下载的 exe 双击没反应 | 首次运行 Windows 可能弹「已保护你的电脑」→ 点**更多信息 → 仍要运行**（未签名程序的正常提示） |
| exe 能开但页面打不开 | 打开 `%APPDATA%\maizi\app.log` 看错误，把内容发我 |

---

## 说明：exe 里包含什么

- `app.py` 被编译进 exe；`index.html` 通过 `--add-data "index.html;."` 一起打进去，运行时解到临时目录。
- exe **不含你的 API 密钥**；密钥在首次运行时由你填入，存在 `%APPDATA%\maizi\config.json`，只在本机。
- 换电脑使用：直接拷 exe，首次打开重新填一次密钥即可。
