# 阶段产物 JSON Schema（S1 / S2 / S3）

本文件定义 `probe/` 目录下三类 JSON 产物的字段约定，以及**代码实际读取的字段**与**审计/展示字段**的区分。

---

## 并发写入约定（多子代理）

- 每个子代理只写**自己负责目录的专属文件名**（`probe_<路径SHA1前8位>.json`、`units_<唯一名>.json`、`group_<唯一名>.json`），不与其他子代理共用同一文件。
- 写入时先写同目录临时文件再原子改名（`os.replace` / 临时 `.tmp` 落盘后重命名），防止 `progress` 或 `coverage` 在并发读取时读到半截 JSON。

---

## 1. S1 探针产物：`probe_<sha1_8>.json`

```json
{
  "path": "D:/资料盘/Downloads",
  "action": "split",
  "confidence": "high",
  "unit_why": "下载暂存区，内含独立安装包与文档，需按单元拆解",
  "rename_to": null,
  "merge_target": null,
  "enter_children": [],
  "actual": { "what": "下载暂存目录" },
  "hypothesis": "名字为 Downloads，猜测是待分流的暂存区",
  "verification": {
    "method": "抽样检查子项形态与扩展名分布",
    "findings": "含 6 个安装包、4 份 PDF、2 个视频目录"
  },
  "evidence": ["子项类型高度混杂，彼此无依赖关系"],
  "confirmation": {
    "holds": true,
    "confidence": "high",
    "why": "符合暂存区特征",
    "residualRisk": "无"
  }
}
```

### 字段说明

| 字段 | 必填 | 类型 | 消费者（代码是否读取） | 说明 |
|---|---|---|---|---|
| `path` | **是** | `string`（绝对路径） | `coverage` / `decisions` / `plan` / `ui` / `progress` | 节点绝对路径 |
| `action` | **是** | `string`（枚举） | `coverage` / `decisions` / `plan` / `ui` / `progress` | 见下方合法取值表 |
| `enter_children` | 当 `action="enter"` | `string[]` 或 `{path: string}[]` | `coverage` / `progress` | 需要继续下钻的子目录路径清单 |
| `rename_to` | 当 `action="rename"` | `string`（单文件名，无分隔符） | `decisions` / `plan` / `ui` | 建议的新目录名（仅零信息名使用） |
| `merge_target` | 当 `action="merge"` | `string`（目标目录绝对路径） | `plan` / `ui` | 建议合并去向 |
| `unit_why` | 推荐 | `string` | `decisions` / `plan` / `ui` | 一句话动作理由 |
| `confidence` | 推荐 | `"high" \| "medium" \| "low"` | `decisions` / `ui` / `progress` | 置信度 |
| `actual.what` | 可选 | `string` | `decisions` | 目录实际用途简述 |
| `hypothesis` / `verification` / `confirmation` / `evidence` | 审计字段 | `object` / `string[]` | **仅展示/审计**（`ui` 展示 `evidence`；引擎不强校验文本质量） | 给人工复核的推理留痕 |

### `action` 合法取值（共 9 个）

| 值 | 语义 | 后续动作 |
|---|---|---|
| `keep` | 结构清晰、原地保留 | 结束，不进 S2 |
| `enter` | 容器目录，需下钻到 `enter_children` | 对子节点派 S1 探针；若本层也需清点可进 S2 |
| `split` | 内部含多个独立单元，需拆分归位 | 必须进 S2 清点 + S3 归组 |
| `rename` | 零信息名（如 `新建文件夹`）自成一类，建议改名 | `plan` 可生成改名；或进 `ui.html` 节点级待拍板 |
| `merge` | 整目录并入某个已知去向 | `plan` 检查信任区；或进 `ui.html` 节点级待拍板 |
| `ask` | 事实清楚但存在主观取舍，需用户二选一/命名 | 进 `ui.html` 节点级待拍板 |
| `uncertain` | 证据不足，拿不准，暂定不动待复核 | 进 `ui.html` 节点级待拍板（与 `unclear` 等价，优先用 `uncertain`） |
| `unclear` | `uncertain` 的兼容旧名 | 同 `uncertain` |
| `skip` | 受模型限制或已确认不纳入处理 | 跳过，不进 S2/S3/S4 |

---

## 2. S2 最小单元清点产物：`units_<name>.json`

```json
{
  "path": "D:/资料盘/Downloads",
  "total_units": 12,
  "confirmed_units": [
    { "path": "D:/资料盘/Downloads/MyGame", "unit_count": 1 }
  ],
  "loose_files_as_units": [
    "D:/资料盘/Downloads/report.pdf"
  ],
  "sub_containers": [
    { "path": "D:/资料盘/Downloads/SubBox" }
  ]
}
```

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `path` | **是** | `string` | 被清点目录的绝对路径 |
| `total_units` | **是** | 非负整数 `int`（兼容整数值 `float`，禁止 `bool`/负数） | 最小单元总数 |
| `confirmed_units` | 推荐 | `{path: string, unit_count?: int}[]` | S2 核对过的不可拆目录单元；`ui` 仅把 `unit_count == 1` 的项作为可执行来源 |
| `loose_files_as_units` | 推荐 | `string[]` | 散落文件单元（绝对路径或相对 `path` 的文件名）；`ui` 用来核对可执行来源 |
| `sub_containers` | 可选 | `string[]` 或 `{path: string}[]` | 发现的嵌套子容器，`coverage` 会纳入覆盖检查 |

---

## 3. S3 归组产物：`group_<name>.json`

```json
{
  "path": "D:/资料盘/Downloads",
  "groups": [
    {
      "name": "Python 教程视频",
      "units": ["D:/资料盘/Downloads/ep01.mp4", "D:/资料盘/Downloads/ep02.mp4"],
      "relation": "episode-complement",
      "why": "同一门课的连续分集"
    }
  ],
  "solo_units": ["D:/资料盘/Downloads/单独文件.pdf"],
  "uncertain": ["D:/资料盘/Downloads/看不懂的包.bin"]
}
```

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `path` | **是** | `string` | 所属节点绝对路径 |
| `groups` | **是** | `object[]` | 归组列表；每项必含 `name: string`、`units: string[]`（或计数 `int`），可选 `relation`、`why` |
| `groups[].relation` | 可选 | 枚举 | 合法取值：`theme`（同一类东西）、`multi-version`（同作品多版本）、`episode-complement`（同作品分集互补）、`cross-layer-dup`（跨层重复）、`form-cluster`（形态/来源相同的一批） |
| `groups[].keep_together` | 可选 | `bool` | 已确认组内组件有强依赖。工作台仅允许用户选择移动已核实的完整目录或压缩包，分散组件只展示供复核 |
| `solo_units` | 可选 | `string[]` | 独立单件，不强行归组；`ui` 会转为独立单件卡片 |
| `uncertain` | 可选 | `string[]` | 暂定待复核项 |
