# 部署与备份恢复

## 正式部署

1. 将源码放到可持续运行的主机。按 README 安装依赖、创建管理员。将正式报名表导入一次。用现有数据库迁移时，使用下方备份和恢复流程。
2. 配置固定 HTTPS 域名或学校网络提供的 HTTPS 地址，将反向代理上游指向应用所在主机的 `127.0.0.1:8080`。只开放代理的 HTTPS 端口，数据库放在本地持久卷。将 `QINGYOU_SECRET` 作为至少 32 字符的部署密钥保存，服务重启时沿用。
3. 在服务管理器中以普通服务账户启动。Windows 可运行以下命令，Linux 将 Python 路径改为 `.venv/bin/python`。使用环境变量或服务账户的密钥配置设置 `QINGYOU_SECRET`、`QINGYOU_SIMSUN`、`QINGYOU_TIMES`。

```powershell
.\.venv\Scripts\python.exe -X utf8 app.py --db runtime/event.sqlite3 serve --host 127.0.0.1 --port 8080 --https
```

PDF 默认使用 Windows 的 `C:\Windows\Fonts\simsun.ttc` 和 `C:\Windows\Fonts\times.ttf`。Linux 部署需自行提供具备使用权限的字体文件，并以 `QINGYOU_SIMSUN`、`QINGYOU_TIMES` 指定绝对路径。字体随 PDF 嵌入，源码包不分发字体。

4. 从不同网络的两台手机打开 HTTPS 房间链接。检查公开大屏、管理员登录、两字段登记入口、PDF 下载和 37 场赛程。用临时验收房间演练，正式房间应保持未抽签状态。
5. 将同一房间公开地址或 GitHub Pages 入口发到微信群或 QQ 群。若使用临时同网段试运行，可显式绑定主机局域网 IP。微信群/QQ群跨网访问需要可达的 HTTPS 地址，`127.0.0.1` 只在服务器本机可达。

单个实例支持多个房间与并发请求。SQLite 用 WAL、唯一约束和写事务防止重复抽签。备份和正式数据需要可持久读写的本地文件系统。

## GitHub Pages 群分享入口

将 Pages 发布源设为 `main` 分支的 `/docs`。更换比赛服务域名时，更新 `docs/index.html` 的三个房间链接并推送。Pages 入口提供导航，抽签请求由链接指向的 HTTPS 服务处理。

当前联调入口指向运行于本机的 Cloudflare Quick Tunnel。电脑、Python 服务和隧道进程需保持运行；隧道重启会生成新地址。固定域名部署后更新 Pages 的链接即可继续使用原群入口。

## 备份

在操作日志页点“下载数据库备份”，或运行以下命令。命令接受新文件路径，备份可在线执行。

```powershell
.\.venv\Scripts\python.exe -X utf8 app.py --db runtime/event.sqlite3 backup runtime/event-backup-01.sqlite3
```

建议在名单核验完成、抽签锁定、日程发布、比赛结束各保存一份。备份含学院负责人、参赛名单、裁判凭证和管理员账号哈希，按校内授权范围保管。迁移时也应保存 `QINGYOU_SECRET` 或重新签发登录会话。

## 恢复

```powershell
.\.venv\Scripts\python.exe -X utf8 app.py restore runtime/event-backup-01.sqlite3 runtime/event-restored.sqlite3
```

程序验证数据库完整性、外键和结构版本。恢复到新文件后用该文件启动服务，核对房间 ID、签位、已完成比赛、日程和操作日志，再切换反向代理。旧库保留作回溯。若恢复到抽签开始前，先告知现场停止操作，并以主持人公开核对的快照为准。
