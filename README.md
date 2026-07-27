# 云鲸扫拖机器人 Home Assistant 集成

云鲸扫拖机器人 Home Assistant 自定义集成，通过云鲸云端 API 和 MQTT
协议发现、控制机器人并同步状态。集成不提供局域网直连模式。

## 功能

- 开始、暂停、继续和停止清扫
- 返回基站、查找机器人
- 四档吸力
- 房间分区清扫（Home Assistant 2026.3 及以上）
- 拖布湿度选择
- 清洗并烘干拖布开关，可打开启动、关闭结束洗烘流程
- 支持单独烘干能力的型号额外提供烘干拖布按钮
- 基站集尘按钮
- 云端耗材剩余用量传感器
- 云端机器人通知同步，包括耗材更换或用尽提醒
- 电量、清扫面积、清扫时间和固件版本
- 回基站和充电状态
- 地图、房间名称、机器人位置、轨迹与障碍物叠加
- 实时推送、断线重连、状态订阅续期和轮询兜底
- 云端凭据失效后的 Home Assistant 重认证流程

具体功能取决于机器人、基站配置和固件。集成会读取设备功能列表，仅在设备声明
支持时提供单独烘干入口。

耗材传感器显示与官方 App 一致的剩余百分比，并在属性中提供耗材类型、代码、
已用时长和总时长。云端消息通过“机器人通知”事件实体同步；新消息的
`event_type` 为 `notification`，识别为耗材提醒时为 `consumable`。集成加载
时已有的历史消息不会重复触发，之后的新消息按 60 秒轮询周期同步。

## 安装

### HACS（推荐）

1. 打开 **HACS → 集成**。
2. 点击右上角菜单，选择 **自定义存储库**。
3. 填入本项目的 GitHub [仓库地址](https://github.com/chliny/ha-narwal)，类别选择 **Integration（集成）**。
4. 在 HACS 中搜索并下载 **云鲸智能**。
5. 重启 Home Assistant。

后续版本可直接在 HACS 中更新。HACS 下载失败时，请确认仓库根目录包含
`hacs.json`，并且 `custom_components/narwal/manifest.json` 中包含版本号。
操作入口可参考 [HACS 自定义仓库说明](https://www.hacs.xyz/docs/faq/custom_repositories/)。

### 手动安装

将整个 `custom_components/narwal` 目录复制到 Home Assistant 配置目录：

```text
config/
└── custom_components/
    └── narwal/
```

重启 Home Assistant，然后打开：

**设置 → 设备与服务 → 添加集成 → 云鲸智能**

### 配置

添加集成后输入官方 App 使用的手机号和密码。集成默认使用
中国大陆登录区域，即国际区号 `86`、国家代码 `CN`；手机号可以填写普通
11 位号码，也可以使用 `+86` 或 `0086` 前缀。集成会自动列出账号绑定的
机器人；账号有多台设备时可选择要添加的一台。

集成会把凭据保存在 Home Assistant 配置项中，用于访问令牌过期后重新
登录。凭据只发送至云鲸官方 API，不会写入日志。请保护 Home Assistant 的
配置目录和备份。

## 已知限制

- 机器人深度休眠或断网时，Broker 鉴权仍可能成功，但状态与命令会超时。
- 吸力和拖布湿度没有稳定的云端状态广播，集成仅显示本次运行期间由
  Home Assistant 最近设置的值。
- 摄像头原始图像仍涉及设备侧加密，本集成只提供清扫地图，不提供室内摄像
  画面。

## 打开调试日志

在 Home Assistant 的 `configuration.yaml` 中加入：

```yaml
logger:
  default: info
  logs:
    custom_components.narwal: debug
```

保存后重启 Home Assistant，然后在 **设置 → 系统 → 日志** 中查看。完成
排查后建议删除该配置或把级别改回 `info`，避免日志持续增长。

提交问题时可以附上与 `custom_components.narwal` 有关的日志，但请先删除
手机号、密码、访问令牌、设备 ID、序列号、IP 地址和 MQTT 消息内容等敏感
信息。

## 来源与许可

本项目的协议实现参考了
[sjmotew/NarwalIntegration](https://github.com/sjmotew/NarwalIntegration) 和
[rudyll/narwal_r](https://github.com/rudyll/narwal_r) 的社区研究。
