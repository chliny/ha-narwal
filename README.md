# 云鲸扫拖机器人 Home Assistant 集成

云鲸扫拖机器人 Home Assistant 自定义集成，通过云鲸云端 API 和 MQTT
协议发现、控制机器人并同步状态。

## 功能

- 开始、暂停、继续和停止清扫
- 返回基站、查找机器人
- 支持设备的吸力设置
- 房间分区清扫（Home Assistant 2026.3 及以上）
- 支持设备的拖布湿度、清洁策略和拖布烘干强度选择
- 支持设备的音量滑块
- 支持官方云端返回的语音包名称与切换
- 更多设备配置开关，包括童锁、宠物模式、地毯清洁、智能脏污检测、热水洗拖布、
  自动/智能/静音集尘和语音控制等
- 清洗并烘干拖布开关，可打开启动、关闭结束洗烘流程
- 支持单独烘干能力的型号额外提供烘干拖布按钮
- 拖布烘干剩余时间
- 支持设备的基站集尘按钮
- 当前地图的清洁任务记录分页列表，并读取最近任务的详情
- 电池健康详情与尘袋健康度
- 云端耗材剩余用量、机器人侧 MQTT 状态及明确的单项重置按钮
- 已保存清洁计划/快捷任务选择与启动；未命名计划按 App 的清洁模式名称展示
- 多地图列表、当前地图选择与切换
- 清洁时间线；清扫面积和时长继续通过 MQTT 实时更新
- 云端机器人通知同步，包括耗材更换或用尽提醒
- 电量、清扫面积、清扫时间，以及显示当前/可用版本的只读固件更新状态
- 回基站和充电状态
- 地图、房间名称、机器人位置、轨迹与障碍物叠加
- 实时推送、断线重连、状态订阅续期和轮询兜底
- 云端凭据失效后的 Home Assistant 重认证流程

具体功能取决于机器人、基站配置和固件。集成会读取 APK 使用的功能列表、
设备配置和基础状态字段，仅在设备声明相应能力时提供吸力、拖布湿度、集尘、
清洁策略、拖布烘干强度、音量、语音包、更多配置开关和单独烘干入口。
电池详情、烘干剩余时间、机器人侧耗材、清洁计划、时间线和多地图功能也只有在
对应 MQTT 请求成功后才创建实体。
不支持这些协议的型号不会展示相应实体。

## 安装

最低要求为 **Home Assistant 2026.3.0**，与 `hacs.json` 一致。
该版本起原生支持房间分区清扫（`Segment` / clean-area API）。

### HACS（推荐）

1. 打开 **HACS → 集成**。
2. 点击右上角菜单，选择 **自定义存储库**。
3. 填入本项目的 GitHub [仓库地址](https://github.com/chliny/ha-narwal)，类别选择 **Integration（集成）**。
4. 在 HACS 中搜索并下载 **云鲸智能**。
5. 重启 Home Assistant。

后续版本可直接在 HACS 中更新。HACS 下载失败时，请确认仓库根目录包含
`hacs.json`，并且 `custom_components/narwal/manifest.json` 中包含版本号。
操作入口可参考 [HACS 自定义仓库说明](https://www.hacs.xyz/docs/faq/custom_repositories/)。

### 自动发送机器人通知

集成不会自行调用 `notify.*` 服务，避免在用户没有配置通知目标时产生副作用。
云端新消息会通过事件实体
`event.narwal_<设备ID>_notification` 发出，推荐使用它触发 Home Assistant 通知：

```yaml
alias: 云鲸机器人通知
triggers:
  - trigger: state
    entity_id: event.narwal_<设备ID>_notification
actions:
  - action: notify.mobile_app_手机
    data:
      title: "云鲸 {{ trigger.to_state.attributes.event_type }}"
      message: >-
        {{ trigger.to_state.attributes.title }}
        {{ trigger.to_state.attributes.content }}
```

只提醒耗材消息时，可以增加条件：

```yaml
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.event_type == 'consumable' }}
```

`sensor.narwal_<设备ID>_latest_notification` 适合在仪表盘显示最新消息，
但不建议作为唯一的通知触发源：相同标题和内容的连续通知可能拥有不同消息 ID，
传感器状态文本却可能相同。事件实体包含 `message_id`、标题、正文、分类和时间，
并按消息 ID 在当前运行周期内去重。云端轮询间隔通常为 60 秒，通知可能有相应延迟。

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
- 部分固件不会持续广播吸力和拖布湿度；收到设备值前，实体可能暂时没有状态。
- 烘干剩余时间实体保留原始整数，暂无单位。
- 电池详情中的容量、温度、电压和电流也保留原始值， 暂无单位。
- 任务详情目前只读取标准清洁记录详情；特殊维护任务只有列表摘要。

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

## 开发版本验证

当前开发依赖固定为 HA 2026.8.1，最低支持 HA 2026.3.0。两者均要求 Python >=3.14.2，
`pyproject.toml` 的项目安装要求则为 Python >=3.14.7。HACS 安装复制集成目录，
不执行项目打包，因此项目的 Python 要求不能替代集成运行时兼容性验证。

Ruff 和 Black 的目标版本固定为 Python 3.12，避免格式化工具生成当前代码库未采用的
异常语法；最低 HA 运行环境使用 Python 3.14.2 及以上。

CI 的最低/当前 HA 测试矩阵分别使用 HA 2026.3.0 和 HA 2026.8.1（均使用 Python 3.14）。
在独立环境中从仓库根目录安装依赖：

```sh
python -m pip install -r requirements.txt 'pytest>=8.0' "homeassistant==${HA_VERSION}"
python -m pytest -q
```

`HA_VERSION` 设置为对应矩阵版本。不要使用 `pip install .`、`pip install -e '.[dev]'`
或 `uv sync` 验证 Python 3.12，因为项目打包要求会先拒绝该 Python 版本；
也不要在最低版本任务中使用固定安装 HA 2026.8.1 的 `requirements-dev.txt`。

本地全量回归结果：HA 2026.3.0 和 HA 2026.8.1（Python 3.14.7）均为 **408 passed**，
集成目录也通过语法编译检查。CI 使用相同的全量测试命令。
实体单元测试会在缺少可选摄像头依赖时使用替身，不能替代完整 HA 启动或真机验证。

## 来源说明

本项目的协议实现参考了
[sjmotew/NarwalIntegration](https://github.com/sjmotew/NarwalIntegration) 和
[rudyll/narwal_r](https://github.com/rudyll/narwal_r) 的社区研究。
