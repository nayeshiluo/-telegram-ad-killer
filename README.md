# Telegram 广告杀手

独立的Telegram群广告审核机器人。Python 3.11+，标准库，无额外Python依赖。版本v1.0.1。

支持中文广告组合规则、隐藏链接、域名名单、机器人引流信号、照片AI复核、三分钟复核、管理员主动封禁和本群样本学习。提供案件归档、黑白名单、按钮管理、功能开关与本人误封申诉。

## 快速了解

默认observe，只检测。真实处罚需主人明确开启；管理员`/adkill CONFIRM`在观察模式也会执行真实处罚。

完整流程、规则、权限、失败处理与边界见[设计方案](docs/DESIGN.md)。AI的自述置信度不作为准确率；模糊证据不自动封禁。

## 部署

1. 创建Telegram Bot；将Bot加入源群和归档频道。源群需要管理员、删除消息和限制成员权限；归档频道需要管理员、发布消息权限。
2. 将代码放入`/opt/ad-killer`。复制`config.example.json`为`/etc/ad-killer/config.json`，填写Bot用户名、主人ID、源群ID及归档频道ID。AI默认关闭，需支持chat/completions且支持视觉的模型服务才可检测照片。
3. 创建`/etc/ad-killer`，权限700；以权限600保存`bot-token`及`ai-key`。关闭AI时ai-key可为空，systemd模板仍要求该文件存在。不要将Token或Key写入源码、命令行、GitHub。
4. 安装`ad-killer.service`到`/etc/systemd/system/`，执行`systemctl daemon-reload`与`systemctl enable --now ad-killer`。
5. 只启动一个轮询实例；已有Webhook会拒绝启动，不擅自清除。检查`systemctl status ad-killer`和`/var/log/ad-killer/bot.log`。

服务以DynamicUser运行，SQLite在`/var/lib/ad-killer`；日志轮转，总大小约4MiB；默认内存上限160MiB、CPU30%。不需要公开Web端口。

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

不访问链接目标页面、不扫描历史，不识别视频/音频/贴纸画面；照片需AI开启。未实现二维码专用解码和机器人触发者归因。重复检测只覆盖同群短时间相同推广内容。模型判断可能误判或漏判；请先在测试群验证。

本版未加入复核前自动禁言/删除，保护正常成员免于提前处罚。申诉更新显示失败时仍保存在案件详情，需管理员查看。解封不恢复已删除消息，也不自动重新入群。

## 致谢和许可证

MIT。本项目以公共机器人功能作为设计参考，不包含其未公开源码或规则库。关键词组合参考AzurLab/Tg-Ad-RegEx所展示的类别，公开疑似样例参考MarkIvory2973/tg-spam README；样例不等于已确认广告。详见设计方案。

## 审查修复

v1.0.1修复命令误路由、AI失败人工兜底、普通消息同步查询、编辑消息新版本复核和卡片满额记录。核实结论与剩余边界见[审查修复说明](docs/REVIEW-20261008.md)。
