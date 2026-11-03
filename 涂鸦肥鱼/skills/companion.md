# DSH 伴随者

## 工具

| 工具 | 用于 |
|---|---|
| `image-save` | 把用户发来的图落盘，返回路径 |
| `now-image` | 取用户刚发来的图，返回原图路径 |
| `look` | 截图当前屏幕 |
| `find` | 找坐标 |
| `mark` | 记标注 |
| `finish` | 出图，归档，清缓存 |
| `pencil` | 桌面画板 |
| `history` | 历史 |

---

# 零、钩子

## 进入钩子

**有且只有一种情况加载本技能：用户发来一张图片。**

用户发来图片，即加载，即从第 1 步开始执行，不询问、不等确认。

下列任一情况**不加载**本技能：

| 情况 | 走哪个 |
|---|---|
| 用户没发图 | 不加载 |
| 我要看屏幕 / 桌面 / 某个窗口 | `look`，不加载 |
| 我要在桌面画板上作答 | `pencil`，不加载 |
| 用户对上一次交付的标注结果有明确不满 | `dsh-companion-retouch` |

## 退出钩子

`finish` 返回 `ok: true` 的那一刻，本流程结束。

结束后禁止再调用 `find`、`mark`、`read_image`；禁止自行追查遗漏目标；直接交付，等用户回话。

---

# 一、图片来源规则（本流程最硬的约束）

**本流程只允许 `read_image` 一张图：`diff.png`。原图一律不读。**

`diff.png` 也**只看一眼**：

- 禁止 `find { method: "zoom" }`
- 禁止 `find { method: "grid" }`
- 禁止放大、切块、反复调看
- 禁止为了追精度重看第二次

看一眼，按大概位置取坐标，发出去。**精度不重要，快才重要。**

坐标有两个来源，允许同时使用：

| 来源 | 怎么用 |
|---|---|
| 差分块 | 块表里框住目标的，记块号，交给 `mark { blockIds }` —— 坐标由程序取，不用手抄 |
| 自己看 | 块表没框住、或框得不对的，在 `diff.png` 上按大概位置直接写 `shapes` |

想看原图、想仔细找、想放大 —— 那是 `dsh-companion-retouch`，只有用户提出明确不满后才会加载。

---

# 二、执行流程

九步，按顺序执行，不跳步，不回头重来。

```
第 1 步  定标题
第 2 步  image-save { output: "<inbox>/<标题>.png" }
第 3 步  now-image
第 4 步  find { method: "scan", image: <path>, query: <英文词>, layers: "text,obj" }
第 5 步  find { method: "blocks", image: <path>, task: <task> }
第 6 步  read_image <第 5 步返回的 diff.png>      只看一眼
第 7 步  mark { task, image: <path>, label: "<标题>", blockIds: [...], shapes: [...] }
第 8 步  finish { task }
第 9 步  交付
```

所有 `image` 参数填第 3 步拿到的 `path`。

---

# 三、第 1 步：定标题

格式固定：

```
<图片大致描述> + <用户要求>
```

例：

| 用户的图 | 用户说 | 标题 |
|---|---|---|
| bilibili 首页推荐流 | 标注出弱水三千的位置 | 在 bilibili 首页推荐流中找出弱水三千 |
| 百度图片搜索结果 | 找出图中的马 | 在牛和马混杂的网络页面找出马 |
| 百度图片搜索结果 | 标注鸡 | 在鸡兔同笼风格的图库搜索页面中标注鸡 |

标题有三个用途，全部要写：

1. `mark` 的 `label` 参数 —— 决定归档文件名
2. 交付时回复的第一行
3. `task` 的标识，用英文短名即可

---

# 四、第 2 步：image-save

```
image-save { output: "<插件目录>/data/inbox/<标题>.png" }
```

`<插件目录>` 用 `C:\Users\computer\Desktop\plugins\dsh伴随者`。

已存在同名文件时加 `overwrite: true`。

---

# 五、第 3 步：now-image

```
now-image
```

返回 `path`，后续所有 `image` 参数都用它。

返回 `task` 时记下，第 5 步和第 7 步都用这个 `task`。

---

# 六、第 4 步：scan

```
find { method: "scan", image: "<path>", query: "<英文词>", layers: "text,obj" }
```

## query 只发英文

检测器把词原样透传给模型，**不做任何翻译**。中文传进去一个都认不出来，而且失败得很安静 —— 只会得到「物体 0 个」，看起来像图里真没有。

所以 query 必须是英文，从 COCO 80 类里选，再补 2~4 个英文同义词：

```
chicken, rooster, hen
handbag, purse, bag
sports ball, ball
horse, pony, foal
```

传了非 ASCII 字符，`find` 会直接报错并列出 80 类。

## COCO 80 类

```
person           bicycle          car              motorcycle
airplane         bus              train            truck
boat             traffic light    fire hydrant     stop sign
parking meter    bench            bird             cat
dog              horse            sheep            cow
elephant         bear             zebra            giraffe
backpack         umbrella         handbag          tie
suitcase         frisbee          skis             snowboard
sports ball      kite             baseball bat     baseball glove
skateboard       surfboard        tennis racket    bottle
wine glass       cup              fork             knife
spoon            bowl             banana           apple
sandwich         orange           broccoli         carrot
hot dog          pizza            donut            cake
chair            couch            potted plant     bed
dining table     toilet           tv               laptop
mouse            remote           keyboard         cell phone
microwave        oven             toaster          sink
refrigerator     book             clock            vase
scissors         teddy bear       hair drier       toothbrush
```

目标不在 80 类里时，用最接近的上位类。

## 记录第 4 步的结果

文字层和物体层的坐标全部记下，第 7 步直接用。

第 5 步照常执行 —— scan 的物体框常常只框住目标的一部分，差分才是整图的分块地图。

---

# 七、第 5 步：blocks

```
find { method: "blocks", image: "<path>", task: "<task>" }
```

返回块表，每行 `(x,y)-(x2,y2) w h`，外加两个路径：

| 字段 | 内容 |
|---|---|
| `path` | `diff.png`，第 6 步要看的那张 |
| `blocksJson` | 块表文件，`mark { blockIds }` 从这里取坐标 |

`task` 必须和第 3 步拿到的一致 —— 块表按 `work/<task>/find/blocks.json` 存，两边对不上 `mark` 就找不到。

---

# 八、第 6 步：read_image diff.png（只看一眼）

```
read_image "<第 5 步返回的 diff.png>"
```

看一眼，做两件事：

1. 块表里哪个块框住了目标 → 记下块号，进第 7 步的 `blockIds`
2. 块表漏掉的目标 → 按大概位置直接写坐标，进第 7 步的 `shapes`

**看一眼就够。** 不放大、不切块、不重看、不追求像素级贴合。

---

# 九、第 7 步：mark

两种来源一次提交：

```
mark {
  task: "<task>",
  image: "<path>",
  label: "<标题>",
  blockIds: [3, 5, 7],
  shapes: [
    {"kind":"box","x":420,"y":70,"w":250,"h":180}
  ]
}
```

- `blockIds` 来自第 6 步记下的块号，坐标由程序从 `blocks.json` 取
- `shapes` 是第 4 步的 scan 框 + 第 6 步自己按大概位置写的框
- 两种都传会合并；只传一种也行

不分批，一次提交全部。

## shapes 写法

框，xywh：

```json
{"kind":"box","x":957,"y":565,"w":80,"h":104}
```

框，四点：

```json
{"kind":"box","points":[[375,450],[600,450],[600,675],[375,675]]}
```

圆：`{"kind":"circle","center":[997,616],"circumference":251}`
椭圆：`{"kind":"ellipse","foci":[[800,500],[1200,620]],"circumference":1100}`
文字：`{"kind":"text","x":100,"y":200,"text":"内容","size":20}`
点：`{"kind":"dot","x":500,"y":300}`

不传 `stroke`。程序按背景自动选色。

标错了用 `mark { task, reset: true }` 丢弃本轮，再重新提交。

---

# 十、第 8 步：finish

```
finish { task }
```

执行内容：

1. 累计的 shapes 一次画到原图
2. 输出唯一一张最终图，存进 `history/`
3. 清空 `work/<taskId>/`
4. 源图进 `sources/`，保留 5 张

返回 `ok: true` 即触发退出钩子。

---

# 十一、第 9 步：交付

回复格式固定：

```
第一行：<标题>

![<标题>](<最终图路径>)

| 位置 | 内容 |
|---|---|
| ... | ... |

最终图：`<最终图路径>`
```

交付后停手，等用户回话。

---

# 十二、本流程禁止

- 禁止 `read_image` 原图
- 禁止 `find { method: "zoom" }`
- 禁止 `find { method: "grid" }`
- 禁止对 `diff.png` 放大、切块、反复调看
- 禁止用 bash / python 切图、扫像素、算连通域
- 禁止 `query` 传中文或任何非 ASCII 字符
- 禁止分批 `mark`（一次提交全部）
- 禁止交付后自行追查遗漏
- 禁止把图片写到 `data/inbox`、`data/work`、`data/history` 以外的位置

---

# 十三、目录

```
<插件目录>/data/
  inbox/             用户发来的图
  work/<taskId>/     中间图，finish 时清空
    find/diff.png      差分图（本流程唯一允许看的图）
    find/blocks.json   差分块表（mark 按块号取坐标）
  history/           最终图
  sources/           最近 5 张源图
```

---

# 十四、find 参数速查

## scan

```
find { method: "scan", image: "<路径>", query: "<英文词>", layers: "text,obj" }
```

| layers | 返回 |
|---|---|
| `"text,obj"` | 文字 + 物体，本流程固定用这个 |
| `"text"` | 只要文字 |
| `"ui"` | 只要界面控件 |
| `"text,ui,obj"` | 三层全给 |

物体层按 `minScore` 过滤，默认 0.25。

## blocks

```
find { method: "blocks", image: "<路径>", task: "<task>" }
```

返回块表、`diff.png` 和 `blocks.json`。

---

# 十五、look 的 action

| action | 内容 |
|---|---|
| `screen` | 全屏所见 |
| `desktop` | 桌面 |
| `window` | 单窗口 |
| `list` | 可见窗口列表 |
| `info` | 屏幕尺寸 |

`look` 只用于看清屏幕上有什么，不是本流程的一步，结果也不当待标注原图。
