# keep-convert

保存 Keep 的跑步和骑行记录，并转成 TCX 和 FIT。

建议把原始 JSON 和 FIT 当作存档。原始 JSON 是 Keep 的完整详情，FIT 包含的运动更全。TCX 只有户外跑和骑行，建议只在目标工具需要 TCX、并且不支持 FIT 时使用。

## 运动类型

| Keep 类型 | TCX | FIT |
| --- | --- | --- |
| 户外跑 | `Running` | `running` / `generic`，带坐标 |
| 室内跑 | 不生成 | `running` / `treadmill`，带时间、距离和心率，不带坐标 |
| 骑行 | `Biking` | `cycling` / `generic`，带坐标 |

FIT 包含户外跑、室内跑和骑行。TCX 包含户外跑和骑行。

TCX 的运动类型只有 Running、Biking 和 Other，没有跑步机。室内跑如果写成 Running，就无法和户外跑区分，所以不生成 TCX。FIT 用 `running` / `treadmill` 标出室内跑。

## 使用方式

需要 Python 3.10 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。在项目目录中先执行 `uv sync`。

三个脚本都可以带一个用户名参数。不写时使用 `default`，数据放在 `data/default/`。写成 `another` 时，数据放在 `data/another/`。

`download_keep.py` 登录 Keep，询问 11 位手机号和密码，保存跑步和骑行列表中的全部活动详情，写入 `data/<用户名>/raw/<活动 id>.json`。这里不做筛选。已有文件会跳过，中断后可以继续执行。

```bash
uv run download_keep.py
```

`convert_tcx.py` 读取该用户的 `raw`，把户外跑和骑行写成 `data/<用户名>/tcx/`。

```bash
uv run convert_tcx.py
```

`convert_fit.py` 读取该用户的 `raw`，把户外跑、室内跑和骑行写成 `data/<用户名>/fit/`。

```bash
uv run convert_fit.py
```

## 数据说明

FIT、TCX 里的距离、时长、卡路里、平均心率和最大心率与 Keep 详情一致。轨迹坐标是 WGS-84，和 Keep 地图上的 GCJ-02 位置会有偏移，轨迹长度也会缩放到官方距离。

FIT 的采样只到整秒，同一秒只留最后一点。心率按时间附在轨迹上，相差超过 10 秒的点没有心率。没有位移的户外跑和室内跑没有坐标。

时长为 0、可疑记录和其他运动不会出现在 TCX 和 FIT 里。

## 致谢

实现参考了 [running_page](https://github.com/yihong0618/running_page)。Keep 数据的解密、坐标转换和运动记录结构来自这个项目。
