# Telegram 广告杀手

独立的Telegram群广告审核机器人。Python 3.11+，标准库，无额外Python依赖。版本v1.0.1。

支持中文广告组合规则、隐藏链接、域名名单、机器人引流信号、照片AI复核、三分钟复核、管理员主动封禁和本群样本学习。提供案件归档、黑白名单、按钮管理、功能开关与本人误封申诉。

## 快速了解

默认observe，只检测。真实处罚需主人明确开启；管理员`/adkill CONFIRM`在观察模式也会执行真实处罚。

完整流程、规则、权限、失败处理与边界见[设计方案](docs/DESIGN.md)。AI的自述置信度不作为准确率；模糊证据不自动封禁。

## 部署

1. 创建Telegram Bot；将Bot加入源群和归档频道。源群需要管理员、删除消息和限制成员权限；归档频道需要管理员、发布消息权限。
2. 将代码放入`/opt/ad-killer`，保证服务运行用户可读取源码及进入该目录。先创建root所有的`/etc/ad-killer`，权限700；再复制`config.example.json`为该目录的`config.json`，以root所有、权限600保存。填写下表中的配置，勿使用示例ID。
3. 以root所有、权限600分别保存`bot-token`及`ai-key`，每个文件只有对应凭证。关闭AI时ai-key可为空，systemd模板仍要求该文件存在。不要将Token或Key写入源码、命令行、GitHub。服务通过`LoadCredential`读取这三个文件，并以`AD_CONFIG=%d/config.json`定位运行时配置副本；无需放宽配置目录或文件权限。
4. 安装`ad-killer.service`到`/etc/systemd/system/`，执行`systemctl daemon-reload`与`systemctl enable --now ad-killer`。
5. 只启动一个轮询实例；已有Webhook会拒绝启动，不擅自清除。检查`systemctl status ad-killer`和`/var/log/ad-killer/bot.log`。

服务以DynamicUser运行，SQLite在`/var/lib/ad-killer`；日志轮转，总大小约4MiB；默认内存上限160MiB、CPU30%。不需要公开Web端口。

### 必填配置与首次验收

| 项目 | 保存位置/说明 |
| --- | --- |
| Bot Token | `/etc/ad-killer/bot-token`，由BotFather签发 |
| Bot用户名 | `expected_username`，不含`@`，必须与Token对应 |
| 主人Telegram数字ID | `owner_id`，不是用户名或手机号 |
| 源群数字ID | `groups`中的键，每个群分别授权；默认`mode: observe` |
| 归档频道数字ID | `archive_channel`，完整案件流程需要频道发布权限 |
| AI URL、模型、开关 | `ai.base_url`、`ai.model`、`ai.enabled`；启用AI时填写 |
| AI密钥 | `/etc/ad-killer/ai-key`，不放入config.json |

AI需要支持OpenAI兼容的`chat/completions`接口；照片识别还需模型支持视觉。远程服务必须使用HTTPS，只有字面本机回环地址可用HTTP。示例`127.0.0.1:8000`只是占位：没有在自己的服务器运行对应接口就不能使用。项目不提供模型额度；使用的是部署者自己配置的上游服务额度。

首次部署先在测试群保持observe，确认`/adstatus`、回复消息的`/adcheck`和`/adreview`模拟按钮正常，检查频道归档权限。确实需要真实处罚时，由主人发送`/admode review CONFIRM`。管理员主动`/adkill CONFIRM`即使observe也会真实删除封禁，不要拿重要账号测试。

本项目目前是手工部署，不是填写四项即可运行的一键安装器。建议使用支持systemd凭证机制的Ubuntu 24.04或更新版本。若启动失败，检查`systemctl status ad-killer`、`journalctl -u ad-killer -n 50`和日志；不要把凭证原文贴到公开Issue。已部署实例升级前备份源代码、配置和SQLite，检查已有drop-in设置，不要覆盖生产配置或删除已有数据库。

启动时自动注册主人私聊、群管理和主人群内命令菜单；注册失败会写入日志。命令权限由程序实时校验，与客户端是否显示菜单无关。

## 常用命令

| 命令 | 权限/用途 |
| --- | --- |
| `/adstatus` | 群管理/主人查看状态 |
| `/adcheck` | 回复消息检测，只检测；人工AI检查绕过缓存 |
| `/adreview` | 回复消息创建复核，观察模式只模拟 |
| `/adkill CONFIRM` | 管理员主动删除并永久封禁，完整成功后学习 |
| `/adcase AD-000001` | 查看本群案件 |
| `/admanage`、`/adsettings` | 名单案件面板、本群检测开关 |
| `/adblacklist`、`/adhistory` | 本群有效ID名单、案件历史 |
| `/adunban AD-000001` | 解封，保留样本 |
| `/adwrong AD-000001` | 纠正误封，撤回本案样本 |
| `/adwhite add/remove 用户ID` | 本群豁免，不授予管理权限 |
| `/adgwhite add/remove 用户ID` | 全局白名单，仅主人 |
| `/adwhitelist`、`/adaudit` | 白名单范围、变更记录 |
| `/admode review CONFIRM` | 主人开启三分钟真实复核；observe/delete/ban模式见方案 |
| `/adappeal AD-000001 说明` | 被封成员私聊申诉，不自动解封 |

普通用户不能操作管理指令，但可在本群复核消息投票；真实案件不能自投。频道管理员只可对原归档卡片解封/纠错。私聊管理功能仅主人可用，其他成员只开放申诉说明与本人申诉。

## 测试

```sh
python3 -m unittest discover -q
python3 -m compileall -q .
```

测试使用模拟Telegram和临时SQLite，不请求生产Bot，不真实处罚成员。CI针对Python3.11/3.12/3.13执行同一套测试。

## 已知限制

多个管理机器人同时删帖时，原消息可能在复核或归档前消失。复核提示允许脱离原消息发送；归档优先复制原消息，仅在Telegram明确返回原消息不存在时，改用接收时保存的文字、媒体file_id或OCR快照，并标明无法核实删除者。媒体不能重发时只保留文字/OCR；没有可用证据或频道权限不足时仍停止处罚。原消息已不存在不会阻止已获批准的封禁，但不把其他机器人的删除记为本Bot删除，也无法确认谁删除了消息。

AI队列保持单工作线程和32条上限，优先处理手动检测、规则疑似广告、图片、普通文字；正在执行的请求不抢占，不增加并发模型请求。自动任务排队超过90秒会交人工兜底（需有本地规则命中）。`/adstatus`显示本次启动的连续失败、队列满次数、最近20次排队与模型耗时中位数。连续3次检测失败会私聊Bot主人一次，真实模型请求恢复成功后通知恢复；缓存命中不会被当作服务恢复。主人需先私聊启动Bot，通知能否送达取决于Telegram私聊权限。重启后这些健康计数重置；失败消息不会自动重扫。

不访问链接目标页面、不扫描历史，不识别视频/音频/贴纸画面；照片需AI开启。未实现二维码专用解码和机器人触发者归因。重复检测只覆盖同群短时间相同推广内容。模型判断可能误判或漏判；请先在测试群验证。

本版未加入复核前自动禁言/删除，保护正常成员免于提前处罚。申诉更新显示失败时仍保存在案件详情，需管理员查看。解封不恢复已删除消息，也不自动重新入群。

## 致谢和许可证

MIT。本项目以公共机器人功能作为设计参考，不包含其未公开源码或规则库。关键词组合参考AzurLab/Tg-Ad-RegEx所展示的类别，公开疑似样例参考MarkIvory2973/tg-spam README；样例不等于已确认广告。详见设计方案。

## 审查修复

v1.0.1修复命令误路由、AI失败人工兜底、普通消息同步查询、编辑消息新版本复核和卡片满额记录。核实结论与剩余边界见[审查修复说明](docs/REVIEW-20261008.md)。
